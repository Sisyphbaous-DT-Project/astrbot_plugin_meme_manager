// 上传队列：文件选择 / 拖拽 / 粘贴 三个入口统一进入待添加预览，
// 确认后串行上传，逐项状态，失败可单独重试。

import { upload, toast, confirmDialog } from "./api.js";
import { renderImage, setPaneMessage, formatSize } from "./preview.js";

const ACCEPTED_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const MAX_BYTES = 20 * 1024 * 1024; // 与后端 MAX_UPLOAD_BYTES 保持一致

const EXT_BY_MIME = {
  "image/png": ".png",
  "image/jpeg": ".jpg",
  "image/gif": ".gif",
  "image/webp": ".webp",
};

// 魔数兜底：file.type 缺失时识别真实格式
async function sniffType(file) {
  if (ACCEPTED_TYPES.has(file.type)) return file.type;
  const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
  if (head[0] === 0x89 && head[1] === 0x50) return "image/png";
  if (head[0] === 0xff && head[1] === 0xd8) return "image/jpeg";
  if (head[0] === 0x47 && head[1] === 0x49 && head[2] === 0x46) return "image/gif";
  if (
    head[8] === 0x57 && head[9] === 0x45 && head[10] === 0x42 && head[11] === 0x50
  )
    return "image/webp";
  return null;
}

function defaultName(mime, seq) {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `截图_${stamp}_${seq}${EXT_BY_MIME[mime] || ".png"}`;
}

