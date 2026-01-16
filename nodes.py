from __future__ import annotations

import hashlib
import io
import json
import os
import time
import wave
import zipfile

import numpy as np
import torch
from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo

import folder_paths
import node_helpers
from comfy.cli_args import args
from comfy_api.input_impl import VideoFromFile


_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
_ALLOWED_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v", ".gif"}
_ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
_ALLOWED_TEXT_EXTS = {".txt", ".json", ".srt", ".vtt", ".csv", ".md", ".log"}

_IMAGE_NAME_QUEUE_BY_SIG: dict[tuple, list[str]] = {}


def _safe_basename(name: str) -> str:
    name = name.replace("\\", "/")
    return os.path.basename(name)


def _safe_zip_member_relpath(name: str) -> str:
    name = (name or "").replace("\\", "/")
    drive, _ = os.path.splitdrive(name)
    if drive or name.startswith("/") or os.path.isabs(name):
        raise RuntimeError("非法路径")
    norm = os.path.normpath(name).replace("\\", "/")
    if norm.startswith("..") or norm.startswith("../"):
        raise RuntimeError("非法路径")
    if norm == ".":
        raise RuntimeError("非法路径")
    return norm


def _extract_zip_member_to_dir(zf: zipfile.ZipFile, member: str, dest_root: str) -> str:
    rel = _safe_zip_member_relpath(member)
    full = os.path.abspath(os.path.join(dest_root, rel))
    dest_root_abs = os.path.abspath(dest_root)
    if os.path.commonpath((dest_root_abs, full)) != dest_root_abs:
        raise RuntimeError("非法路径")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    try:
        info = zf.getinfo(member)
        expected_size = int(getattr(info, "file_size", 0) or 0)
    except Exception:
        expected_size = 0
    if os.path.isfile(full):
        try:
            if expected_size > 0 and os.stat(full).st_size == expected_size:
                return full
        except Exception:
            pass
    data = zf.read(member)
    with open(full, "wb") as f:
        f.write(data)
    return full


def _wav_bytes_to_audio(raw: bytes) -> dict:
    try:
        wf = wave.open(io.BytesIO(raw), "rb")
    except Exception as e:
        raise RuntimeError(f"无法读取 WAV: {e}")
    with wf:
        channels = int(wf.getnchannels())
        sample_rate = int(wf.getframerate())
        sampwidth = int(wf.getsampwidth())
        frames = int(wf.getnframes())
        pcm = wf.readframes(frames)

    if channels <= 0 or sample_rate <= 0 or frames <= 0:
        raise RuntimeError("无效 WAV")

    if sampwidth == 1:
        a = np.frombuffer(pcm, dtype=np.uint8).astype(np.float32)
        a = (a - 128.0) / 128.0
    elif sampwidth == 2:
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 3:
        b = np.frombuffer(pcm, dtype=np.uint8)
        if b.size % 3 != 0:
            raise RuntimeError("无效 WAV PCM")
        b = b.reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        sign = (v & 0x800000) != 0
        v = v - (sign.astype(np.int32) << 24)
        a = v.astype(np.float32) / 8388608.0
    elif sampwidth == 4:
        a = np.frombuffer(pcm, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"不支持的 WAV 位深: {sampwidth * 8}bit")

    total_samples = a.size
    if total_samples % channels != 0:
        raise RuntimeError("无效 WAV 通道数据")
    a = a.reshape(-1, channels).T
    a = np.clip(a, -1.0, 1.0)
    waveform = torch.from_numpy(a).float().unsqueeze(0)
    return {"waveform": waveform, "sample_rate": sample_rate}


def _list_zip_files_in_input() -> list[str]:
    input_dir = folder_paths.get_input_directory()
    try:
        out: list[str] = []
        for root, _, files in os.walk(input_dir):
            for f in files:
                if os.path.splitext(f)[1].lower() != ".zip":
                    continue
                full = os.path.join(root, f)
                if not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, input_dir).replace(os.sep, "/")
                out.append(rel)
    except Exception:
        return []
    return sorted(out)


def _resolve_zip_in_input(zip_file: str) -> tuple[str, str]:
    input_dir = folder_paths.get_input_directory()
    name, base_dir = folder_paths.annotated_filepath(zip_file)
    if base_dir is None:
        base_dir = input_dir

    base_dir = os.path.abspath(base_dir)
    input_dir_abs = os.path.abspath(input_dir)
    if os.path.normcase(base_dir) != os.path.normcase(input_dir_abs):
        raise RuntimeError("仅支持从 input 目录加载 ZIP")

    name = name.replace("\\", "/")
    drive, _ = os.path.splitdrive(name)
    if drive or os.path.isabs(name) or name.startswith("/"):
        raise RuntimeError("非法路径")
    norm = os.path.normpath(name)
    if norm.startswith("..") or norm.startswith("../") or norm.startswith("..\\"):
        raise RuntimeError("非法路径")

    zip_path = os.path.abspath(os.path.join(base_dir, norm))
    if os.path.commonpath((base_dir, zip_path)) != base_dir:
        raise RuntimeError("非法路径")
    return norm.replace("\\", "/"), zip_path


