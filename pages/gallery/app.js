// 表情包管理页主控：分类栏、图片列表、搜索分页、批量选择与分类/同步管理。

import { apiGet, apiPost, toast, confirmDialog } from "./api.js";
import { createGalleryViewer } from "./preview.js";
import { setupUploads } from "./uploads.js";

const bridge = window.AstrBotPluginPage;
const PAGE_SIZE = 48;

const $ = (id) => document.getElementById(id);

const els = {
  search: $("search"),
  addBtn: $("add-btn"),
  fileInput: $("file-input"),
  sidebar: $("sidebar"),
  sidebarToggle: $("sidebar-toggle"),
  sidebarBackdrop: $("sidebar-backdrop"),
  categoryList: $("category-list"),
  newCategoryBtn: $("new-category-btn"),
  scope: $("current-scope"),
  scopeDesc: $("scope-desc"),
  selectModeBtn: $("select-mode-btn"),
  categoryMenuWrap: $("category-menu-wrap"),
  categoryEditBtn: $("category-edit-btn"),
  categoryMoreBtn: $("category-more-btn"),
  status: $("status"),
  grid: $("grid"),
  pager: $("pager"),
  batchBar: $("batch-bar"),
  batchCount: $("batch-count"),
  batchMove: $("batch-move"),
  batchCopy: $("batch-copy"),
  batchDelete: $("batch-delete"),
  batchCancel: $("batch-cancel"),
  syncStatus: $("sync-status"),
  syncUpload: $("sync-upload"),
  syncDownload: $("sync-download"),
  syncOverwriteRemote: $("sync-overwrite-remote"),
  syncOverwriteLocal: $("sync-overwrite-local"),
  clearAllBtn: $("clear-all-btn"),
  categoryModal: $("category-modal"),
  categoryModalTitle: $("category-modal-title"),
  categoryNameInput: $("category-name-input"),
  categoryDescInput: $("category-desc-input"),
  categoryOk: $("category-ok"),
  categoryCancel: $("category-cancel"),
  targetModal: $("target-modal"),
  targetTitle: $("target-title"),
  targetList: $("target-list"),
  targetCancel: $("target-cancel"),
};

const state = {
  category: "", // 空 = 全部图片
  q: "",
  items: [],
  total: 0,
  categories: [],
  selectMode: false,
  selected: new Map(), // key: "category/name" -> item
};

let listReqSeq = 0; // 列表请求序号：丢弃过时响应，防止切分类串图
let loadingMore = false; // 追加分页进行中，防止重复点击漏页
let loadingFirstPage = false;
let imageHostConfigured = false;
let syncPolling = false;
const thumbCache = new Map(); // 缩略图 dataURL 缓存：category/name/mtime -> src

const viewer = createGalleryViewer();

// ---------- 数据加载 ----------

async function loadCategories() {
  const data = await apiGet("overview");
  state.categories = data.categories;
  imageHostConfigured = !!data.image_host_configured;
  if (!imageHostConfigured) {
    els.syncStatus.textContent = "图床未配置（可在插件设置中配置）";
    setSyncButtonsEnabled(false);
  }
  renderCategories(data.total);
}

function renderCategories(total) {
  els.categoryList.innerHTML = "";
  const all = document.createElement("li");
  all.innerHTML = `<span>全部图片</span><span>${total}</span>`;
  all.classList.toggle("active", state.category === "");
  all.addEventListener("click", () => selectCategory(""));
  els.categoryList.appendChild(all);
  for (const cat of state.categories) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = cat.name;
    const count = document.createElement("span");
    count.textContent = cat.count;
    li.appendChild(name);
    li.appendChild(count);
    li.title = cat.description || "";
    li.classList.toggle("active", state.category === cat.name);
    li.addEventListener("click", () => selectCategory(cat.name));
    els.categoryList.appendChild(li);
  }
}

function selectCategory(category) {
  state.category = category;
  state.items = [];
  state.selected.clear();
  updateBatchBar();
  closeSidebarMobile();
  refreshHeader();
  renderCategories(state.categories.reduce((s, c) => s + c.count, 0));
  loadImages();
}