export function setupUploads({ getCurrentCategory, getCategories, onChanged }) {
  const modal = document.getElementById("upload-modal");
  const pane = document.getElementById("upload-preview");
  const infoEl = document.getElementById("upload-info");
  const listEl = document.getElementById("upload-list");
  const categorySelect = document.getElementById("upload-category");
  const confirmBtn = document.getElementById("upload-confirm");
  const clearBtn = document.getElementById("upload-clear");
  const moreBtn = document.getElementById("upload-more");
  const closeBtn = document.getElementById("upload-close");
  const progressEl = document.getElementById("upload-progress");
  const cancelRestBtn = document.getElementById("upload-cancel-rest");
  const zoomBtn = document.getElementById("upload-zoom");
  const fileInput = document.getElementById("file-input");

  let queue = []; // {id, file, name, mime, size, url, status, message, result}
  let seq = 0;
  let selectedId = null;
  let uploading = false;
  let cancelRequested = false;
  let batchCategory = null; // 本批目标分类快照
  let lastFocus = null;
  let libraryChanged = false; // 本批是否已有成功落盘（与弹窗生命周期解耦）
  let refreshPromise = null;
  let previewZoom = "fit"; // 添加前预览的缩放模式（R10：支持原始尺寸）

  function applyPreviewZoom() {
    const img = pane.querySelector("img");
    if (img) img.className = previewZoom === "fit" ? "img-fit" : "img-raw";
    pane.classList.toggle("scrollable", previewZoom === "raw");
    zoomBtn.textContent = previewZoom === "fit" ? "原始尺寸" : "适应窗口";
  }

  zoomBtn.addEventListener("click", () => {
    previewZoom = previewZoom === "fit" ? "raw" : "fit";
    applyPreviewZoom();
  });

  // 侧栏缩略图用静态首帧（createImageBitmap 只解第一帧），
  // 避免一批 GIF 同时解码播放；主预览仍用原图 Blob URL 保留动画
  async function makeStaticThumb(file) {
    try {
      const bitmap = await createImageBitmap(file);
      const canvas = document.createElement("canvas");
      const scale = Math.min(1, 80 / Math.max(bitmap.width, bitmap.height));
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      bitmap.close();
      return canvas.toDataURL("image/png");
    } catch {
      return null; // 失败回退到原图 URL（仍然正确，只是可能动）
    }
  }

  function releaseItem(item) {
    if (item.url) {
      URL.revokeObjectURL(item.url);
      item.url = null;
    }
  }

  function resetQueue() {
    for (const item of queue) releaseItem(item);
    queue = [];
    selectedId = null;
    batchCategory = null;
  }

  function isOpen() {
    return !modal.hidden;
  }

  async function refreshChanged() {
    if (refreshPromise) {
      await refreshPromise;
      if (libraryChanged) return refreshChanged();
      return;
    }
    if (!libraryChanged) return;
    libraryChanged = false;
    refreshPromise = (async () => {
      try {
        await onChanged();
      } catch (err) {
        toast("图片已保存，但图库列表刷新失败，请刷新页面查看", {
          type: "error",
          duration: 6000,
        });
      }
    })();
    try {
      await refreshPromise;
    } finally {
      refreshPromise = null;
    }
  }

  function close({ force = false } = {}) {
    if (uploading && !force) {
      const done = queue.filter((i) => i.status === "done" || i.status === "duplicate").length;
      const pending = queue.length - done;
      confirmDialog({
        title: "还有项目在处理",
        text: `还有 ${pending} 项未处理。已完成的 ${done} 项不会回滚，确定关闭吗？`,
        okText: "关闭",
      }).then((ok) => {
        if (ok) {
          cancelRequested = true;
          close({ force: true });
        }
      });
      return;
    }
    modal.hidden = true;
    resetQueue();
    window.removeEventListener("keydown", onKey);
    // 弹窗关闭后，已完成的服务器变更仍要刷新图库（R15）
    if (libraryChanged) {
      void refreshChanged();
    }
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function onKey(e) {
    if (!isOpen()) return;
    if (!document.getElementById("confirm-modal").hidden) return;
    if (e.key === "Escape") close();
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const target = e.target;
      if (target && (target.closest("select, input, textarea, button") || target.isContentEditable)) return;
      const ids = queue.map((i) => i.id);
      const idx = ids.indexOf(selectedId);
      const next =
        e.key === "ArrowDown"
          ? Math.min(queue.length - 1, idx + 1)
          : Math.max(0, idx - 1);
      if (queue[next]) selectItem(queue[next].id);
      e.preventDefault();
    }
  }

  function renderList() {
    listEl.innerHTML = "";
    for (const item of queue) {
      const li = document.createElement("li");
      li.className = "upload-item";
      li.dataset.id = item.id;
      if (item.id === selectedId) li.classList.add("active");

      const thumb = document.createElement("img");
      thumb.className = "upload-thumb";
      thumb.src = item.thumbUrl || item.url;
      thumb.alt = "";

      const meta = document.createElement("div");
      meta.className = "upload-meta";
      const nameEl = document.createElement("div");
      nameEl.className = "upload-name";
      nameEl.textContent = item.name;
      const stateEl = document.createElement("div");
      stateEl.className = `upload-state state-${item.status}`;
      stateEl.textContent = stateText(item);
      meta.appendChild(nameEl);
      meta.appendChild(stateEl);

      const actions = document.createElement("div");
      actions.className = "upload-item-actions";
      if (item.status === "failed") {
        const retry = document.createElement("button");
        retry.className = "link-btn";
        retry.textContent = "重试";
        retry.addEventListener("click", (e) => {
          e.stopPropagation();
          retryItem(item);
        });
        actions.appendChild(retry);
      }
      if (!uploading || item.status === "pending") {
        const remove = document.createElement("button");
        remove.className = "link-btn danger-text";
        remove.textContent = "移除";
        remove.addEventListener("click", (e) => {
          e.stopPropagation();
          removeItem(item.id);
        });
        actions.appendChild(remove);
      }

      li.appendChild(thumb);
      li.appendChild(meta);
      li.appendChild(actions);
      li.addEventListener("click", () => selectItem(item.id));
      listEl.appendChild(li);
    }
    updateFooter();
  }

  function stateText(item) {
    switch (item.status) {
      case "pending":
        return item.valid ? "等待上传" : `不会上传：${item.message}`;
      case "uploading":
        return "上传中……";
      case "done":
        return `已添加为 ${item.result?.filename || item.name}`;
      case "duplicate":
        return `重复跳过（同 ${item.result?.filename}）`;
      case "failed":
        return `失败：${item.message}`;
      case "cancelled":
        return "已取消";
      default:
        return "";
    }
  }

  function selectItem(id) {
    selectedId = id;
    const item = queue.find((i) => i.id === id);
    if (!item) {
      setPaneMessage(pane, "选择左侧项目查看预览");
      infoEl.textContent = "";
      renderList();
      return;
    }
    const img = renderImage(pane, item.url, { zoom: previewZoom });
    applyPreviewZoom();
    // 只有选中项才解码这一张（浏览器按 img 解码）
    img.onerror = () => setPaneMessage(pane, "图片损坏或无法解码");
    img.onload = () => {
      infoEl.textContent =
        `${item.name} · ${item.mime || "未知格式"} · ` +
        `${img.naturalWidth}×${img.naturalHeight} · ${formatSize(item.size)}` +
        (item.mime === "image/gif" || item.mime === "image/webp" || item.mime === "image/png"
          ? "（多帧信息以服务端校验为准；若来源只提供静态图，上传后也为静态图）"
          : "");
    };
    infoEl.textContent = `${item.name} · ${item.mime || "未知格式"} · ${formatSize(item.size)}`;
    renderList();
  }

  function removeItem(id) {
    const idx = queue.findIndex((i) => i.id === id);
    if (idx < 0) return;
    releaseItem(queue[idx]);
    queue.splice(idx, 1);
    if (selectedId === id) {
      selectedId = queue.length ? queue[Math.max(0, idx - 1)].id : null;
      selectItem(selectedId);
    }
    renderList();
    if (!queue.length) close({ force: true });
  }

  function updateFooter() {
    const total = queue.length;
    const done = queue.filter((i) => i.status === "done" || i.status === "duplicate").length;
    const failed = queue.filter((i) => i.status === "failed").length;
    const pendingCount = queue.filter((i) => i.status === "pending" && i.valid).length;
    progressEl.textContent = uploading
      ? `处理中 ${done}/${total}${failed ? `，失败 ${failed}` : ""}`
      : `共 ${total} 项${failed ? `，失败 ${failed}` : ""}`;
    confirmBtn.disabled = uploading || pendingCount === 0;
    confirmBtn.textContent = failed && pendingCount === 0 ? "确认添加" : "确认添加";
    cancelRestBtn.hidden = !uploading;
    clearBtn.disabled = uploading;
    moreBtn.disabled = uploading;
    categorySelect.disabled = uploading;
  }

  async function enqueueFiles(files) {
    if (!files || !files.length) return;
    if (uploading) {
      toast("当前批次正在上传，完成后再添加图片", { type: "error" });
      return;
    }
    if (!isOpen()) lastFocus = document.activeElement;
    let added = 0;
    for (const file of files) {
      const mime = await sniffType(file);
      if (uploading) {
        toast("当前批次正在上传，完成后再添加图片", { type: "error" });
        break;
      }
      seq += 1;
      const name = file.name && file.name !== "image.png" ? file.name : defaultName(mime, seq);
      const item = {
        id: `u${seq}`,
        file,
        name,
        mime,
        size: file.size,
        url: URL.createObjectURL(file),
        thumbUrl: null, // 静态首帧缩略图（异步生成，失败回退 url）
        status: "pending",
        valid: true,
        message: "",
        result: null,
      };
      if (!mime) {
        item.valid = false;
        item.message = "不支持的格式（仅 PNG/JPG/GIF/WEBP）";
      } else if (file.size > MAX_BYTES) {
        item.valid = false;
        item.message = "超过 20 MiB 上限";
      } else if (file.size === 0) {
        item.valid = false;
        item.message = "空文件";
      }
      if (item.valid) {
        makeStaticThumb(file).then((dataUrl) => {
          if (dataUrl && queue.includes(item)) {
            item.thumbUrl = dataUrl;
            renderList();
          }
        });
      }
      queue.push(item);
      added += 1;
    }
    if (!added) return;

    // 默认目标分类：当前分类；全部图片时需手动选择
    if (batchCategory === null) {
      batchCategory = getCurrentCategory() || "";
    }
    renderCategorySelect();

    modal.hidden = false;
    window.addEventListener("keydown", onKey);
    confirmBtn.focus();
    if (!selectedId && queue.length) selectItem(queue[queue.length - 1].id);
    renderList();
  }

  function renderCategorySelect() {
    categorySelect.innerHTML = "";
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "请选择目标分类";
    categorySelect.appendChild(empty);
    for (const cat of getCategories()) {
      const opt = document.createElement("option");
      opt.value = cat.name;
      opt.textContent = cat.name;
      categorySelect.appendChild(opt);
    }
    categorySelect.value = batchCategory || "";
  }

  async function uploadItem(item) {
    item.status = "uploading";
    renderList();
    try {
      // 传原始分类名，bridge 会自行编码路径段；
      // 用界面展示名构造新 File（保留原字节），让截图默认名真正生效
      const fileToSend =
        item.file.name === item.name
          ? item.file
          : new File([item.file], item.name, { type: item.mime || item.file.type });
      const result = await upload(
        `categories/${batchCategory}/images`,
        fileToSend,
      );
      if (result && result.outcome === "duplicate") {
        item.status = "duplicate";
        item.result = result;
        if (result.refresh_ok === false) item.message = result.message || "";
      } else if (result && result.outcome === "added") {
        item.status = "done";
        item.result = result;
        libraryChanged = true;
        if (result.refresh_ok === false) {
          toast(result.message || "已保存但运行时刷新失败，建议重载插件", {
            type: "error",
            duration: 5000,
          });
        }
      } else {
        // 只认明确的业务结果，其余（HTML、字符串等）一律视为失败，不误报成功
        item.status = "failed";
        item.message = "服务器返回了无法识别的响应";
      }
    } catch (err) {
      item.status = "failed";
      item.message = err.message;
    }
    renderList();
    // 弹窗已被关闭时，完成的服务器变更仍要刷新图库（R15）
    if (!isOpen() && libraryChanged) {
      await refreshChanged();
    }
  }

  async function retryItem(item) {
    if (uploading) return;
    if (!batchCategory) {
      toast("请先选择目标分类", { type: "error" });
      return;
    }
    uploading = true;
    try {
      await uploadItem(item);
    } finally {
      uploading = false;
      renderList(); // 统一恢复按钮/进度状态（成功失败都覆盖）
    }
    await refreshChanged();
  }

  async function confirmAll() {
    if (uploading) return;
    if (!batchCategory) {
      toast("请先选择目标分类", { type: "error" });
      categorySelect.focus();
      return;
    }
    uploading = true;
    cancelRequested = false;
    // 快照本批目标分类，之后切换分类不影响本批
    const targets = queue.filter((i) => i.status === "pending" && i.valid);
    try {
      for (const item of targets) {
        if (cancelRequested) {
          if (item.status === "pending") item.status = "cancelled";
          continue;
        }
        // 发送前回查：项目可能在上传期间被用户移除，移除的不再发出
        if (!queue.includes(item) || item.status !== "pending") continue;
        await uploadItem(item);
      }
      await refreshChanged();
    } finally {
      uploading = false;
      renderList();
    }
    const done = queue.filter((i) => i.status === "done").length;
    const dup = queue.filter((i) => i.status === "duplicate").length;
    const failed = queue.filter((i) => i.status === "failed").length;
    if (!failed && !cancelRequested) {
      toast(`添加完成：成功 ${done}，重复跳过 ${dup}`, { type: "success" });
      close({ force: true });
    } else if (failed) {
      toast(`成功 ${done}，重复 ${dup}，失败 ${failed}（可单独重试）`, {
        type: "error",
        duration: 4500,
      });
    }
  }

  // ---- 事件接线 ----

  categorySelect.addEventListener("change", () => {
    if (!uploading) batchCategory = categorySelect.value || null;
  });
  confirmBtn.addEventListener("click", confirmAll);
  cancelRestBtn.addEventListener("click", () => {
    cancelRequested = true;
    toast("已取消剩余未发出的项目（已发送的无法撤销）");
  });
  clearBtn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "清空整批",
      text: `将移除全部 ${queue.length} 个待添加项目，确定吗？`,
      okText: "清空",
      danger: true,
    });
    if (ok) close({ force: true });
  });
  moreBtn.addEventListener("click", () => fileInput.click());
  closeBtn.addEventListener("click", () => close());
  modal.addEventListener("click", (e) => {
    if (e.target === modal) close();
  });

  // 入口 1：点击按钮选择文件
  fileInput.addEventListener("change", () => {
    const files = [...fileInput.files];
    fileInput.value = "";
    enqueueFiles(files);
  });

  // 入口 2：拖拽
  const main = document.querySelector(".main");
  const dropHint = document.getElementById("drop-hint");
  main.addEventListener("dragover", (e) => {
    if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) {
      e.preventDefault();
      dropHint.classList.add("dragging");
    }
  });
  main.addEventListener("dragleave", (e) => {
    if (e.target === main) dropHint.classList.remove("dragging");
  });
  main.addEventListener("drop", (e) => {
    dropHint.classList.remove("dragging");
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    enqueueFiles([...e.dataTransfer.files]);
  });

  // 入口 3：粘贴（只用 clipboardData.items，不碰 files，避免重复入队）
  window.addEventListener("paste", (e) => {
    const target = e.target;
    // 输入框里的文字粘贴必须放行
    if (
      target &&
      (target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable)
    ) {
      return;
    }
    const items = e.clipboardData ? e.clipboardData.items : null;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (!files.length) {
      if (items.length && !isOpen()) {
        toast("剪贴板里没有可用的图片文件（不会从图片链接下载）");
      }
      return;
    }
    e.preventDefault();
    enqueueFiles(files);
  });

  window.addEventListener("beforeunload", () => resetQueue());

  return { enqueueFiles, isOpen };
}