def _read_zip_contents(zip_path: str):
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile:
        raise RuntimeError("ZIP 文件损坏或格式不正确")
    except Exception as e:
        raise RuntimeError(f"无法打开 ZIP: {e}")

    with zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        name_set = set(names)

        st = os.stat(zip_path)
        zip_key = hashlib.sha256(f"{os.path.abspath(zip_path)}:{st.st_mtime_ns}:{st.st_size}".encode("utf-8")).hexdigest()[:16]
        extract_root = os.path.join(folder_paths.get_temp_directory(), "haigc_zip_extract", zip_key)

        soundfile = None
        try:
            import soundfile as _sf  # type: ignore

            soundfile = _sf
        except Exception:
            soundfile = None

        def _member_key(member: str) -> str:
            member = member.replace("\\", "/")
            dir_prefix = member[: member.rfind("/") + 1] if "/" in member else ""
            base = _safe_basename(member)
            stem = os.path.splitext(base)[0]
            return f"{dir_prefix}{stem}"

        def _safe_member_name(member: str) -> str:
            try:
                return _safe_zip_member_relpath(member)
            except Exception:
                return _safe_basename(member)

        images_seq: list[dict] = []
        videos_seq: list[dict] = []
        audios_seq: list[dict] = []
        texts_seq: list[dict] = []

        for idx, n in enumerate(names):
            n = n.replace("\\", "/")
            base = _safe_basename(n)
            ext = os.path.splitext(base)[1].lower()
            key = _member_key(n)

            if ext in _ALLOWED_IMAGE_EXTS:
                stem = os.path.splitext(base)[0]
                if stem.endswith("_mask"):
                    continue
                dir_prefix = n[: n.rfind("/") + 1] if "/" in n else ""
                stem2, ext2 = os.path.splitext(_safe_basename(n))
                mask_name = f"{dir_prefix}{stem2}_mask{ext2}"
                txt_name = f"{dir_prefix}{stem2}.txt"
                images_seq.append(
                    {
                        "idx": idx,
                        "key": key,
                        "member": n,
                        "mask_member": mask_name if mask_name in name_set else "",
                        "txt_member": txt_name if txt_name in name_set else "",
                        "name": _safe_member_name(n),
                    }
                )
                continue

            if ext in _ALLOWED_VIDEO_EXTS:
                videos_seq.append({"idx": idx, "key": key, "member": n, "name": _safe_member_name(n)})
                continue

            if ext in _ALLOWED_AUDIO_EXTS:
                audios_seq.append({"idx": idx, "key": key, "member": n, "name": _safe_member_name(n), "ext": ext})
                continue

            if ext in _ALLOWED_TEXT_EXTS:
                texts_seq.append({"idx": idx, "key": key, "member": n, "name": _safe_member_name(n)})
                continue

        if len(images_seq) == 0 and len(videos_seq) == 0 and len(audios_seq) == 0 and len(texts_seq) == 0:
            raise RuntimeError("ZIP 内未找到可用内容（支持图片/视频/音频/文本）")

        key_types: dict[str, set] = {}
        for it in images_seq:
            key_types.setdefault(it["key"], set()).add("image")
        for it in videos_seq:
            key_types.setdefault(it["key"], set()).add("video")
        for it in audios_seq:
            key_types.setdefault(it["key"], set()).add("audio")
        for it in texts_seq:
            key_types.setdefault(it["key"], set()).add("text")

        shared_keys = {k for k, ts in key_types.items() if len(ts) >= 2}
        batches: list[dict] = []

        def _sequential_zip(seq_images, seq_videos, seq_audios, seq_texts):
            i_img = 0
            i_vid = 0
            i_aud = 0
            i_txt = 0
            while i_img < len(seq_images) or i_vid < len(seq_videos) or i_aud < len(seq_audios) or i_txt < len(seq_texts):
                b: dict = {}
                if i_img < len(seq_images):
                    b["image"] = seq_images[i_img]
                    i_img += 1
                if i_vid < len(seq_videos):
                    b["video"] = seq_videos[i_vid]
                    i_vid += 1
                if i_aud < len(seq_audios):
                    b["audio"] = seq_audios[i_aud]
                    i_aud += 1
                if i_txt < len(seq_texts):
                    b["text"] = seq_texts[i_txt]
                    i_txt += 1
                batches.append(b)

        if len(shared_keys) > 0:
            first_idx: dict[str, int] = {}
            for seq in (images_seq, videos_seq, audios_seq, texts_seq):
                for it in seq:
                    k = it["key"]
                    if k not in shared_keys:
                        continue
                    cur = first_idx.get(k, None)
                    if cur is None or int(it["idx"]) < cur:
                        first_idx[k] = int(it["idx"])
            shared_order = sorted(shared_keys, key=lambda k: first_idx.get(k, 10**18))

            img_pos: dict[str, list[int]] = {}
            vid_pos: dict[str, list[int]] = {}
            aud_pos: dict[str, list[int]] = {}
            txt_pos: dict[str, list[int]] = {}
            for i, it in enumerate(images_seq):
                img_pos.setdefault(it["key"], []).append(i)
            for i, it in enumerate(videos_seq):
                vid_pos.setdefault(it["key"], []).append(i)
            for i, it in enumerate(audios_seq):
                aud_pos.setdefault(it["key"], []).append(i)
            for i, it in enumerate(texts_seq):
                txt_pos.setdefault(it["key"], []).append(i)

            used_img = [False] * len(images_seq)
            used_vid = [False] * len(videos_seq)
            used_aud = [False] * len(audios_seq)
            used_txt = [False] * len(texts_seq)

            for k in shared_order:
                n_img = len(img_pos.get(k, []))
                n_vid = len(vid_pos.get(k, []))
                n_aud = len(aud_pos.get(k, []))
                n_txt = len(txt_pos.get(k, []))
                max_n = max(n_img, n_vid, n_aud, n_txt)
                for j in range(max_n):
                    b: dict = {}
                    if j < n_img:
                        p = img_pos[k][j]
                        used_img[p] = True
                        b["image"] = images_seq[p]
                    if j < n_vid:
                        p = vid_pos[k][j]
                        used_vid[p] = True
                        b["video"] = videos_seq[p]
                    if j < n_aud:
                        p = aud_pos[k][j]
                        used_aud[p] = True
                        b["audio"] = audios_seq[p]
                    if j < n_txt:
                        p = txt_pos[k][j]
                        used_txt[p] = True
                        b["text"] = texts_seq[p]
                    batches.append(b)

            rem_images = [it for i, it in enumerate(images_seq) if not used_img[i]]
            rem_videos = [it for i, it in enumerate(videos_seq) if not used_vid[i]]
            rem_audios = [it for i, it in enumerate(audios_seq) if not used_aud[i]]
            rem_texts = [it for i, it in enumerate(texts_seq) if not used_txt[i]]
            _sequential_zip(rem_images, rem_videos, rem_audios, rem_texts)
        else:
            _sequential_zip(images_seq, videos_seq, audios_seq, texts_seq)

        output_images: list[torch.Tensor] = []
        output_masks: list[torch.Tensor] = []
        output_prompts: list[str] = []
        output_videos: list[object] = []
        output_audios: list[dict] = []
        output_texts: list[str] = []
        output_naming = {"images": [], "videos": [], "audios": [], "texts": []}

        for b in batches:
            img_it = b.get("image", None)
            if img_it is not None:
                n = str(img_it.get("member", "") or "")
                try:
                    raw = zf.read(n)
                    pil = node_helpers.pillow(Image.open, io.BytesIO(raw))
                    pil = node_helpers.pillow(ImageOps.exif_transpose, pil)
                except Exception as e:
                    raise RuntimeError(f"读取图片失败: {_safe_basename(n)} ({e})")

                if pil.mode == "I":
                    pil = pil.point(lambda i: i * (1 / 255))

                rgb = pil.convert("RGB")
                mask_member = str(img_it.get("mask_member", "") or "")
                if mask_member:
                    try:
                        mask_raw = zf.read(mask_member)
                        mask_pil = node_helpers.pillow(Image.open, io.BytesIO(mask_raw))
                        mask_pil = node_helpers.pillow(ImageOps.exif_transpose, mask_pil)
                        mask_pil = mask_pil.convert("L")
                    except Exception as e:
                        raise RuntimeError(f"读取遮罩失败: {_safe_basename(mask_member)} ({e})")
                    if mask_pil.size != rgb.size:
                        mask_pil = mask_pil.resize(rgb.size, resample=Image.NEAREST)
                    mask_np = np.array(mask_pil).astype(np.float32) / 255.0
                    mask = torch.from_numpy(mask_np)
                elif "A" in pil.getbands() or (pil.mode == "P" and "transparency" in pil.info):
                    rgba = pil.convert("RGBA")
                    mask_np = np.array(rgba.getchannel("A")).astype(np.float32) / 255.0
                    mask = 1.0 - torch.from_numpy(mask_np)
                else:
                    mask = torch.zeros((rgb.size[1], rgb.size[0]), dtype=torch.float32, device="cpu")

                prompt_text = ""
                txt_member = str(img_it.get("txt_member", "") or "")
                if txt_member:
                    try:
                        prompt_text = zf.read(txt_member).decode("utf-8", errors="ignore").strip()
                    except Exception:
                        prompt_text = ""

                img_np = np.array(rgb).astype(np.float32) / 255.0
                img = torch.from_numpy(img_np)[None,]
                safe_member = str(img_it.get("name", "") or "")
                sig_arr = np.clip(np.array(rgb), 0, 255).astype(np.uint8)
                sig = (sig_arr.shape, hashlib.sha1(sig_arr.tobytes()).digest())
                _IMAGE_NAME_QUEUE_BY_SIG.setdefault(sig, []).append(safe_member)

                output_images.append(img)
                output_masks.append(mask.unsqueeze(0))
                output_prompts.append(str(prompt_text or ""))
                output_naming["images"].append(safe_member)

            vid_it = b.get("video", None)
            if vid_it is not None:
                n = str(vid_it.get("member", "") or "")
                safe_member = str(vid_it.get("name", "") or "")
                try:
                    full = _extract_zip_member_to_dir(zf, n, extract_root)
                    v = VideoFromFile(full)
                    try:
                        setattr(v, "__haigc_zip_member", safe_member)
                    except Exception:
                        pass
                    output_videos.append(v)
                    output_naming["videos"].append(safe_member)
                except Exception as e:
                    output_texts.append(f"{safe_member}\n视频读取失败: {e}")
                    output_naming["texts"].append(safe_member)

            aud_it = b.get("audio", None)
            if aud_it is not None:
                n = str(aud_it.get("member", "") or "")
                safe_member = str(aud_it.get("name", "") or "")
                ext = str(aud_it.get("ext", "") or "").lower()
                try:
                    raw = zf.read(n)
                    if ext == ".wav":
                        aud = _wav_bytes_to_audio(raw)
                    else:
                        if soundfile is None:
                            raise RuntimeError("缺少 soundfile，无法解码该音频格式")
                        full = _extract_zip_member_to_dir(zf, n, extract_root)
                        data, sr = soundfile.read(full, always_2d=True, dtype="float32")
                        a = np.asarray(data, dtype=np.float32).T
                        a = np.clip(a, -1.0, 1.0)
                        waveform = torch.from_numpy(a).float().unsqueeze(0)
                        aud = {"waveform": waveform, "sample_rate": int(sr)}
                    if isinstance(aud, dict):
                        aud["__haigc_zip_member"] = safe_member
                        aud["__haigc_zip_bytes"] = raw
                    output_audios.append(aud)
                    output_naming["audios"].append(safe_member)
                except Exception as e:
                    output_texts.append(f"{safe_member}\n音频读取失败: {e}")
                    output_naming["texts"].append(safe_member)

            txt_it = b.get("text", None)
            if txt_it is not None:
                n = str(txt_it.get("member", "") or "")
                safe_member = str(txt_it.get("name", "") or "")
                try:
                    text = zf.read(n).decode("utf-8", errors="ignore")
                except Exception as e:
                    text = f"{safe_member}\n文本读取失败: {e}"
                out_text = text if text.startswith(f"{safe_member}\n") else f"{safe_member}\n{text}"
                output_texts.append(out_text)
                output_naming["texts"].append(safe_member)

        return output_images, output_masks, output_prompts, output_videos, output_audios, output_texts, output_naming