function refreshHeader() {
  const cat = state.categories.find((c) => c.name === state.category);
  els.scope.textContent = state.category ? `当前分类：${state.category}` : "全部图片";
  els.scopeDesc.textContent = cat && cat.description ? `使用说明：${cat.description}` : "";
  els.categoryMenuWrap.hidden = !state.category;
}

async function loadImages({ append = false } = {}) {
  if (append && (loadingMore || loadingFirstPage || !state.items.length)) return false;
  const seq = ++listReqSeq;
  if (append) {
    loadingMore = true;
  } else {
    loadingFirstPage = true;
    state.items = [];
    els.grid.innerHTML = "";
    els.pager.innerHTML = "";
  }
  els.status.textContent = "加载中……";
  const params = {
    offset: append ? state.items.length : 0, // 按已确认的唯一数据推进 offset
    limit: PAGE_SIZE,
  };
  if (state.category) params.category = state.category;
  if (state.q) params.q = state.q;
  try {
    const data = await apiGet("images", params);
    if (seq !== listReqSeq) return false; // 已有更新的请求，丢弃本次响应
    state.total = data.total;
    state.items = append ? state.items.concat(data.items) : data.items;
    renderGrid();
    renderPager();
    els.status.textContent =
      state.total === 0
        ? "这个范围还没有表情，点右上角「添加图片」或直接把图片拖进来"
        : `共 ${state.total} 张，已加载 ${state.items.length} 张` +
          (state.q ? `（搜索范围：${state.category || "全部图片"}）` : "");
    return true;
  } catch (err) {
    if (seq !== listReqSeq) return false;
    els.status.textContent = "";
    if (err.authExpired) {
      showFatal(err.message);
      return false;
    }
    els.grid.innerHTML = "";
    const div = document.createElement("div");
    div.className = "error-state";
    div.textContent = `加载失败：${err.message} `;
    const retry = document.createElement("button");
    retry.className = "ghost";
    retry.textContent = "重试";
    retry.addEventListener("click", () => loadImages());
    div.appendChild(retry);
    els.grid.appendChild(div);
    return false;
  } finally {
    if (seq === listReqSeq) {
      loadingMore = false;
      loadingFirstPage = false;
    }
  }
}

function renderPager() {
  els.pager.innerHTML = "";
  if (state.items.length < state.total) {
    const more = document.createElement("button");
    more.className = "ghost";
    more.textContent = `加载更多（剩余 ${state.total - state.items.length} 张）`;
    more.addEventListener("click", () => loadImages({ append: true }));
    els.pager.appendChild(more);
  }
}

// ---------- 网格与选择 ----------

function itemKey(item) {
  return `${item.category}/${item.name}`;
}

function renderGrid() {
  els.grid.innerHTML = "";
  for (const item of state.items) {
    const cell = document.createElement("div");
    cell.className = "cell";
    if (state.selected.has(itemKey(item))) cell.classList.add("selected");

    const wrap = document.createElement("div");
    wrap.className = "thumb-wrap";
    const img = document.createElement("img");
    img.className = "thumb";
    img.alt = item.name;
    img.loading = "lazy";
    wrap.appendChild(img);

    if (item.is_animated) {
      const badge = document.createElement("span");
      badge.className = "anim-badge";
      badge.textContent = "动图";
      wrap.appendChild(badge);
    }

    const check = document.createElement("button");
    check.className = "check-btn";
    check.type = "button";
    check.setAttribute("aria-label", "选择");
    check.textContent = state.selected.has(itemKey(item)) ? "✓" : "";
    check.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleSelect(item);
    });
    wrap.appendChild(check);

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = state.category ? item.name : `${item.category}/${item.name}`;
    name.title = name.textContent;

    cell.appendChild(wrap);
    cell.appendChild(name);
    cell.addEventListener("click", () => {
      if (state.selectMode) {
        toggleSelect(item);
      } else {
        const idx = state.items.findIndex((i) => itemKey(i) === itemKey(item));
        viewer.open(state.items, idx);
      }
    });
    els.grid.appendChild(cell);

    const thumbKey = `${item.category}/${item.name}/${item.mtime}`;
    const cached = thumbCache.get(thumbKey);
    if (cached) {
      img.src = cached;
    } else {
      apiGet("thumb", { category: item.category, name: item.name })
        .then((t) => {
          const src = `data:image/png;base64,${t.content}`;
          thumbCache.set(thumbKey, src);
          img.src = src;
        })
        .catch(() => {
          img.alt = "加载失败";
          img.classList.add("thumb-broken");
        });
    }
  }
}

