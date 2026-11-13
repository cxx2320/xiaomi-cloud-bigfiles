/* 小米云盘大文件分析 —— 前端逻辑 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const ui = {
    loginCard: $("login-card"),
    main: $("main"),
    cookie: $("cookie"),
    passToken: $("pass-token"),
    btnLogin: $("btn-login"),
    loginMsg: $("login-msg"),
    userChip: $("user-chip"),
    btnLogout: $("btn-logout"),

    quotaText: $("quota-text"),
    quotaBar: $("quota-bar"),
    scanMsg: $("scan-msg"),
    btnScan: $("btn-scan"),
    btnExport: $("btn-export"),

    progress: $("progress"),
    progressText: $("progress-text"),
    progressBar: $("progress-bar"),
    progressErrors: $("progress-errors"),

    statCount: $("stat-count"),
    statSize: $("stat-size"),
    statLargest: $("stat-largest"),
    statTime: $("stat-time"),

    listDirs: $("list-dirs"),
    listTypes: $("list-types"),
    listServices: $("list-services"),

    fMinSize: $("f-min-size"),
    fType: $("f-type"),
    fSource: $("f-source"),
    fQ: $("f-q"),
    fLimit: $("f-limit"),
    btnReset: $("btn-reset"),

    tableInfo: $("table-info"),
    tbody: $("tbody"),
    pageInfo: $("page-info"),
    btnPrev: $("btn-prev"),
    btnNext: $("btn-next"),

    modal: $("preview-modal"),
    pvTitle: $("pv-title"),
    pvImg: $("pv-img"),
    pvEmpty: $("pv-empty"),
    pvMeta: $("pv-meta"),
    pvClose: $("pv-close"),
  };

  const state = {
    loggedIn: false,
    sort: "size",
    order: "desc",
    offset: 0,
    limit: 100,
    minSize: 0,
    type: "",
    source: "",
    q: "",
    total: 0,
    largest: 0,
    scanned: false,
    items: [],
    typeLabels: {},
    timer: null,
    loading: false,
  };

  /* ------------------------------ 工具 ------------------------------ */
  function humanSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + " B";
    const units = ["KB", "MB", "GB", "TB", "PB"];
    let value = n / 1024;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value /= 1024;
      i += 1;
    }
    return value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2) + " " + units[i];
  }

  function fmtTime(ms) {
    if (!ms) return "—";
    let n = Number(ms);
    if (Number.isNaN(n) || n <= 0) return "—";
    if (n < 1e11) n *= 1000; // 秒级时间戳转毫秒
    const d = new Date(n);
    if (Number.isNaN(d.getTime())) return "—";
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function extOf(name) {
    const idx = (name || "").lastIndexOf(".");
    if (idx <= 0 || idx === name.length - 1) return "FILE";
    return name.slice(idx + 1).slice(0, 4);
  }

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options));
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  }

  function setMsg(el, text, kind) {
    if (!el) return;
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }

  /* ------------------------------ 登录 ------------------------------ */
  async function doLogin() {
    const cookie = ui.cookie.value.trim();
    if (!cookie) {
      setMsg(ui.loginMsg, "请先粘贴 Cookie", "error");
      return;
    }
    ui.btnLogin.disabled = true;
    setMsg(ui.loginMsg, "正在验证…");
    try {
      const data = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ cookie, pass_token: ui.passToken.value.trim() }),
      });
      setMsg(ui.loginMsg, "");
      ui.cookie.value = "";
      ui.passToken.value = "";
      await refreshStatus();
      setMsg(ui.scanMsg, data.user ? `已连接：${data.user}` : "", "ok");
    } catch (err) {
      setMsg(ui.loginMsg, err.message, "error");
    } finally {
      ui.btnLogin.disabled = false;
    }
  }

  async function doLogout() {
    if (!confirm("退出并清除本机保存的 Cookie？")) return;
    stopPolling();
    try {
      await api("/api/logout", { method: "POST" });
    } catch (e) {
      /* 忽略 */
    }
    state.loggedIn = false;
    ui.main.classList.add("hidden");
    ui.loginCard.classList.remove("hidden");
    ui.userChip.classList.add("hidden");
    ui.btnLogout.classList.add("hidden");
  }

  /* ------------------------------ 状态 ------------------------------ */
  function showMain(loggedIn, user, quotaOk) {
    state.loggedIn = loggedIn;
    ui.main.classList.toggle("hidden", !loggedIn);
    ui.btnLogout.classList.toggle("hidden", !loggedIn);
    ui.userChip.classList.toggle("hidden", !loggedIn || !user);
    if (user) ui.userChip.textContent = quotaOk ? user : user + "（令牌过期）";

    const needLogin = !loggedIn || !quotaOk;
    ui.loginCard.classList.toggle("hidden", !needLogin);
    if (loggedIn && !quotaOk) {
      setMsg(
        ui.loginMsg,
        "登录已过期：请重新粘贴 Cookie。建议同时填写 passToken，服务运行期间会自动续期，不会频繁掉线。",
        "error"
      );
    }
  }

  async function refreshStatus() {
    let data;
    try {
      data = await api("/api/status");
    } catch (err) {
      setMsg(ui.loginMsg, err.message, "error");
      return;
    }
    showMain(data.logged_in, data.user, Boolean(data.quota));

    if (data.quota && data.quota.total > 0) {
      const pct = Math.min(100, (data.quota.used / data.quota.total) * 100);
      ui.quotaText.textContent = `${humanSize(data.quota.used)} / ${humanSize(data.quota.total)}（${pct.toFixed(1)}%）`;
      ui.quotaBar.style.width = pct + "%";
    } else {
      ui.quotaText.textContent = data.logged_in ? "—（令牌已过期）" : "—";
      ui.quotaBar.style.width = "0";
    }
    renderServices(data.quota);

    const cache = data.cache || {};
    state.scanned = Boolean(cache.scanned_at);
    if (cache.count) {
      ui.statCount.textContent = cache.count.toLocaleString();
      ui.statSize.textContent = humanSize(cache.size);
      ui.statTime.textContent = fmtTime(cache.scanned_at);
      ui.btnExport.disabled = false;
    } else {
      ui.btnExport.disabled = true;
    }

    if (data.scan && data.scan.running) {
      ui.progress.classList.remove("hidden");
      ui.btnScan.disabled = true;
      updateProgress(data.scan);
      startPolling();
    } else {
      ui.progress.classList.add("hidden");
      ui.btnScan.disabled = false;
    }
    if (data.scan && data.scan.error) {
      setMsg(ui.scanMsg, "扫描出错：" + data.scan.error, "error");
    }

    if (data.logged_in) {
      await loadSummary();
      await loadFiles();
    }
  }

  /* ------------------------------ 扫描 ------------------------------ */
  function updateProgress(p) {
    ui.progressText.textContent = p.running
      ? `已扫描 ${p.dirs_done} 个目录 · 相册 ${p.gallery_pages || 0} 页 · 发现 ${Number(p.files_found).toLocaleString()} 个文件 · 累计 ${humanSize(p.bytes_found)}`
      : `扫描结束：${Number(p.files_found).toLocaleString()} 个文件，共 ${humanSize(p.bytes_found)}`;
    if (!p.running) {
      ui.progressBar.classList.remove("indeterminate");
      ui.progressBar.style.width = "100%";
    }
    if (p.errors && p.errors.length) {
      ui.progressErrors.textContent = "部分目录读取失败：" + p.errors.join(" ｜ ");
    } else {
      ui.progressErrors.textContent = "";
    }
  }

  async function startScan() {
    if (!state.loggedIn) return;
    ui.btnScan.disabled = true;
    ui.progress.classList.remove("hidden");
    ui.progressBar.classList.add("indeterminate");
    setMsg(ui.scanMsg, "扫描中…");
    try {
      await api("/api/scan", { method: "POST", body: JSON.stringify({ workers: 8 }) });
      startPolling();
    } catch (err) {
      setMsg(ui.scanMsg, err.message, "error");
      ui.btnScan.disabled = false;
      ui.progress.classList.add("hidden");
    }
  }

  function startPolling() {
    stopPolling();
    state.timer = setInterval(pollScan, 1200);
  }

  function stopPolling() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }

  async function pollScan() {
    try {
      const p = await api("/api/scan");
      updateProgress(p);
      if (!p.running) {
        stopPolling();
        ui.btnScan.disabled = false;
        ui.progressBar.classList.remove("indeterminate");
        ui.progressBar.style.width = "0";
        ui.progress.classList.add("hidden");
        setMsg(
          ui.scanMsg,
          p.error ? "扫描失败：" + p.error : `扫描完成：${Number(p.files_found).toLocaleString()} 个文件`,
          p.error ? "error" : "ok"
        );
        state.offset = 0;
        await refreshStatus();
      }
    } catch (err) {
      stopPolling();
      ui.btnScan.disabled = false;
    }
  }

  /* ------------------------------ 汇总 ------------------------------ */
  async function loadSummary() {
    let data;
    try {
      data = await api("/api/summary");
    } catch (e) {
      return;
    }
    const s = data.summary || {};
    state.typeLabels = data.type_labels || {};
    state.largest = s.largest || 0;

    ui.statCount.textContent = Number(s.file_count || 0).toLocaleString();
    ui.statSize.textContent = humanSize(s.total_size || 0);
    ui.statLargest.textContent = humanSize(s.largest || 0);
    ui.statTime.textContent = fmtTime(data.scanned_at);

    // 目录
    ui.listDirs.innerHTML = "";
    const dirs = s.top_dirs || [];
    if (!dirs.length) {
      ui.listDirs.innerHTML = '<li class="muted">还没有扫描结果</li>';
    } else {
      const max = Math.max(...dirs.map((d) => d.size), 1);
      dirs.forEach((d) => {
        const li = document.createElement("li");
        li.innerHTML = `
          <div class="rank-top">
            <span class="rank-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</span>
            <span class="rank-size">${humanSize(d.size)} · ${d.count} 个</span>
          </div>
          <div class="rank-bar"><i style="width:${((d.size / max) * 100).toFixed(1)}%"></i></div>`;
        ui.listDirs.appendChild(li);
      });
    }

    // 类型
    ui.listTypes.innerHTML = "";
    const types = s.by_type || [];
    if (!types.length) {
      ui.listTypes.innerHTML = '<li class="muted">还没有扫描结果</li>';
    } else {
      const max = Math.max(...types.map((t) => t.size), 1);
      types.forEach((t) => {
        const li = document.createElement("li");
        li.innerHTML = `
          <div class="rank-top">
            <span class="rank-name">${escapeHtml(t.label || t.type)}</span>
            <span class="rank-size">${humanSize(t.size)} · ${t.count} 个</span>
          </div>
          <div class="rank-bar"><i style="width:${((t.size / max) * 100).toFixed(1)}%"></i></div>`;
        ui.listTypes.appendChild(li);
      });
    }
  }

  /** 云服务占用明细：相册 / 桌面图标布局 / 录音备份 等 */
  function renderServices(quota) {
    const el = ui.listServices;
    el.innerHTML = "";
    const items = (quota && quota.detail) || [];
    if (!items.length) {
      el.innerHTML = '<li class="muted">暂无数据（登录后显示）</li>';
      return;
    }
    const used = (quota && quota.used) || Math.max(...items.map((i) => i.size), 1);
    const max = Math.max(...items.map((i) => i.size), 1);
    items.forEach((it) => {
      const li = document.createElement("li");
      const pct = used ? ((it.size / used) * 100).toFixed(1) : "0.0";
      li.innerHTML = `
        <div class="rank-top">
          <span class="rank-name">${escapeHtml(it.label || it.key)}</span>
          <span class="rank-size">${humanSize(it.size)} · ${pct}%</span>
        </div>
        <div class="rank-bar"><i style="width:${((it.size / max) * 100).toFixed(1)}%"></i></div>`;
      el.appendChild(li);
    });
  }

  /** 预览弹窗：相册的缩略图是带签名的直链，可直接用 img 加载 */
  function openPreview(item) {
    ui.pvTitle.textContent = item.name || "";
    ui.pvMeta.textContent = [item.path, humanSize(item.size), fmtTime(item.createTime)]
      .filter(Boolean)
      .join("　·　");

    if (item.thumb) {
      ui.pvImg.src = item.thumb;
      ui.pvImg.classList.remove("hidden");
      ui.pvEmpty.classList.add("hidden");
    } else {
      ui.pvImg.removeAttribute("src");
      ui.pvImg.classList.add("hidden");
      ui.pvEmpty.textContent = "该文件暂无预览图（云盘文件暂不支持预览）";
      ui.pvEmpty.classList.remove("hidden");
    }
    ui.modal.classList.remove("hidden");
  }

  function closePreview() {
    ui.modal.classList.add("hidden");
    ui.pvImg.removeAttribute("src");
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /* ------------------------------ 文件表 ------------------------------ */
  async function loadFiles() {
    if (!state.loggedIn) return;
    if (state.loading) return;
    state.loading = true;
    const params = new URLSearchParams({
      sort: state.sort,
      order: state.order,
      limit: String(state.limit),
      offset: String(state.offset),
      min_size: String(state.minSize),
      type: state.type,
      source: state.source,
      q: state.q,
    });

    try {
      const data = await api("/api/files?" + params.toString());
      state.total = data.total || 0;
      renderRows(data.items || []);
      renderPager(data);
      updateSortHeaders();
    } catch (err) {
      ui.tbody.innerHTML = `<tr><td colspan="7" class="empty">${escapeHtml(err.message)}</td></tr>`;
    } finally {
      state.loading = false;
    }
  }

  function renderRows(items) {
    if (!items.length) {
      const filtering = state.minSize > 0 || state.type || state.source || state.q;
      const text = state.scanned
        ? filtering
          ? "没有匹配的文件，试试调整筛选条件"
          : "云盘和相册里都没有扫描到文件"
        : "没有匹配的文件，先点击「开始扫描全盘」";
      ui.tbody.innerHTML = `<tr><td colspan="7" class="empty">${text}</td></tr>`;
      return;
    }
    const max = Math.max(...items.map((f) => f.size || 0), 1);
    const labels = state.typeLabels || {};
    state.items = items;
    const rows = items.map((f, idx) => {
      const width = Math.max(2, ((f.size || 0) / max) * 100);
      const canPreview = Boolean(f.thumb) || f.type === "pic" || f.type === "video";
      return `<tr>
        <td><div class="cell-name">
          <span class="ext-badge">${escapeHtml(extOf(f.name))}</span>
          <span class="name-text" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
        </div></td>
        <td>${canPreview ? `<button class="preview-btn" data-idx="${idx}">预览</button>` : '<span class="muted">—</span>'}</td>
        <td><div class="path-text" title="${escapeHtml(f.path)}">${escapeHtml(f.dir || "/")}</div></td>
        <td class="num"><div class="size-cell">
          <span class="size-bar"><i style="width:${width.toFixed(1)}%"></i></span>
          <span>${humanSize(f.size)}</span>
        </div></td>
        <td>${fmtTime(f.createTime)}</td>
        <td>${fmtTime(f.modifyTime)}</td>
        <td>${escapeHtml(labels[f.type] || f.type || "其他")}</td>
      </tr>`;
    });
    ui.tbody.innerHTML = rows.join("");
  }

  function renderPager(data) {
    const total = data.total || 0;
    const start = total === 0 ? 0 : data.offset + 1;
    const end = Math.min(data.offset + data.limit, total);
    ui.tableInfo.textContent = `共 ${total.toLocaleString()} 个文件 · 合计 ${humanSize(data.size_sum || 0)}`;
    ui.pageInfo.textContent = total === 0 ? "第 0 / 0 页" : `第 ${start}-${end} 条，共 ${total.toLocaleString()} 条`;
    ui.btnPrev.disabled = data.offset <= 0;
    ui.btnNext.disabled = end >= total;
  }

  function updateSortHeaders() {
    document.querySelectorAll("th.sortable").forEach((th) => {
      const key = th.dataset.sort;
      const active = key === state.sort;
      th.dataset.active = active ? "true" : "false";
      const arrow = active ? (state.order === "desc" ? " ▼" : " ▲") : "";
      const label = th.dataset.label || th.textContent.replace(/[▲▼]\s*$/, "").trim();
      th.dataset.label = label;
      th.textContent = label + arrow;
    });
  }

  function onSortClick(e) {
    const th = e.currentTarget;
    const key = th.dataset.sort;
    if (!key) return;
    if (state.sort === key) {
      state.order = state.order === "desc" ? "asc" : "desc";
    } else {
      state.sort = key;
      state.order = key === "name" ? "asc" : "desc";
    }
    state.offset = 0;
    loadFiles();
  }

  /* ------------------------------ 事件绑定 ------------------------------ */
  ui.btnLogin.addEventListener("click", doLogin);
  ui.btnLogout.addEventListener("click", doLogout);
  ui.btnScan.addEventListener("click", startScan);
  ui.btnExport.addEventListener("click", () => {
    window.location.href = "/api/export";
  });

  ui.fMinSize.addEventListener("change", () => {
    state.minSize = Number(ui.fMinSize.value) || 0;
    state.offset = 0;
    loadFiles();
  });
  ui.fType.addEventListener("change", () => {
    state.type = ui.fType.value;
    state.offset = 0;
    loadFiles();
  });
  ui.fSource.addEventListener("change", () => {
    state.source = ui.fSource.value;
    state.offset = 0;
    loadFiles();
  });
  ui.fLimit.addEventListener("change", () => {
    state.limit = Number(ui.fLimit.value) || 100;
    state.offset = 0;
    loadFiles();
  });

  let searchTimer = null;
  ui.fQ.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = ui.fQ.value.trim();
      state.offset = 0;
      loadFiles();
    }, 300);
  });

  ui.btnReset.addEventListener("click", () => {
    ui.fMinSize.value = "0";
    ui.fType.value = "";
    ui.fSource.value = "";
    ui.fQ.value = "";
    state.minSize = 0;
    state.type = "";
    state.source = "";
    state.q = "";
    state.offset = 0;
    loadFiles();
  });

  ui.btnPrev.addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadFiles();
  });

  ui.btnNext.addEventListener("click", () => {
    state.offset += state.limit;
    loadFiles();
  });

  document.querySelectorAll("th.sortable").forEach((th) => {
    th.addEventListener("click", onSortClick);
  });

  ui.cookie.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") doLogin();
  });

  // 预览：事件委托，避免为每行绑定监听
  ui.tbody.addEventListener("click", (e) => {
    const btn = e.target.closest(".preview-btn");
    if (!btn) return;
    const idx = Number(btn.dataset.idx);
    const item = state.items[idx];
    if (item) openPreview(item);
  });

  ui.pvClose.addEventListener("click", closePreview);
  ui.modal.addEventListener("click", (e) => {
    if (e.target.dataset.close) closePreview();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !ui.modal.classList.contains("hidden")) closePreview();
  });
  ui.pvImg.addEventListener("error", () => {
    ui.pvImg.classList.add("hidden");
    ui.pvEmpty.textContent = "缩略图加载失败（签名可能已过期，点击「开始扫描全盘」可刷新）";
    ui.pvEmpty.classList.remove("hidden");
  });

  /* ------------------------------ 启动 ------------------------------ */
  (async function init() {
    await refreshStatus();
    if (!state.loggedIn) {
      ui.loginCard.classList.remove("hidden");
    }
  })();
})();