class HAIGC_LoadImagesFromZip:
    @classmethod
    def INPUT_TYPES(s):
        files = _list_zip_files_in_input()
        if not files:
            files = [""]
        if "" not in files:
            files.append("")
        return {
            "required": {"zip_file": (files, {"zip_upload": True})},
            "optional": {
                "批处理游标": ("*",),
                "加载": ("BOOLEAN", {"default": True}),
            },
        }

    CATEGORY = "HAIGC/Zip"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "VIDEO", "AUDIO", "STRING", "*", "*")
    RETURN_NAMES = ("图像", "遮罩", "提示词", "视频", "音频", "文本", "命名信息", "批处理游标")
    FUNCTION = "load_zip"
    OUTPUT_IS_LIST = (True, True, True, True, True, True, False, False)

    def load_zip(self, zip_file: str, 批处理游标=None, 加载: bool = True):
        def _empty():
            naming = {"images": [], "videos": [], "audios": [], "texts": []}
            payload = {
                "__haigc_chain_v1": True,
                "images": [],
                "masks": [],
                "prompts": [],
                "videos": [],
                "audios": [],
                "texts": [],
                "naming": naming,
            }
            return [], [], [], [], [], [], naming, payload

        def _parse_payload(v):
            if not isinstance(v, dict):
                return None
            if v.get("__haigc_chain_v1") is not True:
                return None
            images = v.get("images", None)
            masks = v.get("masks", None)
            prompts = v.get("prompts", None)
            videos = v.get("videos", None)
            audios = v.get("audios", None)
            texts = v.get("texts", None)
            naming = v.get("naming", None)
            if not isinstance(images, list) or not isinstance(masks, list) or not isinstance(prompts, list) or not isinstance(videos, list) or not isinstance(audios, list) or not isinstance(texts, list):
                return None
            if not isinstance(naming, dict):
                naming = {"images": [], "videos": [], "audios": [], "texts": []}
            return images, masks, prompts, videos, audios, texts, naming

        parsed = _parse_payload(批处理游标)
        if parsed is None:
            acc_images, acc_masks, acc_prompts, acc_videos, acc_audios, acc_texts, acc_naming, _ = _empty()
        else:
            acc_images, acc_masks, acc_prompts, acc_videos, acc_audios, acc_texts, acc_naming = parsed
            acc_naming = {
                "images": list(acc_naming.get("images", []) or []),
                "videos": list(acc_naming.get("videos", []) or []),
                "audios": list(acc_naming.get("audios", []) or []),
                "texts": list(acc_naming.get("texts", []) or []),
            }

        if not bool(加载):
            payload = {
                "__haigc_chain_v1": True,
                "images": list(acc_images),
                "masks": list(acc_masks),
                "prompts": list(acc_prompts),
                "videos": list(acc_videos),
                "audios": list(acc_audios),
                "texts": list(acc_texts),
                "naming": acc_naming,
            }
            return list(acc_images), list(acc_masks), list(acc_prompts), list(acc_videos), list(acc_audios), list(acc_texts), acc_naming, payload

        zip_file = str(zip_file or "")
        if zip_file.strip() == "":
            payload = {
                "__haigc_chain_v1": True,
                "images": list(acc_images),
                "masks": list(acc_masks),
                "prompts": list(acc_prompts),
                "videos": list(acc_videos),
                "audios": list(acc_audios),
                "texts": list(acc_texts),
                "naming": acc_naming,
            }
            return list(acc_images), list(acc_masks), list(acc_prompts), list(acc_videos), list(acc_audios), list(acc_texts), acc_naming, payload

        zip_file, zip_path = _resolve_zip_in_input(zip_file)
        if not os.path.isfile(zip_path):
            raise RuntimeError(f"找不到 ZIP 文件: {zip_file}")

        out_images, out_masks, out_prompts, out_videos, out_audios, out_texts, out_naming = _read_zip_contents(zip_path)
        combined_images = list(acc_images) + list(out_images)
        combined_masks = list(acc_masks) + list(out_masks)
        combined_prompts = list(acc_prompts) + list(out_prompts)
        combined_videos = list(acc_videos) + list(out_videos)
        combined_audios = list(acc_audios) + list(out_audios)
        combined_texts = list(acc_texts) + list(out_texts)
        combined_naming = {
            "images": list(acc_naming.get("images", []) or []) + list((out_naming or {}).get("images", []) or []),
            "videos": list(acc_naming.get("videos", []) or []) + list((out_naming or {}).get("videos", []) or []),
            "audios": list(acc_naming.get("audios", []) or []) + list((out_naming or {}).get("audios", []) or []),
            "texts": list(acc_naming.get("texts", []) or []) + list((out_naming or {}).get("texts", []) or []),
        }
        payload = {
            "__haigc_chain_v1": True,
            "images": combined_images,
            "masks": combined_masks,
            "prompts": combined_prompts,
            "videos": combined_videos,
            "audios": combined_audios,
            "texts": combined_texts,
            "naming": combined_naming,
        }
        return combined_images, combined_masks, combined_prompts, combined_videos, combined_audios, combined_texts, combined_naming, payload

    @classmethod
    def IS_CHANGED(s, zip_file: str, **kwargs):
        if not bool(kwargs.get("加载", True)):
            return "disabled"
        try:
            _, zip_path = _resolve_zip_in_input(zip_file)
        except Exception:
            return ""
        if not os.path.isfile(zip_path):
            return ""
        st = os.stat(zip_path)
        return f"{st.st_mtime_ns}:{st.st_size}"

    @classmethod
    def VALIDATE_INPUTS(s, zip_file: str, **kwargs):
        if not bool(kwargs.get("加载", True)):
            return True
        cursor = kwargs.get("批处理游标", None)
        if isinstance(cursor, dict) and cursor.get("__haigc_chain_v1") is True and (not zip_file or str(zip_file).strip() == ""):
            return True
        if not zip_file:
            return "请先上传或选择 ZIP 文件"
        try:
            zip_file, zip_path = _resolve_zip_in_input(zip_file)
        except Exception as e:
            return str(e)
        if not os.path.isfile(zip_path):
            return f"无效 ZIP 文件: {zip_file}"
        return True


