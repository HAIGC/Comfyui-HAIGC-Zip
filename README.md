# Comfyui-HAIGC-Zip

ComfyUI 自定义节点：ZIP 读写、多媒体打包、帧选择、以及图片尺寸倍数取整。

## 中文

### 功能概览

- ZIP 读取：从 ZIP 中读取图片/视频/音频/文本并按序输出
- 命名保留：可输出“命名信息”，用于保存时保持原文件名/路径
- 视频转帧：从视频解码得到帧序列，并可抽帧与限帧
- 帧取帧：输出首帧/中间帧/尾帧，或按规则选择帧输出
- ZIP 保存：把图片/视频/音频/文本/文件写入 ZIP，并在界面提供下载按钮
- 图片尺寸倍数取整：按给定倍数把图片宽高缩放到最接近的倍数尺寸

### 安装

1. 将本仓库放到你的 ComfyUI 目录下：

   `ComfyUI/custom_nodes/Comfyui-HAIGC-Zip`

2. 安装依赖（如果你的环境未包含）：

   - `soundfile>=0.12.1`

3. 重启 ComfyUI

### 节点说明

#### 1) 加载zip文件（HAIGC_LoadImagesFromZip）

- 作用：从 input 目录选择或上传一个 ZIP 文件，读取其中的内容并输出。
- 交互：节点面板里提供“选择文件上传”按钮，上传后自动写入 ComfyUI 的 input 目录并可直接选择。
- 常见用途：将一组图片/视频/音频/字幕/提示词等内容打包后在工作流中统一读取。
- 输出新增：命名信息（任意类型 `*`），用于把原 ZIP 内的成员路径按序传递给保存节点，实现“按原文件名保存”。

#### 2) 视频转帧(含音频)（HAIGC_VideoRelay）

- 输入：视频（VIDEO）
- 可选参数：
  - 每隔N帧：抽帧步长（默认 1）
  - 最大帧数：最多输出多少帧（0 表示不限制）
- 输出：
  - 帧（IMAGE，可能是多帧序列）
  - 音频（AUDIO）
  - 视频信息（HAIGC_VIDEOINFO）

#### 3) 帧取帧(首中尾/指定帧)（HAIGC_VideoFrameRelay）

- 输入：帧（IMAGE，多帧序列或批次）
- 可选参数：指定帧（INT，默认 0）
- 输出：
  - 帧：按“指定帧规则”输出
  - 首帧：总是输出首帧（单帧）
  - 中间帧：总是输出中间帧（单帧）
  - 尾帧：总是输出尾帧（单帧）
- 指定帧规则：
  - 0：输出所有帧
  - 1：输出首帧
  - -1：输出尾帧
  - >= 2：按 1-based 索引取第 N 帧（例如 2 表示第 2 帧），超界会自动夹到最后一帧

#### 4) 文本内容转接（HAIGC_TextRelay）

- 输入：文本（STRING）
- 输出：
  - 文件名（STRING，可空）
  - 内容（STRING）
- 说明：
  - 如果输入文本的第一行看起来像一个文件名（带扩展名或包含路径分隔符），则该行会作为“文件名”，其余内容作为“内容”。
  - 常用于把文本作为 ZIP 中的一个文件写入（配合“保存Zip格式”）。

#### 5) 保存Zip格式（HAIGC_SaveImagesToZip）

- 作用：把输入内容写入一个 ZIP 文件并输出到 ComfyUI 的 output 目录，同时在节点面板提供“下载ZIP”按钮。
- 输入（可选）：zip（任意类型 `*`）
  - 你可以把 IMAGE/VIDEO/AUDIO/STRING/文件引用对象等传入，节点会尽量识别并写入 ZIP。
- 输入（可选）：命名信息（任意类型 `*`）
  - 将“加载zip文件”的命名信息输出连接到这里，可让保存后的 ZIP 尽量沿用原 ZIP 中的文件名与子目录结构。
- 元数据：
  - 当 ComfyUI 未禁用 metadata 时，会把 prompt / extra_pnginfo 写入 PNG 元信息；视频会尽可能携带 metadata。

### 文件名与路径保留（重要）

- 推荐连法：加载zip文件 →（任意处理）→ 保存Zip格式，同时把“命名信息”也一路接到保存节点。
- 命名匹配规则（按序）：
  - 图像：按图像序列顺序依次取用原文件名（含子目录）。保存结果的扩展名尽量沿用原扩展名；如果无法写入该格式，会自动回落到 PNG 并调整扩展名。
  - 视频：优先直接写入源文件并沿用原文件名；否则使用容器格式推断扩展名并生成默认名。
  - 音频：如果能拿到原始字节，则沿用原文件名；否则会编码为 WAV（扩展名会变成 `.wav`）。
  - 文本：如果文本第一行是“文件名/路径”，会以该行作为文件名；否则如果提供了命名信息，则使用命名信息；都没有则使用默认名。
- 重名处理：如果同一路径/文件名出现多次，会自动追加 `_dupN` 避免覆盖。
- 遮罩命名：本插件不会单独输出“遮罩文件名队列”。若需要把遮罩作为独立文件写入 ZIP，请先把遮罩转换为三通道 IMAGE，并在命名信息的图像序列里为它预留对应的文件名顺序。

