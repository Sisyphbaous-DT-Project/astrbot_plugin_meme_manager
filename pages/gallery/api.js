// bridge 调用封装：统一错误处理与登录失效识别。
// bridge 对失败只传 Error(message)（HTTP status 丢失），这里靠消息识别鉴权失效。

export class ApiError extends Error {
  constructor(message, { authExpired = false } = {}) {
    super(message);
    this.authExpired = authExpired;
  }
}

function normalizeError(err) {
  const message = err && err.message ? err.message : String(err);
  // Dashboard 未登录/Token 失效时的典型消息
  if (/未授权|Token 过期|Token 无效|Unauthorized/i.test(message)) {
    return new ApiError("登录已失效，请刷新页面重新登录 AstrBot 面板", {
      authExpired: true,
    });
  }
  if (/Failed to fetch|NetworkError|网络/i.test(message)) {
    return new ApiError("网络请求失败，请检查连接后重试");
  }
  return new ApiError(message);
}

export async function apiGet(endpoint, params) {
  try {
    return await window.AstrBotPluginPage.apiGet(endpoint, params);
  } catch (err) {
    throw normalizeError(err);
  }
}

export async function apiPost(endpoint, body) {
  try {
    return await window.AstrBotPluginPage.apiPost(endpoint, body);
  } catch (err) {
    throw normalizeError(err);
  }
}

export async function upload(endpoint, file) {
  try {
    return await window.AstrBotPluginPage.upload(endpoint, file);
  } catch (err) {
    throw normalizeError(err);
  }
}

export async function download(endpoint, params, filename) {
  try {
    return await window.AstrBotPluginPage.download(endpoint, params, filename);
  } catch (err) {
    throw normalizeError(err);
  }
}

export function toast(message, { type = "info", duration = 3000 } = {}) {
  const root = document.getElementById("toast-root");
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => {
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), 300);
  }, duration);
}

// 通用页面内确认弹窗（不用原生 confirm）
export function confirmDialog({ title, text, okText = "确认", danger = false }) {
  return new Promise((resolve) => {
    const modal = document.getElementById("confirm-modal");
    const titleEl = document.getElementById("confirm-title");
    const textEl = document.getElementById("confirm-text");
    const okBtn = document.getElementById("confirm-ok");
    const cancelBtn = document.getElementById("confirm-cancel");

    titleEl.textContent = title;
    textEl.textContent = text;
    okBtn.textContent = okText;
    okBtn.classList.toggle("danger-solid", danger);
    modal.hidden = false;
    okBtn.focus();

    const done = (value) => {
      modal.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      window.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onBackdrop = (e) => e.target === modal && done(false);
    const onKey = (e) => e.key === "Escape" && done(false);

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    window.addEventListener("keydown", onKey);
  });
}