class HAIGC_VideoRelay:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "视频": ("VIDEO",),
            },
            "optional": {
                "每隔N帧": ("INT", {"default": 1, "min": 1, "max": 120}),
                "最大帧数": ("INT", {"default": 0, "min": 0, "max": 100000}),
            },
        }

    CATEGORY = "HAIGC/Zip"
    RETURN_TYPES = ("IMAGE", "AUDIO", "HAIGC_VIDEOINFO")
    RETURN_NAMES = ("帧", "音频", "视频信息")
    FUNCTION = "to_frames"
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = (True, True, True)

    def to_frames(self, 视频, 每隔N帧: int = 1, 最大帧数: int = 0):
        if 视频 is None:
            return ([], [], [])

        videos = 视频 if isinstance(视频, (list, tuple)) else [视频]

        out_frames: list[torch.Tensor] = []
        out_audios: list[dict] = []
        out_video_infos: list[dict] = []

        def _scalar_at(value, index: int, default):
            if value is None:
                return default
            if isinstance(value, (list, tuple)):
                if len(value) == 0:
                    return default
                if index < len(value):
                    return value[index]
                return value[0]
            return value

        for idx, v in enumerate(videos):
            comps = None
            if hasattr(v, "get_components"):
                comps = v.get_components()

            source_path = getattr(v, "_VideoFromFile__file", None)
            if not isinstance(source_path, str):
                source_path = ""

            images = getattr(comps, "images", None) if comps is not None else None
            frame_rate = getattr(comps, "frame_rate", None) if comps is not None else None

            width = int(images.shape[2]) if isinstance(images, torch.Tensor) and images.ndim == 4 else 0
            height = int(images.shape[1]) if isinstance(images, torch.Tensor) and images.ndim == 4 else 0
            frame_count = int(images.shape[0]) if isinstance(images, torch.Tensor) and images.ndim == 4 else 0
            fps = float(frame_rate) if frame_rate not in (None, "") else 0.0
            duration = (frame_count / fps) if fps > 0 and frame_count > 0 else 0.0

            has_audio = False
            audio = getattr(comps, "audio", None) if comps is not None else None
            if isinstance(audio, dict):
                wf = audio.get("waveform", None)
                sr = audio.get("sample_rate", None)
                if isinstance(wf, torch.Tensor) and wf.numel() > 0 and isinstance(sr, (int, float)) and int(sr) > 0:
                    has_audio = True

            file_size_mb = 0.0
            try:
                if source_path and os.path.exists(source_path):
                    file_size_mb = os.path.getsize(source_path) / (1024 * 1024)
            except Exception:
                file_size_mb = 0.0

            out_video_infos.append(
                {
                    "width": width,
                    "height": height,
                    "fps": round(float(fps), 3) if fps else 0.0,
                    "total_frames": frame_count,
                    "frame_count": frame_count,
                    "duration": round(float(duration), 3) if duration else 0.0,
                    "filename": os.path.basename(source_path) if source_path else "",
                    "source_path": source_path,
                    "has_audio": has_audio,
                    "file_size_mb": round(float(file_size_mb), 2) if file_size_mb else 0.0,
                    "video_bitrate_kbps": 0,
                    "audio_bitrate_kbps": 0,
                }
            )
            if comps is None:
                continue

            if isinstance(images, torch.Tensor) and images.ndim == 4 and images.shape[-1] == 3:
                total = int(images.shape[0])
                step = max(int(_scalar_at(每隔N帧, idx, 1) or 1), 1)
                limit = int(_scalar_at(最大帧数, idx, 0) or 0)
                idxs = list(range(0, total, step))
                if limit > 0:
                    idxs = idxs[:limit]
                if len(idxs) > 0:
                    out_frames.append(images[idxs])

            if audio is None:
                audio = {"waveform": torch.zeros((1, 1, 0), dtype=torch.float32), "sample_rate": 1}
            elif isinstance(audio, dict):
                if "waveform" not in audio or "sample_rate" not in audio:
                    audio = {"waveform": torch.zeros((1, 1, 0), dtype=torch.float32), "sample_rate": 1}
            out_audios.append(audio)

        return (out_frames, out_audios, out_video_infos)