function toggleSelect(item) {
  const key = itemKey(item);
  if (state.selected.has(key)) {
    state.selected.delete(key);
  } else {
    state.selected.set(key, item);
  }
  // 只更新选中样式和计数，不整格重绘（缩略图走缓存，不重发请求）
  const idx = state.items.findIndex((i) => itemKey(i) === key);
  const cell = els.grid.children[idx];
  if (cell) {
    const selected = state.selected.has(key);
    cell.classList.toggle("selected", selected);
    const check = cell.querySelector(".check-btn");
    if (check) check.textContent = selected ? "✓" : "";
  }
  updateBatchBar();
}

function updateBatchBar() {
  const n = state.selected.size;
  els.batchBar.hidden = !(state.selectMode || n > 0);
  els.batchCount.textContent = `已选 ${n} 张`;
  els.batchMove.disabled = n === 0;
  els.batchCopy.disabled = n === 0;
  els.batchDelete.disabled = n === 0;
  els.selectModeBtn.textContent = state.selectMode ? "退出选择" : "选择";
  els.selectModeBtn.classList.toggle("primary", state.selectMode);
}

// ---------- 批量操作 ----------

function selectedItems() {
  return [...state.selected.values()].map((i) => ({
    category: i.category,
    name: i.name,
  }));
}

async function runBatch(op, extra = {}) {
  const items = selectedItems();
  if (!items.length) return;
  try {
    const result = await apiPost(`images/${op}`, { items, ...extra });
    const doneKey = op === "delete" ? "deleted" : op === "move" ? "moved" : "copied";
    const doneList = result[doneKey] || [];
    const failedList = [
      ...(result.missing || []),
      ...(result.conflicting || []),
      ...(result.rejected || []),
    ];
    // 只从选择中移除已成功项，冲突/失败项保留供重试
    for (const i of doneList) {
      state.selected.delete(`${i.category}/${i.name}`);
    }
    if (failedList.length) {
      const names = failedList.map((i) => `${i.category}/${i.name}`).join("、");
      toast(
        `完成 ${doneList.length} 项，${failedList.length} 项未处理（同名冲突或不存在），已保留选择：${names}`,
        { type: "error", duration: 6000 },
      );
    } else {
      toast(`已完成 ${doneList.length} 项`, { type: "success" });
    }
    if (state.selected.size === 0) state.selectMode = false;
    updateBatchBar();
    await refreshAll({ preserveSelection: true });
  } catch (err) {
    if (err.authExpired) return showFatal(err.message);
    console.error("batch error stack:", err.stack);
    toast(`操作失败：${err.message}`, { type: "error" });
  }
}

function openTargetModal(title, onPick) {
  els.targetTitle.textContent = title;
  els.targetList.innerHTML = "";
  for (const cat of state.categories) {
    if (cat.name === state.category) continue;
    const li = document.createElement("li");
    li.textContent = `${cat.name}（${cat.count}）`;
    li.addEventListener("click", () => {
      els.targetModal.hidden = true;
      onPick(cat.name);
    });
    els.targetList.appendChild(li);
  }
  els.targetModal.hidden = false;
}

// ---------- 分类管理 ----------

function openCategoryModal({ title, name = "", description = "", nameReadonly = false, onSave }) {
  els.categoryModalTitle.textContent = title;
  els.categoryNameInput.value = name;
  els.categoryNameInput.readOnly = nameReadonly;
  els.categoryDescInput.value = description;
  els.categoryModal.hidden = false;
  (nameReadonly ? els.categoryDescInput : els.categoryNameInput).focus();

  const cleanup = () => {
    els.categoryModal.hidden = true;
    els.categoryOk.onclick = null;
  };
  els.categoryOk.onclick = async () => {
    const newName = els.categoryNameInput.value.trim();
    const desc = els.categoryDescInput.value.trim();
    if (!newName) {
      toast("分类名不能为空", { type: "error" });
      return;
    }
    try {
      await onSave(newName, desc);
      cleanup();
      await refreshAll();
    } catch (err) {
      if (err.authExpired) return showFatal(err.message);
      toast(`保存失败：${err.message}`, { type: "error" });
    }
  };
  els.categoryCancel.onclick = cleanup;
}

