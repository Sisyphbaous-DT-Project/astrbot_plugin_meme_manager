// 预览组件：添加前预览与图库大图查看器共用的视觉与交互。
// 原则：object-fit: contain 完整显示不裁切；适应窗口/原始尺寸切换；棋盘格区分透明。

import { apiGet, download, toast } from "./api.js";

export function b64ToBlobUrl(b64, mime) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return URL.createObjectURL(new Blob([bytes], { type: mime }));
}

// 给指定容器渲染一张图：返回 <img>；调用方负责 URL 生命周期
export function renderImage(pane, url, { zoom = "fit" } = {}) {
  pane.innerHTML = "";
  const img = document.createElement("img");
  img.className = zoom === "fit" ? "img-fit" : "img-raw";
  img.alt = "";
  img.src = url;
  pane.appendChild(img);
  return img;
}

export function setPaneMessage(pane, text) {
  pane.innerHTML = "";
  const div = document.createElement("div");
  div.className = "preview-placeholder";
  div.textContent = text;
  pane.appendChild(div);
}

export function formatSize(bytes) {
  if (bytes == null) return "未知";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

export function formatInfo({ name, mime, width, height, size, isAnimated, extra }) {
  const parts = [
    name || "未知文件名",
    mime || "未知格式",
    width != null && height != null ? `${width}×${height}` : "尺寸未知",
    formatSize(size),
  ];
  if (isAnimated === true) parts.push("动图");
  if (extra) parts.push(extra);
  return parts.join(" · ");
}

// 图库大图查看器
export function createGalleryViewer({ onClose } = {}) {
  const modal = document.getElementById("viewer");
  const pane = document.getElementById("viewer-pane");
  const titleEl = document.getElementById("viewer-title");
  const infoEl = document.getElementById("viewer-info");
  const zoomBtn = document.getElementById("viewer-zoom");
  const downloadBtn = document.getElementById("viewer-download");
  const closeBtn = document.getElementById("viewer-close");
  const prevBtn = document.getElementById("viewer-prev");
  const nextBtn = document.getElementById("viewer-next");

  let items = [];
  let index = 0;
  let zoom = "fit";
  let blobUrl = null;
  let lastFocus = null;
  let showSeq = 0; // 大图请求序号：丢弃过时响应，防止连点串图

  function releaseUrl() {
    if (blobUrl) {
      URL.revokeObjectURL(blobUrl);
      blobUrl = null;
    }
  }

  async function show() {
    const item = items[index];
    if (!item) return;
    const mySeq = ++showSeq;
    titleEl.textContent = `${item.category}/${item.name}`;
    zoom = "fit";
    zoomBtn.textContent = "原始尺寸";
    pane.classList.remove("scrollable");
    setPaneMessage(pane, "加载中……");
    infoEl.textContent = "";
    try {
      const full = await apiGet("image", {
        category: item.category,
        name: item.name,
      });
      // 响应到达时若已切换图片或已关闭，丢弃本次结果
      if (mySeq !== showSeq || modal.hidden) return;
      releaseUrl();
      blobUrl = b64ToBlobUrl(full.content, full.mime);
      const img = renderImage(pane, blobUrl, { zoom });
      img.onerror = () => setPaneMessage(pane, "图片解码失败");
      infoEl.textContent = formatInfo({
        name: item.name,
        mime: full.mime,
        width: full.width,
        height: full.height,
        size: full.size,
        isAnimated: full.is_animated,
      });
    } catch (err) {
      if (mySeq !== showSeq || modal.hidden) return;
      setPaneMessage(pane, "加载失败");
      const retry = document.createElement("button");
      retry.className = "ghost";
      retry.textContent = "重试";
      retry.addEventListener("click", show);
      pane.appendChild(retry);
      infoEl.textContent = err.message;
    }
  }

  function close() {
    modal.hidden = true;
    releaseUrl();
    window.removeEventListener("keydown", onKey);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    if (onClose) onClose();
  }

  function onKey(e) {
    if (modal.hidden) return;
    if (e.key === "Escape") close();
    if (e.key === "ArrowLeft" && index > 0) { index -= 1; show(); }
    if (e.key === "ArrowRight" && index < items.length - 1) { index += 1; show(); }
  }

  closeBtn.addEventListener("click", close);
  modal.addEventListener("click", (e) => e.target === modal && close());
  prevBtn.addEventListener("click", () => { if (index > 0) { index -= 1; show(); } });
  nextBtn.addEventListener("click", () => {
    if (index < items.length - 1) { index += 1; show(); }
  });
  zoomBtn.addEventListener("click", () => {
    zoom = zoom === "fit" ? "raw" : "fit";
    zoomBtn.textContent = zoom === "fit" ? "原始尺寸" : "适应窗口";
    const img = pane.querySelector("img");
    if (img) img.className = zoom === "fit" ? "img-fit" : "img-raw";
    pane.classList.toggle("scrollable", zoom === "raw");
  });
  downloadBtn.addEventListener("click", async () => {
    const item = items[index];
    if (!item) return;
    try {
      await download(
        "image/download",
        { category: item.category, name: item.name },
        item.name,
      );
    } catch (err) {
      toast(`下载失败：${err.message}`, { type: "error" });
    }
  });

  return {
    open(list, startIndex) {
      items = list;
      index = startIndex;
      lastFocus = document.activeElement;
      modal.hidden = false;
      closeBtn.focus();
      window.addEventListener("keydown", onKey);
      show();
    },
    close,
  };
}
