import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

function buildViewURL(file) {
	const params = new URLSearchParams({
		filename: file.filename,
		type: file.type || "output",
	});
	if (file.subfolder) params.set("subfolder", file.subfolder);
	return api.apiURL("/view?" + params.toString());
}

async function uploadZipToInput(file) {
	const form = new FormData();
	form.append("image", file, file.name);
	form.append("type", "input");
	form.append("overwrite", "true");
	const resp = await api.fetchApi("/upload/image", {
		method: "POST",
		body: form,
	});
	if (!resp.ok) {
		throw new Error(`upload failed: ${resp.status}`);
	}
	return await resp.json();
}

app.registerExtension({
	name: "HAIGC.ZipNodes",
	async beforeRegisterNodeDef(nodeType, nodeData) {
		if (nodeData.name === "HAIGC_LoadImagesFromZip") {
			const onNodeCreated = nodeType.prototype.onNodeCreated;
			nodeType.prototype.onNodeCreated = function () {
				if (onNodeCreated) onNodeCreated.apply(this, arguments);

				const zipWidget = this.widgets?.find((w) => w.name === "zip_file");
				if (!zipWidget) return;
				zipWidget.label = "ZIP文件";

				const uploadWidgets = (this.widgets || []).filter((w) => w?.type === "button" && w?.name === "选择文件上传");
				if (uploadWidgets.length > 0) {
					const keep = uploadWidgets[0];
					keep.__haigcZipUpload = true;
					this.widgets = (this.widgets || []).filter((w) => w === keep || !(w?.type === "button" && w?.name === "选择文件上传"));
				}

				let input = this._haigcZipUploadInput;
				if (!input) {
					input = document.createElement("input");
					input.type = "file";
					input.accept = ".zip,application/zip";
					input.style.display = "none";
					document.body.appendChild(input);
					this._haigcZipUploadInput = input;
				}

				input.onchange = async () => {
					try {
						const f = input.files?.[0];
						if (!f) return;
						const res = await uploadZipToInput(f);
						const uploadedPath = res.subfolder ? `${res.subfolder}/${res.name}` : res.name;
						if (zipWidget.options?.values && !zipWidget.options.values.includes(uploadedPath)) {
							zipWidget.options.values.push(uploadedPath);
							zipWidget.options.values.sort();
						}
						zipWidget.value = uploadedPath;
						app.graph.setDirtyCanvas(true, true);
					} catch (e) {
						console.error(e);
					} finally {
						input.value = "";
					}
				};

				let uploadButton = (this.widgets || []).find((w) => w?.__haigcZipUpload === true);
				if (!uploadButton) {
					uploadButton = this.addWidget("button", "选择文件上传", null, () => input.click(), { serialize: false });
					uploadButton.__haigcZipUpload = true;
				}
			};
		}

		if (nodeData.name === "HAIGC_SaveImagesToZip") {
			const onNodeCreated = nodeType.prototype.onNodeCreated;
			nodeType.prototype.onNodeCreated = function () {
				if (onNodeCreated) onNodeCreated.apply(this, arguments);

				const prefixWidget = this.widgets?.find((w) => w.name === "filename_prefix");
				if (prefixWidget) prefixWidget.label = "文件名前缀";
			};

			nodeType.prototype.onExecuted = function (message) {
				const file = message?.zip?.[0] || message?.images?.[0];
				if (!file) return;

				const url = buildViewURL(file);
				this._haigcZipDownloadUrl = url;

				const downloadWidgets = (this.widgets || []).filter((w) => w?.type === "button" && w?.name === "下载ZIP");
				if (downloadWidgets.length > 0) {
					const keep = downloadWidgets[0];
					keep.__haigcZipDownload = true;
					this.widgets = (this.widgets || []).filter((w) => w === keep || !(w?.type === "button" && w?.name === "下载ZIP"));
				}

				let downloadButton = (this.widgets || []).find((w) => w?.__haigcZipDownload === true);
				if (!downloadButton) {
					downloadButton = this.addWidget(
						"button",
						"下载ZIP",
						null,
						() => {
							const u = this._haigcZipDownloadUrl;
							if (u) window.open(u, "_blank", "noopener,noreferrer");
						},
						{ serialize: false }
					);
					downloadButton.__haigcZipDownload = true;
				}
				app.graph.setDirtyCanvas(true, true);
			};
		}
	},
});