async function refreshAll({ preserveSelection = false } = {}) {
  if (!preserveSelection) state.selected.clear();
  await loadCategories();
  refreshHeader();
  thumbCache.clear(); // 图库已变更，缩略图按新数据重新取
  const loaded = await loadImages();
  updateBatchBar();
  if (!loaded) throw new Error("图库列表刷新失败，请刷新页面查看");
}

// ---------- 同步与高级操作 ----------

async function loadSyncStatus() {
  if (!imageHostConfigured) {
    els.syncStatus.textContent = "图床未配置（可在插件设置中配置）";
    setSyncButtonsEnabled(false);
    return false;
  }
  try {
    const s = await apiGet("sync/status");
    if (!s.configured) {
      els.syncStatus.textContent = "图床未配置（可在插件设置中配置）";
      setSyncButtonsEnabled(false);
      return false;
    }
    els.syncStatus.textContent =
      `图床：${s.provider} · 待上传 ${s.to_upload_count} · 待下载 ${s.to_download_count}`;
    setSyncButtonsEnabled(!syncPolling);
    return true;
  } catch (err) {
    els.syncStatus.textContent = `同步状态获取失败：${err.message}（收起再展开可重试）`;
    setSyncButtonsEnabled(false);
    return false;
  }
}

function setSyncButtonsEnabled(enabled) {
  for (const btn of [
    els.syncUpload,
    els.syncDownload,
    els.syncOverwriteRemote,
    els.syncOverwriteLocal,
  ]) {
    btn.disabled = !enabled;
  }
}

async function startSync(direction, label, dangerInfo = null) {
  if (dangerInfo) {
    const ok = await confirmDialog({
      title: label,
      text: dangerInfo,
      okText: "确认执行",
      danger: true,
    });
    if (!ok) return;
  }
  try {
    await apiPost("sync/start", { direction });
    toast(`${label}已启动`, { type: "success" });
    pollSyncProcess(label);
  } catch (err) {
    if (err.authExpired) return showFatal(err.message);
    toast(`启动失败：${err.message}`, { type: "error" });
  }
}

async function pollSyncProcess(label) {
  if (syncPolling) return;
  syncPolling = true;
  let statusReady = false;
  setSyncButtonsEnabled(false);
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      const p = await apiGet("sync/process");
      if (!p.running) {
        if (p.finished) {
          if (p.success) {
            toast(`${label}完成：${p.message || ""}`, { type: "success" });
          } else {
            toast(`${label}失败：${p.message || "请查看日志"}`, { type: "error", duration: 6000 });
          }
        }
        await refreshAll();
        statusReady = await loadSyncStatus();
        return;
      }
    }
  } catch (err) {
    toast(`同步进度获取失败：${err.message}`, { type: "error" });
  } finally {
    syncPolling = false;
    setSyncButtonsEnabled(statusReady);
  }
}

// ---------- 其他 ----------

function showFatal(message) {
  els.grid.innerHTML = "";
  els.status.textContent = "";
  const div = document.createElement("div");
  div.className = "error-state";
  div.textContent = message;
  els.grid.appendChild(div);
}

function closeSidebarMobile() {
  els.sidebar.classList.remove("open");
  els.sidebarBackdrop.hidden = true;
}

async function onLibraryChanged() {
  await refreshAll();
}

// ---------- 事件接线 ----------