class HAIGC_VideoFrameRelay:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "帧": ("IMAGE",),
            },
            "optional": {
                "指定帧": ("INT", {"default": 0, "min": -1, "max": 1000000}),
            },
        }

    CATEGORY = "HAIGC/Zip"
    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "IMAGE")
    RETURN_NAMES = ("帧", "首帧", "中间帧", "尾帧")
    FUNCTION = "pick"
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = (True, True, True, True)

    def pick(self, 帧, 指定帧: int = 0):
        if 帧 is None:
            return ([], [], [], [])

        def _scalar_at(value, index: int, default):
            if value is None:
                return default
            if isinstance(value, (list, tuple)):
                if len(value) == 0:
                    return default
                if index < len(value):
                    return value[index]
                return value[0]
            return value

        def _normalize_sequences(v) -> list[torch.Tensor]:
            if v is None:
                return []
            if isinstance(v, torch.Tensor):
                if v.ndim == 4 and v.shape[-1] == 3:
                    return [v]
                return []
            if isinstance(v, (list, tuple)):
                seq = list(v)
                while len(seq) == 1 and isinstance(seq[0], (list, tuple)):
                    seq = list(seq[0])
                tensors = [t for t in seq if isinstance(t, torch.Tensor) and t.ndim == 4 and t.shape[-1] == 3]
                if len(tensors) == 0:
                    return []
                if any(int(t.shape[0]) > 1 for t in tensors) or len(tensors) == 1:
                    return tensors
                try:
                    return [torch.cat(tensors, dim=0)]
                except Exception:
                    return tensors
            return []

        sequences = _normalize_sequences(帧)
        if len(sequences) == 0:
            return ([], [], [], [])

        out_frames: list[torch.Tensor] = []
        out_first: list[torch.Tensor] = []
        out_mid: list[torch.Tensor] = []
        out_last: list[torch.Tensor] = []

        for idx, images in enumerate(sequences):
            total = int(images.shape[0])
            if total <= 0:
                continue

            out_first.append(images[0:1])
            out_last.append(images[total - 1 : total])
            mid = total // 2
            out_mid.append(images[mid : mid + 1])

            raw_pick_index = _scalar_at(指定帧, idx, 0)
            try:
                pick_index = int(raw_pick_index) if raw_pick_index is not None else 0
            except Exception:
                pick_index = 0

            if pick_index == 0:
                out_frames.append(images)
            elif pick_index == -1:
                out_frames.append(images[total - 1 : total])
            else:
                pick_index = max(int(pick_index), 1) - 1
                if pick_index >= total:
                    pick_index = total - 1
                out_frames.append(images[pick_index : pick_index + 1])

        return (out_frames, out_first, out_mid, out_last)


class HAIGC_TextRelay:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "文本": ("STRING",),
            },
        }

    CATEGORY = "HAIGC/Zip"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("文件名", "内容")
    FUNCTION = "to_text"
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = (True, True)

    def to_text(self, 文本):
        texts = 文本 if isinstance(文本, (list, tuple)) else [文本]

        out_names: list[str] = []
        out_contents: list[str] = []
        for t in texts:
            if t is None:
                out_names.append("")
                out_contents.append("")
                continue
            s = str(t)
            name = ""
            body = s
            parts = s.split("\n", 1)
            if len(parts) == 2:
                head = parts[0].strip()
                ext = os.path.splitext(head)[1].lower()
                if ext in _ALLOWED_TEXT_EXTS or "/" in head or "\\" in head:
                    name = head
                    body = parts[1]
            out_names.append(name)
            out_contents.append(str(body))
        return (out_names, out_contents)