#### 6) 图片尺寸倍数取整（HAIGC_ImageResizeToMultiple）

- 分类：HAIGC/Image
- 输入：
  - 图片（IMAGE）
  - 倍数：2 / 4 / 8 / 16 / 32 / 64
- 输出：图片（IMAGE）
- 行为：
  - 分别对宽和高计算“最接近的倍数尺寸”，然后进行双线性缩放。
  - 若上下同样接近，优先取更大的那个倍数尺寸。
- 示例：
  - 输入 1023×1023，倍数=2 → 输出 1024×1024

### 常见问题

- 看不到“选择文件上传/下载ZIP”按钮：
  - 请确认 `custom_nodes/Comfyui-HAIGC-Zip/js/haigc_zip.js` 被正确加载（重启 ComfyUI 后刷新网页）。

### 联系方式

- 微信：HAIGC1994

## English

### Overview

- Load ZIP: read images/videos/audios/texts from a ZIP file and output them in order
- Name preservation: output “Naming Info” for saving with original names/paths
- Video to frames: decode video into frames with optional frame skipping/limit
- Frame picker: always output first/middle/last frame and select frames by rules
- Save to ZIP: write images/videos/audios/texts/files into a ZIP, with a download button in UI
- Resize to multiple: resize image width/height to the nearest multiple (2/4/8/16/32/64)

### Installation

1. Place this repository into your ComfyUI custom nodes folder:

   `ComfyUI/custom_nodes/Comfyui-HAIGC-Zip`

2. Install dependencies (if not already available in your environment):

   - `soundfile>=0.12.1`

3. Restart ComfyUI

### Nodes

#### 1) Load ZIP (HAIGC_LoadImagesFromZip)

- Purpose: select or upload a ZIP file from the input directory, then read and output its contents.
- UI: provides an “Upload” button; uploaded ZIP is saved into ComfyUI input directory and becomes selectable.
- New output: Naming Info (`*`). This carries original ZIP member paths in order, so the save node can preserve file names.

#### 2) Video to Frames (with Audio) (HAIGC_VideoRelay)

- Input: VIDEO
- Optional:
  - Every N frames: frame step (default 1)
  - Max frames: maximum number of frames to output (0 = unlimited)
- Output:
  - Frames (IMAGE)
  - Audio (AUDIO)
  - Video info (HAIGC_VIDEOINFO)

#### 3) Frame Picker (first/middle/last/specified) (HAIGC_VideoFrameRelay)

- Input: frames (IMAGE sequence)
- Optional: specified frame (INT, default 0)
- Output:
  - Frames: output based on the rule below
  - First frame: always single-frame output
  - Middle frame: always single-frame output
  - Last frame: always single-frame output
- Rules:
  - 0: output all frames
  - 1: output the first frame
  - -1: output the last frame
  - >= 2: 1-based indexing (e.g. 2 = the 2nd frame); out-of-range is clamped to the last frame

#### 4) Text Relay (HAIGC_TextRelay)

- Input: STRING
- Output:
  - File name (STRING, can be empty)
  - Content (STRING)
- Notes:
  - If the first line looks like a file name/path, it will be treated as the output “file name”, and the rest becomes “content”.

#### 5) Save as ZIP (HAIGC_SaveImagesToZip)

- Purpose: write incoming data into a ZIP file under ComfyUI output directory, and show a “Download ZIP” button.
- Input (optional): `zip` (`*`)
  - Accepts various types; the node attempts to detect and serialize supported content into the ZIP.
- Input (optional): Naming Info (`*`)
  - Connect the “Naming Info” output from the Load ZIP node to preserve original file names and subfolders as much as possible.
- Metadata:
  - When metadata is enabled, prompt/extra_pnginfo is embedded into PNG; video metadata is attached when possible.

### Name & Path Preservation (Important)

- Recommended wiring: Load ZIP → (any processing) → Save as ZIP, and pass “Naming Info” through.
- Matching rules (in order):
  - Images: consumes names sequentially (including subfolders). Tries to keep the original extension; falls back to PNG when needed.
  - Videos: writes the original file with the same name when possible; otherwise generates a default name based on container format.
  - Audios: keeps original name only when original bytes are available; otherwise re-encodes to WAV (`.wav`).
  - Texts: if the first line looks like a file name/path, it is used as the ZIP member name; otherwise it uses Naming Info if provided.
- Duplicates: appends `_dupN` to avoid overwriting.
- Masks: there is no dedicated “mask name queue”. If you need masks as separate files, convert masks to 3-channel IMAGE first and reserve names in the image name sequence.

#### 6) Resize to Multiple (HAIGC_ImageResizeToMultiple)

- Category: HAIGC/Image
- Input:
  - Image (IMAGE)
  - Multiple: 2 / 4 / 8 / 16 / 32 / 64
- Output: IMAGE
- Behavior:
  - Width and height are resized to the nearest multiple using bilinear interpolation.
  - If both neighbors are equally close, it rounds up.
- Example:
  - Input 1023×1023, multiple=2 → Output 1024×1024

### Contact

- WeChat: HAIGC1994