function wireEvents(uploads) {
  els.addBtn.addEventListener("click", () => els.fileInput.click());
  // fileInput 的 change 由 uploads.js 处理
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const openModals = [...document.querySelectorAll(".modal:not([hidden])")];
    const modal = openModals.at(-1);
    if (!modal) return;
    const focusable = [...modal.querySelectorAll(
      "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]"
    )].filter((node) => node.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!modal.contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  let searchTimer = null;
  els.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = els.search.value.trim();
      state.selected.clear();
      updateBatchBar();
      loadImages();
    }, 300);
  });

  els.selectModeBtn.addEventListener("click", () => {
    state.selectMode = !state.selectMode;
    if (!state.selectMode) state.selected.clear();
    renderGrid();
    updateBatchBar();
  });

  els.batchCancel.addEventListener("click", () => {
    state.selected.clear();
    state.selectMode = false;
    renderGrid();
    updateBatchBar();
  });

  // 全选 = 当前已加载的列表，不暗中选中未加载页
  $("batch-select-all").addEventListener("click", () => {
    for (const item of state.items) {
      state.selected.set(itemKey(item), item);
    }
    state.selectMode = true;
    renderGrid();
    updateBatchBar();
    toast(`已选中当前已加载的 ${state.items.length} 张`, { duration: 2000 });
  });

  els.batchDelete.addEventListener("click", async () => {
    const n = state.selected.size;
    const ok = await confirmDialog({
      title: "删除所选图片",
      text: `将删除已选的 ${n} 张图片，此操作不可恢复。确定吗？`,
      okText: "删除",
      danger: true,
    });
    if (ok) runBatch("delete");
  });

  els.batchMove.addEventListener("click", () => {
    openTargetModal(`移动 ${state.selected.size} 张图片到……`, (target) =>
      runBatch("move", { target_category: target }),
    );
  });

  els.batchCopy.addEventListener("click", () => {
    openTargetModal(`复制 ${state.selected.size} 张图片到……`, (target) =>
      runBatch("copy", { target_category: target }),
    );
  });

  els.targetCancel.addEventListener("click", () => {
    els.targetModal.hidden = true;
  });

  els.newCategoryBtn.addEventListener("click", () => {
    openCategoryModal({
      title: "新建分类",
      onSave: async (name, desc) => {
        await apiPost("categories", { name, description: desc || "请添加描述" });
        toast(`已创建分类「${name}」`, { type: "success" });
      },
    });
  });

  els.categoryEditBtn.addEventListener("click", () => {
    const cat = state.categories.find((c) => c.name === state.category);
    if (!cat) return;
    openCategoryModal({
      title: `编辑分类「${cat.name}」`,
      name: cat.name,
      description: cat.description,
      nameReadonly: true,
      onSave: async (_name, desc) => {
        await apiPost(`categories/${cat.name}/description`, { description: desc });
        toast("说明已保存", { type: "success" });
      },
    });
  });

  els.categoryMoreBtn.addEventListener("click", async () => {
    const cat = state.categories.find((c) => c.name === state.category);
    if (!cat) return;
    const action = await chooseCategoryAction(cat);
    if (!action) return;
    try {
      if (action === "rename") {
        openCategoryModal({
          title: `重命名分类「${cat.name}」（说明保持不变）`,
          name: cat.name,
          description: cat.description,
          onSave: async (newName) => {
            await apiPost(`categories/${cat.name}/rename`, { new_name: newName });
            state.category = newName;
            toast(`已重命名为「${newName}」`, { type: "success" });
          },
        });
      } else if (action === "clear") {
        const ok = await confirmDialog({
          title: "清空分类",
          text: `将删除分类「${cat.name}」下的全部 ${cat.count} 张图片，但保留分类和说明。确定吗？`,
          okText: "清空图片",
          danger: true,
        });
        if (!ok) return;
        await apiPost(`categories/${cat.name}/clear`);
        toast("已清空该分类的图片", { type: "success" });
        await refreshAll();
      } else if (action === "delete") {
        const ok = await confirmDialog({
          title: "删除分类",
          text:
            `将删除分类「${cat.name}」本身及其说明` +
            (cat.count ? `，同时删除其中的 ${cat.count} 张图片` : "") +
            "。此操作不可恢复。确定吗？",
          okText: "删除分类",
          danger: true,
        });
        if (!ok) return;
        await apiPost(`categories/${cat.name}/delete`);
        state.category = "";
        toast("已删除该分类", { type: "success" });
        await refreshAll();
      }
    } catch (err) {
      if (err.authExpired) return showFatal(err.message);
      toast(`操作失败：${err.message}`, { type: "error" });
    }
  });

  els.clearAllBtn.addEventListener("click", async () => {
    const total = state.categories.reduce((s, c) => s + c.count, 0);
    const ok = await confirmDialog({
      title: "清空全部图片",
      text: `将删除全部 ${total} 张图片（保留所有分类和说明）。此操作不可恢复，确定吗？`,
      okText: "清空全部",
      danger: true,
    });
    if (!ok) return;
    try {
      const result = await apiPost("categories/clear_all");
      toast(`已清空 ${result.deleted_count} 个文件`, { type: "success" });
      await refreshAll();
    } catch (err) {
      if (err.authExpired) return showFatal(err.message);
      toast(`操作失败：${err.message}`, { type: "error" });
    }
  });

  els.syncUpload.addEventListener("click", () => startSync("upload", "同步到云端"));
  els.syncDownload.addEventListener("click", () => startSync("download", "从云端同步"));
  els.syncOverwriteRemote.addEventListener("click", async () => {
    let info = "将让云端与本地完全一致。";
    try {
      const s = await apiGet("sync/status");
      if (s.configured) info = `将上传 ${s.to_upload_count} 个文件，并删除云端多出的 ${s.to_delete_remote_count} 个文件。`;
    } catch { /* 状态拿不到时也要给出确认 */ }
    startSync("overwrite_to_remote", "覆盖到云端", info);
  });
  els.syncOverwriteLocal.addEventListener("click", async () => {
    let info = "将让本地与云端完全一致。";
    try {
      const s = await apiGet("sync/status");
      if (s.configured) info = `将下载 ${s.to_download_count} 个文件，并删除本地多出的 ${s.to_delete_local_count} 个文件。`;
    } catch { /* 同上 */ }
    startSync("overwrite_from_remote", "从云端覆盖", info);
  });

  els.sidebarToggle.addEventListener("click", () => {
    els.sidebar.classList.add("open");
    els.sidebarBackdrop.hidden = false;
  });
  els.sidebarBackdrop.addEventListener("click", closeSidebarMobile);

  // 图床状态延迟到用户展开「高级操作」时才查询（进页面不自动访问图床）
  document.getElementById("advanced").addEventListener("toggle", (e) => {
    if (e.target.open) {
      loadSyncStatus();
      if (imageHostConfigured && !syncPolling) {
        apiGet("sync/process").then((p) => {
          if (p.running) pollSyncProcess("图床同步");
        }).catch(() => {});
      }
    }
  });
}