class HAIGC_SaveImagesToZip:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self._run_key = None
        self._zip_info = None
        self._written_any = 0
        self._written_images = 0
        self._written_videos = 0
        self._written_audios = 0
        self._written_texts = 0
        self._written_files = 0
        self._seen_images = set()
        self._seen_files = set()
        self._seen_arcnames = set()
        self._ui_emitted = False
        self._last_call_ts = 0.0

    @classmethod
    def INPUT_TYPES(s):
        return {
            "optional": {
                "zip": ("*",),
                "命名信息": ("*",),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    @classmethod
    def VALIDATE_INPUTS(s, input_types=None, **kwargs):
        return True

    RETURN_TYPES = ()
    FUNCTION = "save_zip"
    OUTPUT_NODE = True
    CATEGORY = "HAIGC/Zip"

    def _resolve_annotated_file(self, name: str) -> tuple[str, str]:
        name = (name or "").strip()
        if not name:
            raise RuntimeError("非法路径")
        raw_name, base_dir = folder_paths.annotated_filepath(name)
        if base_dir is None:
            base_dir = folder_paths.get_input_directory()

        base_dir = os.path.abspath(base_dir)
        raw_name = raw_name.replace("\\", "/")
        drive, _ = os.path.splitdrive(raw_name)
        if drive or os.path.isabs(raw_name) or raw_name.startswith("/"):
            raise RuntimeError("非法路径")
        norm = os.path.normpath(raw_name)
        if norm.startswith("..") or norm.startswith("../") or norm.startswith("..\\"):
            raise RuntimeError("非法路径")

        full = os.path.abspath(os.path.join(base_dir, norm))
        if os.path.commonpath((base_dir, full)) != base_dir:
            raise RuntimeError("非法路径")
        return norm.replace("\\", "/"), full

    def _audio_to_wav_bytes(self, audio) -> bytes:
        if isinstance(audio, dict):
            waveform = audio.get("waveform", None)
            sample_rate = audio.get("sample_rate", None)
        else:
            waveform = getattr(audio, "waveform", None)
            sample_rate = getattr(audio, "sample_rate", None)

        if waveform is None or sample_rate is None:
            raise RuntimeError("无效音频输入")

        if isinstance(waveform, torch.Tensor):
            w = waveform.detach().to(device="cpu")
        else:
            w = torch.as_tensor(waveform, device="cpu")

        if w.ndim == 3:
            w = w[0]
        if w.ndim == 1:
            w = w.unsqueeze(0)
        if w.ndim != 2:
            raise RuntimeError("无效音频波形维度")

        w = torch.clamp(w, -1.0, 1.0)
        w_i16 = (w * 32767.0).to(dtype=torch.int16)
        pcm = w_i16.t().contiguous().numpy().tobytes()

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(int(w_i16.shape[0]))
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm)
        return buf.getvalue()

    def save_zip(
        self,
        zip=None,
        images=None,
        命名信息=None,
        prompt=None,
        extra_pnginfo=None,
        **kwargs,
    ):
        filename_prefix = "ComfyUI_ZIP"
        if zip is None and images is not None:
            zip = images
        elif zip is not None and images is not None:
            zip = [zip, images]

        if zip is None:
            return {"ui": {"images": []}}

        def _as_list(v):
            if v is None:
                return []
            if isinstance(v, (list, tuple)):
                return list(v)
            return [v]

        def _parse_naming(v):
            if v is None:
                return None
            if isinstance(v, dict):
                return {
                    "images": _as_list(v.get("images", None) or v.get("图像", None) or v.get("image_names", None) or v.get("图像文件名", None)),
                    "videos": _as_list(v.get("videos", None) or v.get("视频", None) or v.get("video_names", None) or v.get("视频文件名", None)),
                    "audios": _as_list(v.get("audios", None) or v.get("音频", None) or v.get("audio_names", None) or v.get("音频文件名", None)),
                    "texts": _as_list(v.get("texts", None) or v.get("文本", None) or v.get("text_names", None) or v.get("文本文件名", None)),
                }
            return None

        try:
            marker_obj = {
                "prompt": prompt or {},
                "extra_pnginfo": extra_pnginfo or {},
            }
            marker_json = json.dumps(
                marker_obj,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except Exception:
            marker_json = b""
        run_key = hashlib.sha256(marker_json).hexdigest()[:16]

        now = time.time()
        if run_key != self._run_key or (now - self._last_call_ts) > 60.0:
            self._run_key = run_key
            self._zip_info = None
            self._written_any = 0
            self._written_images = 0
            self._written_videos = 0
            self._written_audios = 0
            self._written_texts = 0
            self._written_files = 0
            self._seen_images = set()
            self._seen_files = set()
            self._seen_arcnames = set()
            self._name_queue_images = []
            self._name_queue_videos = []
            self._name_queue_audios = []
            self._name_queue_texts = []
            self._ui_emitted = False
        self._last_call_ts = now

        if not hasattr(self, "_name_ctx_set") or self._name_ctx_set != run_key:
            naming = _parse_naming(命名信息)
            if naming is not None:
                self._name_queue_images = [str(x) for x in naming.get("images", []) if str(x).strip() != ""]
                self._name_queue_videos = [str(x) for x in naming.get("videos", []) if str(x).strip() != ""]
                self._name_queue_audios = [str(x) for x in naming.get("audios", []) if str(x).strip() != ""]
                self._name_queue_texts = [str(x) for x in naming.get("texts", []) if str(x).strip() != ""]
            self._name_ctx_set = run_key

        if self._zip_info is None:
            def _peek_dimensions(v):
                if v is None:
                    return 0, 0
                if isinstance(v, torch.Tensor):
                    if v.ndim >= 4:
                        return int(v.shape[2]), int(v.shape[1])
                    return 0, 0
                if isinstance(v, (list, tuple)):
                    for x in v:
                        w, h = _peek_dimensions(x)
                        if w or h:
                            return w, h
                    return 0, 0
                if hasattr(v, "get_dimensions"):
                    try:
                        w, h = v.get_dimensions()
                        return int(w), int(h)
                    except Exception:
                        return 0, 0
                return 0, 0

            width, height = _peek_dimensions(zip)

            full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                filename_prefix, self.output_dir, width, height
            )
            zip_filename = f"{filename}_{counter:05}_.zip"
            zip_path = os.path.join(full_output_folder, zip_filename)
            self._zip_info = (zip_filename, zip_path, subfolder)
        else:
            zip_filename, zip_path, subfolder = self._zip_info

        metadata = None
        if not args.disable_metadata:
            metadata = PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for x in extra_pnginfo:
                    metadata.add_text(x, json.dumps(extra_pnginfo[x]))

        zip_mode = "w" if self._written_any == 0 else "a"
        video_metadata = None
        if not args.disable_metadata:
            vm = {}
            if extra_pnginfo is not None:
                vm.update(extra_pnginfo)
            if prompt is not None:
                vm["prompt"] = prompt
            if len(vm) > 0:
                video_metadata = vm

        with zipfile.ZipFile(zip_path, zip_mode, compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            def _resolve_file_ref(obj: dict) -> tuple[str, str] | None:
                filename = obj.get("filename", None)
                if filename is None:
                    filename = obj.get("name", None)
                if not filename:
                    return None
                subfolder = obj.get("subfolder", "") or ""
                t = obj.get("type", None) or "input"
                base_dir = folder_paths.get_directory_by_type(str(t))
                if base_dir is None:
                    return None
                base_dir = os.path.abspath(base_dir)
                rel = os.path.normpath(os.path.join(subfolder, str(filename))).replace("\\", "/")
                drive, _ = os.path.splitdrive(rel)
                if drive or os.path.isabs(rel) or rel.startswith("/") or rel.startswith("../") or rel.startswith("..\\") or rel.startswith(".."):
                    return None
                full = os.path.abspath(os.path.join(base_dir, rel))
                if os.path.commonpath((base_dir, full)) != base_dir:
                    return None
                return rel.replace("\\", "/"), full

            def _write_image_tensor(img_tensor: torch.Tensor):
                t = img_tensor
                if t.ndim == 4:
                    batch = [t[i : i + 1] for i in range(t.shape[0])]
                else:
                    batch = [t]
                for one in batch:
                    i = 255.0 * one[0].detach().to(device="cpu").numpy()
                    arr = np.clip(i, 0, 255).astype(np.uint8)
                    sig = (arr.shape, hashlib.sha1(arr.tobytes()).digest())
                    orig_name = None
                    if hasattr(self, "_name_queue_images") and len(self._name_queue_images) > 0:
                        orig_name = self._name_queue_images.pop(0)
                    if orig_name is None:
                        orig_queue = _IMAGE_NAME_QUEUE_BY_SIG.get(sig, None)
                        if orig_queue is not None and len(orig_queue) > 0:
                            orig_name = orig_queue.pop(0)
                    if orig_name is None:
                        if sig in self._seen_images:
                            continue
                        self._seen_images.add(sig)
                    img = Image.fromarray(arr)
                    buf = io.BytesIO()
                    arcname = None
                    if orig_name:
                        try:
                            arcname = _safe_zip_member_relpath(str(orig_name).replace("\\", "/"))
                        except Exception:
                            arcname = _safe_basename(str(orig_name))
                    if not arcname:
                        arcname = f"{filename_prefix}_{self._written_images:05}.png"

                    base, ext = os.path.splitext(arcname)
                    ext_l = ext.lower()
                    if ext_l in (".jpg", ".jpeg"):
                        img.save(buf, format="JPEG", quality=95, subsampling=0)
                    elif ext_l == ".webp":
                        img.save(buf, format="WEBP", quality=95, method=4)
                    elif ext_l in (".bmp", ".tif", ".tiff"):
                        img.save(buf, format="PNG", pnginfo=metadata, compress_level=4)
                        arcname = f"{base}.png"
                    else:
                        img.save(buf, format="PNG", pnginfo=metadata, compress_level=4)
                        if ext == "":
                            arcname = f"{arcname}.png"

                    if arcname in self._seen_arcnames:
                        n = 2
                        while True:
                            cand = f"{base}_dup{n}{os.path.splitext(arcname)[1]}"
                            if cand not in self._seen_arcnames:
                                arcname = cand
                                break
                            n += 1
                    self._seen_arcnames.add(arcname)
                    zf.writestr(arcname, buf.getvalue())
                    self._written_images += 1
                    self._written_any += 1

            def _write_video_obj(v):
                if not hasattr(v, "save_to"):
                    return
                orig_name = getattr(v, "__haigc_zip_member", None)
                if orig_name is None and hasattr(self, "_name_queue_videos") and len(self._name_queue_videos) > 0:
                    orig_name = self._name_queue_videos.pop(0)
                source_path = getattr(v, "_VideoFromFile__file", None)
                arcname = None
                if orig_name:
                    try:
                        arcname = _safe_zip_member_relpath(str(orig_name).replace("\\", "/"))
                    except Exception:
                        arcname = _safe_basename(str(orig_name))

                if arcname and isinstance(source_path, str) and os.path.isfile(source_path):
                    base, ext = os.path.splitext(arcname)
                    if arcname in self._seen_arcnames:
                        n = 2
                        while True:
                            cand = f"{base}_dup{n}{ext}"
                            if cand not in self._seen_arcnames:
                                arcname = cand
                                break
                            n += 1
                    self._seen_arcnames.add(arcname)
                    zf.write(source_path, arcname=arcname)
                    self._written_videos += 1
                    self._written_any += 1
                    return

                buf = io.BytesIO()
                v.save_to(buf, metadata=video_metadata)
                data = buf.getvalue()
                if not arcname:
                    fmt = None
                    if hasattr(v, "get_container_format"):
                        try:
                            fmt = str(v.get_container_format()).lower()
                        except Exception:
                            fmt = None
                    ext = "mp4"
                    if fmt:
                        if "webm" in fmt:
                            ext = "webm"
                        elif "matroska" in fmt:
                            ext = "mkv"
                        elif "avi" in fmt:
                            ext = "avi"
                        elif "mp4" in fmt or "mov" in fmt:
                            ext = "mp4"
                    arcname = f"{filename_prefix}_video_{self._written_videos:05}.{ext}"

                base, ext = os.path.splitext(arcname)
                if arcname in self._seen_arcnames:
                    n = 2
                    while True:
                        cand = f"{base}_dup{n}{ext}"
                        if cand not in self._seen_arcnames:
                            arcname = cand
                            break
                        n += 1
                self._seen_arcnames.add(arcname)
                zf.writestr(arcname, data)
                self._written_videos += 1
                self._written_any += 1

            def _write_audio_obj(a):
                orig_name = a.get("__haigc_zip_member", None) if isinstance(a, dict) else getattr(a, "__haigc_zip_member", None)
                orig_bytes = a.get("__haigc_zip_bytes", None) if isinstance(a, dict) else getattr(a, "__haigc_zip_bytes", None)
                if orig_name is None and hasattr(self, "_name_queue_audios") and len(self._name_queue_audios) > 0:
                    orig_name = self._name_queue_audios.pop(0)
                arcname = None
                if orig_name:
                    try:
                        arcname = _safe_zip_member_relpath(str(orig_name).replace("\\", "/"))
                    except Exception:
                        arcname = _safe_basename(str(orig_name))
                if arcname and isinstance(orig_bytes, (bytes, bytearray)) and len(orig_bytes) > 0:
                    base, ext = os.path.splitext(arcname)
                    if arcname in self._seen_arcnames:
                        n = 2
                        while True:
                            cand = f"{base}_dup{n}{ext}"
                            if cand not in self._seen_arcnames:
                                arcname = cand
                                break
                            n += 1
                    self._seen_arcnames.add(arcname)
                    zf.writestr(arcname, bytes(orig_bytes))
                    self._written_audios += 1
                    self._written_any += 1
                    return

                data = self._audio_to_wav_bytes(a)
                if arcname:
                    base, _ = os.path.splitext(arcname)
                    arcname = f"{base}.wav"
                else:
                    arcname = f"{filename_prefix}_audio_{self._written_audios:05}.wav"
                base, ext = os.path.splitext(arcname)
                if arcname in self._seen_arcnames:
                    n = 2
                    while True:
                        cand = f"{base}_dup{n}{ext}"
                        if cand not in self._seen_arcnames:
                            arcname = cand
                            break
                        n += 1
                self._seen_arcnames.add(arcname)
                zf.writestr(arcname, data)
                self._written_audios += 1
                self._written_any += 1

            def _write_text(s: str):
                if s is None:
                    return
                s = str(s)
                if s.strip() == "":
                    return
                queued_name = None
                if hasattr(self, "_name_queue_texts") and len(self._name_queue_texts) > 0:
                    queued_name = self._name_queue_texts.pop(0)
                arcname = None
                body = s
                parts = s.split("\n", 1)
                if len(parts) == 2:
                    head = parts[0].strip()
                    ext = os.path.splitext(head)[1].lower()
                    if ext in _ALLOWED_TEXT_EXTS or "/" in head or "\\" in head:
                        try:
                            arcname = _safe_zip_member_relpath(head.replace("\\", "/"))
                        except Exception:
                            arcname = _safe_basename(head)
                        body = parts[1]
                if not arcname:
                    if queued_name:
                        try:
                            arcname = _safe_zip_member_relpath(str(queued_name).replace("\\", "/"))
                        except Exception:
                            arcname = _safe_basename(str(queued_name))
                    if not arcname:
                        arcname = f"{filename_prefix}_text_{self._written_texts:05}.txt"
                base, ext = os.path.splitext(arcname)
                if arcname in self._seen_arcnames:
                    n = 2
                    while True:
                        cand = f"{base}_dup{n}{ext}"
                        if cand not in self._seen_arcnames:
                            arcname = cand
                            break
                        n += 1
                self._seen_arcnames.add(arcname)
                data = str(body).encode("utf-8")
                zf.writestr(arcname, data)
                self._written_texts += 1
                self._written_any += 1

            def _write_file(full_path: str, rel_in_zip: str):
                if not os.path.isfile(full_path):
                    return
                st = os.stat(full_path)
                sig = (os.path.normcase(full_path), int(st.st_mtime_ns), int(st.st_size))
                if sig in self._seen_files:
                    return
                self._seen_files.add(sig)
                arcname = f"files/{rel_in_zip.replace('\\', '/')}"
                zf.write(full_path, arcname=arcname)
                self._written_files += 1
                self._written_any += 1

            def _write_bytes(b: bytes):
                if not b:
                    return
                arcname = f"{filename_prefix}_blob_{self._written_files:05}.bin"
                zf.writestr(arcname, b)
                self._written_files += 1
                self._written_any += 1

            def _write_any(v):
                if v is None:
                    return
                if isinstance(v, (list, tuple)):
                    for x in v:
                        _write_any(x)
                    return
                if isinstance(v, torch.Tensor):
                    _write_image_tensor(v)
                    return
                if isinstance(v, (bytes, bytearray)):
                    _write_bytes(bytes(v))
                    return
                if isinstance(v, dict):
                    if "waveform" in v and "sample_rate" in v:
                        _write_audio_obj(v)
                        return
                    ref = _resolve_file_ref(v)
                    if ref is not None:
                        rel, full = ref
                        _write_file(full, rel)
                        return
                    _write_text(json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str))
                    return
                if hasattr(v, "save_to"):
                    _write_video_obj(v)
                    return
                waveform = getattr(v, "waveform", None)
                sample_rate = getattr(v, "sample_rate", None)
                if waveform is not None and sample_rate is not None:
                    _write_audio_obj(v)
                    return
                if isinstance(v, str):
                    raw = v.strip()
                    if raw != "":
                        try:
                            rel, full = self._resolve_annotated_file(raw)
                            if os.path.isfile(full):
                                _write_file(full, rel)
                                return
                        except Exception:
                            pass
                        _write_text(raw)
                    return
                _write_text(str(v))

            _write_any(zip)

        if self._written_any == 0:
            raise RuntimeError("未写入任何内容（zip 输入为空或类型不支持）")

        ui_file = {
            "filename": zip_filename,
            "subfolder": subfolder,
            "type": self.type,
        }
        if self._ui_emitted:
            return {"ui": {"images": []}}
        self._ui_emitted = True
        return {
            "ui": {
                "images": [ui_file],
            }
        }


NODE_CLASS_MAPPINGS = {
    "HAIGC_LoadImagesFromZip": HAIGC_LoadImagesFromZip,
    "HAIGC_VideoRelay": HAIGC_VideoRelay,
    "HAIGC_VideoFrameRelay": HAIGC_VideoFrameRelay,
    "HAIGC_TextRelay": HAIGC_TextRelay,
    "HAIGC_SaveImagesToZip": HAIGC_SaveImagesToZip,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "HAIGC_LoadImagesFromZip": "加载zip文件",
    "HAIGC_VideoRelay": "视频转帧(含音频)",
    "HAIGC_VideoFrameRelay": "帧取帧(首中尾/指定帧)",
    "HAIGC_TextRelay": "文本内容转接",
    "HAIGC_SaveImagesToZip": "保存Zip格式",
}