// 「更多」的下拉选择：用一个简单确认弹窗列出三个操作
function chooseCategoryAction(cat) {
  return new Promise((resolve) => {
    els.targetTitle.textContent = `分类「${cat.name}」操作`;
    els.targetList.innerHTML = "";
    const actions = [
      ["rename", "重命名分类"],
      ["clear", `清空分类图片（${cat.count} 张，保留分类和说明）`],
      ["delete", "删除分类本身（连同图片和说明）"],
    ];
    for (const [key, label] of actions) {
      const li = document.createElement("li");
      li.textContent = label;
      if (key === "delete") li.classList.add("danger-text");
      li.addEventListener("click", () => {
        els.targetModal.hidden = true;
        resolve(key);
      });
      els.targetList.appendChild(li);
    }
    els.targetModal.hidden = false;
    els.targetCancel.onclick = () => {
      els.targetModal.hidden = true;
      resolve(null);
    };
  });
}

// ---------- 初始化 ----------

(async () => {
  try {
    await bridge.ready();
    document.title = bridge.t("pages.gallery.title", "表情包管理");
    bridge.onContext(() => {
      document.title = bridge.t("pages.gallery.title", "表情包管理");
    });

    const uploads = setupUploads({
      getCurrentCategory: () => state.category,
      getCategories: () => state.categories,
      onChanged: onLibraryChanged,
    });
    wireEvents(uploads);

    await loadCategories();
    refreshHeader();
    await loadImages();
    // 图床状态不在初始化时查询，展开「高级操作」时才加载
  } catch (err) {
    showFatal(err && err.message ? err.message : "初始化失败，请刷新重试");
  }
})();
