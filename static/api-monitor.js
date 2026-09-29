import { readTestEvents } from "./test-controls.js";

const byId = (id) => document.getElementById(id);
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
const dateTime = (value) => value ? new Date(value).toLocaleString() : "—";
const percent = (value) => `${(value * 100).toFixed(1)}%`;
const duration = (seconds) => seconds == null ? "—" : seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
const runStatus = { running: "进行中", success: "成功", error: "失败", cancelled: "已取消" };
const scheduleStatus = { running: "正在测试", waiting: "等待下一轮", stopping: "正在停止" };
const stepLabels = { pending: "等待", working: "请求中", done: "有效", invalid: "数字不足", error: "接口失败", skipped: "无需调用" };
const PAGE_SIZE = 10;
const LAYOUT_KEY = "modeltrace.monitor-layout";
const resultText = (run) => `${escape(run.prediction)} · ${percent(run.probability)}`;
const statusPill = (run) => `<span class="monitor-pill" data-tone="${escape(run?.status || "idle")}">${run ? runStatus[run.status] || escape(run.status) : "未测试"}</span>`;
const lastOutcome = (run) => run.status === "success" ? resultText(run)
  : escape(run.error || (run.status === "running" ? "等待完成" : runStatus[run.status] || run.status));

async function request(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败，请重试");
  return payload;
}

const jsonRequest = (method, values) => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
});

// API 监测：配置列表（卡片 / 表格）→ 配置详情（操作、进度、分页测试记录）。
export function bindApiMonitor(renderResult) {
  const view = {
    configs: [], loaded: false, detailId: null, schedule: null, page: 0, total: 0,
    layout: localStorage.getItem(LAYOUT_KEY) === "table" ? "table" : "cards",
    listHtml: "", rowsHtml: "", editingId: null, runConfigId: null, runResult: false,
    runBusy: false, configBusy: false, scheduleBusy: false, refreshFailed: false,
  };
  let scheduleVersion = 0;
  let historyVersion = 0;
  let recordVersion = 0;
  const configDialog = byId("monitor-config-dialog");
  const recordDialog = byId("monitor-record-dialog");
  const current = () => view.configs.find((config) => config.id === view.detailId);
  const activeSchedule = (config) => view.schedule && view.schedule.state !== "stopped" && view.schedule.config_id === config?.id ? view.schedule : null;

  function message(text = "", type = "error") {
    const shown = view.detailId ? "monitor-detail-message" : "monitor-list-message";
    for (const id of ["monitor-list-message", "monitor-detail-message"]) {
      const element = byId(id);
      element.textContent = id === shown ? text : "";
      element.className = `message ${type}`;
      element.hidden = id !== shown || !text;
    }
  }

  const listContainer = () => byId(view.layout === "table" ? "monitor-table-rows" : "monitor-grid");
  const schedulePill = (config) => {
    const schedule = activeSchedule(config);
    return schedule ? `<span class="monitor-pill" data-tone="${schedule.state === "stopping" ? "cancelled" : "running"}">${scheduleStatus[schedule.state]}</span>` : "";
  };

  function cardHtml(config) {
    const last = config.last_run;
    const success = config.last_success;
    return `<button type="button" class="monitor-card" data-config-id="${escape(config.id)}">
      <span class="monitor-card-head"><strong>${escape(config.name)}</strong>${schedulePill(config)}</span>
      <span class="monitor-card-model">${escape(config.api_model)}</span>
      <span class="monitor-card-url">${escape(config.base_url)}</span>
      <span class="monitor-card-foot">
        <span class="monitor-card-line"><span>${last ? `最近 ${escape(dateTime(last.started_at))}` : "最近结果"}</span><span>${config.run_count} 条记录</span></span>
        <span class="monitor-card-line">${statusPill(last)}<span class="monitor-card-outcome">${last ? lastOutcome(last) : "尚未测试"}</span></span>
        <span class="monitor-card-line"><span>最近成功</span><span>${success ? escape(dateTime(success.started_at)) : "暂无成功记录"}</span></span>
        ${success ? `<span class="monitor-card-success" title="${resultText(success)}">${resultText(success)}</span>` : ""}
      </span>
    </button>`;
  }

  // 整行可点击进入详情；名称按钮承担键盘焦点。失败原因可能很长，表格内截断并用 title 展示全文。
  function rowHtml(config) {
    const last = config.last_run;
    const success = config.last_success;
    const lastNote = last && last.status !== "success" ? ` · ${lastOutcome(last)}` : "";
    return `<tr data-config-id="${escape(config.id)}">
      <td><button class="monitor-link" type="button" data-config-id="${escape(config.id)}">${escape(config.name)}</button><small>${escape(config.base_url)}</small></td>
      <td>${escape(config.api_model)}</td>
      <td>${schedulePill(config) || '<span class="monitor-muted">未开启</span>'}</td>
      <td>${statusPill(last)}${last ? `<small title="${escape(dateTime(last.started_at))}${lastNote}">${escape(dateTime(last.started_at))}${lastNote}</small>` : ""}</td>
      <td>${success ? `<strong>${resultText(success)}</strong><small>${escape(dateTime(success.started_at))}</small>` : '<span class="monitor-muted">暂无成功记录</span>'}</td>
      <td>${config.run_count} 条</td>
    </tr>`;
  }

  function renderList() {
    byId("monitor-loading").hidden = view.loaded;
    byId("monitor-empty").hidden = !view.loaded || view.configs.length > 0;
    byId("monitor-grid").hidden = !view.configs.length || view.layout !== "cards";
    byId("monitor-table").hidden = !view.configs.length || view.layout !== "table";
    const container = listContainer();
    const html = view.configs.map(view.layout === "table" ? rowHtml : cardHtml).join("");
    // 轮询刷新时内容不变就不重绘，避免键盘焦点丢失。
    if (html !== view.listHtml) {
      const focused = container.contains(document.activeElement) ? document.activeElement.dataset.configId : null;
      container.innerHTML = html;
      view.listHtml = html;
      if (focused) container.querySelector(`button[data-config-id="${CSS.escape(focused)}"]`)?.focus();
    }
  }

  function setLayout(layout) {
    view.layout = layout;
    view.listHtml = "";
    localStorage.setItem(LAYOUT_KEY, layout);
    for (const button of document.querySelectorAll("[data-monitor-layout]")) {
      const active = button.dataset.monitorLayout === layout;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    }
    renderList();
  }

  function renderSchedule() {
    const config = current();
    if (!config) return;
    const schedule = view.schedule;
    const mine = activeSchedule(config);
    const pill = byId("monitor-schedule-state");
    const toggle = byId("monitor-schedule-toggle");
    let meta;
    // 开关本身表达开/关，状态标签只在本配置定时运行时显示当前阶段。
    pill.hidden = !mine;
    if (!schedule) {
      meta = "";
    } else if (mine) {
      pill.textContent = scheduleStatus[mine.state];
      pill.dataset.tone = mine.state === "stopping" ? "cancelled" : "running";
      meta = mine.state === "stopping" ? "等待当前请求结束后停止"
        : `每轮结束后等待 ${mine.interval_minutes} 分钟${mine.next_run_at ? ` · 下次 ${dateTime(mine.next_run_at)}` : ""} · 修改配置后需重新开启才生效`;
    } else {
      meta = schedule.state !== "stopped"
        ? `「${schedule.config_name}」正在定时测试，同一时间只运行一个定时任务`
        : `开启后立即测试一轮，之后每轮结束等待 ${config.interval_minutes} 分钟；关闭页面仍继续，服务重启后需重新开启`;
    }
    byId("monitor-schedule-meta").textContent = meta;
    toggle.setAttribute("aria-checked", String(Boolean(mine)));
    toggle.disabled = !schedule || view.scheduleBusy || (mine ? mine.state === "stopping" : schedule.state !== "stopped");
  }

  function renderDetail() {
    const config = current();
    if (!config) return;
    byId("monitor-crumb").textContent = config.name;
    byId("monitor-name").textContent = config.name;
    byId("monitor-url").textContent = config.base_url;
    byId("monitor-model").textContent = config.api_model;
    byId("monitor-stream").textContent = config.stream ? "流式" : "完整响应";
    byId("monitor-temperature").textContent = config.temperature ?? "接口默认";
    byId("monitor-interval").textContent = `${config.interval_minutes} 分钟`;
    byId("monitor-run").disabled = view.runBusy;
    byId("monitor-run").textContent = view.runBusy && view.runConfigId === config.id ? "正在测试…" : "立即测试";
    byId("monitor-edit").disabled = view.configBusy || (view.runBusy && view.runConfigId === config.id);
    byId("monitor-delete").disabled = byId("monitor-edit").disabled;
    // 进度与结果只属于发起测试的配置，切到其他配置时隐藏。
    byId("monitor-progress").hidden = view.runConfigId !== config.id;
    byId("monitor-run-result").hidden = view.runConfigId !== config.id || !view.runResult;
    renderSchedule();
  }

  function showView(detailId) {
    const previous = view.detailId;
    view.detailId = detailId;
    byId("monitor-list-view").hidden = Boolean(detailId);
    byId("monitor-detail-view").hidden = !detailId;
    message();
    if (!detailId) {
      renderList();
      // 从详情返回列表时把焦点还给对应卡片或表格行。
      if (previous) listContainer().querySelector(`button[data-config-id="${CSS.escape(previous)}"]`)?.focus();
      return;
    }
    view.page = 0;
    view.rowsHtml = "";
    byId("monitor-records-rows").replaceChildren();
    byId("monitor-records-table").hidden = true;
    byId("monitor-pager").hidden = true;
    byId("monitor-records-count").textContent = "";
    byId("monitor-records-empty").hidden = false;
    byId("monitor-records-empty").textContent = "正在读取测试记录…";
    renderDetail();
    byId("monitor-name").focus();
    loadHistory();
  }

  async function loadConfigs() {
    const payload = await request("/api/test/configs");
    view.configs = payload.configs;
    view.loaded = true;
    // 地址中的配置不存在或已在其他页面删除时回到列表。
    if (view.detailId && !current()) go(null, true);
    renderList();
    renderDetail();
  }

  async function loadSchedule() {
    const version = scheduleVersion;
    try {
      const schedule = await request("/api/test/schedule");
      if (version !== scheduleVersion || view.scheduleBusy) return;
      view.schedule = schedule;
    } catch (error) {
      if (version === scheduleVersion) byId("monitor-schedule-meta").textContent = `定时状态读取失败：${error.message}`;
      return;
    }
    renderList();
    renderSchedule();
  }

  async function loadHistory() {
    const config = current();
    if (!config) return;
    const version = ++historyVersion;
    const query = new URLSearchParams({ config_id: config.id, limit: String(PAGE_SIZE), offset: String(view.page * PAGE_SIZE) });
    try {
      const history = await request(`/api/test/history?${query}`);
      if (version !== historyVersion) return;
      view.total = history.total;
      const pages = Math.max(1, Math.ceil(view.total / PAGE_SIZE));
      if (view.page >= pages) { view.page = pages - 1; return loadHistory(); }
      const html = history.items.map((run) => `<tr>
        <td>${escape(dateTime(run.started_at))}${run.api_model !== config.api_model ? `<small>${escape(run.api_model)}</small>` : ""}</td>
        <td>${run.source === "scheduled" ? "定时" : "手动"}</td>
        <td><span class="monitor-pill" data-tone="${escape(run.status)}">${runStatus[run.status] || escape(run.status)}</span></td>
        <td class="monitor-result-cell">${run.status === "success" ? `<strong>${escape(run.prediction)}</strong> · ${percent(run.probability)}` : escape(run.error || "等待完成")}</td>
        <td>${duration(run.duration_seconds)}</td>
        <td><button class="monitor-link" type="button" data-run-id="${escape(run.id)}" aria-label="查看 ${escape(dateTime(run.started_at))} 的测试详情">查看</button></td>
      </tr>`).join("");
      if (html !== view.rowsHtml) {
        const focused = document.activeElement?.dataset.runId;
        byId("monitor-records-rows").innerHTML = html;
        view.rowsHtml = html;
        if (focused) byId("monitor-records-rows").querySelector(`[data-run-id="${CSS.escape(focused)}"]`)?.focus();
      }
      byId("monitor-records-count").textContent = view.total ? `共 ${view.total} 条` : "";
      byId("monitor-records-empty").hidden = view.total > 0;
      byId("monitor-records-empty").textContent = "还没有测试记录。点击「立即测试」或开启定时后，结果会显示在这里。";
      byId("monitor-records-table").hidden = view.total === 0;
      byId("monitor-records").dataset.paged = String(view.total > 0);
      byId("monitor-pager").hidden = view.total === 0;
      byId("monitor-page").textContent = `第 ${view.page + 1} / ${pages} 页`;
      byId("monitor-prev").disabled = view.page === 0;
      byId("monitor-next").disabled = view.page >= pages - 1;
    } catch (error) {
      if (version !== historyVersion) return;
      byId("monitor-records-empty").hidden = false;
      byId("monitor-records-empty").textContent = `测试记录读取失败：${error.message}`;
    }
  }

  function editConfig(config = null) {
    view.editingId = config?.id || null;
    byId("monitor-config-form").reset();
    byId("monitor-config-title").textContent = config ? "编辑配置" : "新建配置";
    for (const [field, key] of [["name", "name"], ["base", "base_url"], ["model", "api_model"], ["temperature", "temperature"]]) {
      byId(`config-${field}`).value = config?.[key] ?? "";
    }
    byId("config-stream").checked = config?.stream ?? true;
    byId("config-interval").value = config?.interval_minutes ?? 60;
    byId("config-key").required = !config;
    byId("config-key").placeholder = config ? "留空则保留已保存的密钥" : "";
    byId("monitor-config-error").textContent = "";
    configDialog.showModal();
    byId("config-name").focus();
  }

  async function saveConfig(event) {
    event.preventDefault();
    if (view.configBusy) return;
    view.configBusy = true;
    byId("monitor-config-save").disabled = true;
    byId("monitor-config-save").textContent = "保存中…";
    byId("monitor-config-error").textContent = "";
    try {
      const values = {
        name: byId("config-name").value.trim(), base_url: byId("config-base").value.trim(),
        api_model: byId("config-model").value.trim(), api_key: byId("config-key").value,
        temperature: byId("config-temperature").value === "" ? null : Number(byId("config-temperature").value),
        stream: byId("config-stream").checked, interval_minutes: Number(byId("config-interval").value),
      };
      const editing = view.editingId;
      const { config } = await request(`/api/test/configs${editing ? `/${encodeURIComponent(editing)}` : ""}`, jsonRequest(editing ? "PATCH" : "POST", values));
      configDialog.close();
      await loadConfigs();
      if (!editing) go(config.id);
      else message(activeSchedule(config) ? "配置已保存。定时任务仍使用开启时的配置，停止后重新开启才会生效。" : "配置已保存。", "success");
    } catch (error) {
      byId("monitor-config-error").textContent = error.message;
    } finally {
      view.configBusy = false;
      byId("monitor-config-save").disabled = false;
      byId("monitor-config-save").textContent = "保存配置";
      renderDetail();
    }
  }

  async function deleteConfig() {
    const config = current();
    if (!config || view.configBusy) return;
    const records = config.run_count ? `，并同时删除它的 ${config.run_count} 条测试记录` : "";
    if (!window.confirm(`删除配置「${config.name}」${records}？删除后无法恢复。`)) return;
    view.configBusy = true;
    renderDetail();
    try {
      const { deleted_runs: deletedRuns } = await request(`/api/test/configs/${encodeURIComponent(config.id)}`, { method: "DELETE" });
      go(null, true);
      await loadConfigs();
      message(`已删除配置「${config.name}」${deletedRuns ? `及 ${deletedRuns} 条测试记录` : ""}。`, "success");
    } catch (error) {
      message(error.message);
    } finally {
      view.configBusy = false;
      renderDetail();
    }
  }

  async function toggleSchedule() {
    const config = current();
    if (!config || view.scheduleBusy) return;
    const stopping = Boolean(activeSchedule(config));
    view.scheduleBusy = true;
    scheduleVersion++;
    renderSchedule();
    message();
    try {
      view.schedule = await request("/api/test/schedule", stopping ? { method: "DELETE" } : jsonRequest("POST", { config_id: config.id }));
    } catch (error) {
      message(error.message);
    } finally {
      view.scheduleBusy = false;
      renderList();
      renderSchedule();
      await refresh();
    }
  }

  async function runTest() {
    const config = current();
    if (!config || view.runBusy) return;
    view.runBusy = true;
    view.runConfigId = config.id;
    view.runResult = false;
    const refocus = document.activeElement === byId("monitor-run");
    const states = Array(6).fill("pending");
    let accepted = 0;
    let attempt = 0;
    let body = "";
    const renderProgress = (status) => {
      if (status) byId("monitor-progress-status").textContent = status;
      byId("monitor-progress-count").textContent = `有效 ${accepted}/3 · 已尝试 ${attempt}/6`;
      byId("monitor-progress-fill").style.width = `${accepted / 3 * 100}%`;
      byId("monitor-progress-steps").innerHTML = states.map((state, index) => `<span class="progress-step ${state}"><b>${index + 1}</b>挑战 ${index + 1} · ${stepLabels[state]}</span>`).join("");
    };
    const addError = (text) => {
      const item = document.createElement("li");
      item.textContent = text;
      byId("monitor-test-errors").append(item);
    };
    message();
    byId("monitor-run-result").hidden = true;
    byId("monitor-test-errors").replaceChildren();
    byId("monitor-stream-output").hidden = !config.stream;
    byId("monitor-stream-title").textContent = "当前挑战输出";
    byId("monitor-stream-text").textContent = "等待上游输出…";
    renderDetail();
    renderProgress("正在生成挑战，准备调用模型");
    try {
      const response = await fetch("/api/test/runs", jsonRequest("POST", { config_id: config.id }));
      const final = await readTestEvents(response, (event) => {
        if (event.type === "challenge") {
          attempt = event.attempt;
          states[attempt - 1] = "working";
          body = "";
          byId("monitor-stream-text").textContent = "等待上游输出…";
          renderProgress(`正在进行第 ${attempt} 次尝试，等待模型完整输出……`);
        } else if (event.type === "delta") {
          body += event.text;
          byId("monitor-stream-text").textContent = body;
          byId("monitor-stream-title").textContent = `当前挑战输出 · ${body.length} 字符`;
        } else if (event.type === "challenge_result") {
          states[event.attempt - 1] = event.accepted ? "done" : "invalid";
          if (event.accepted) accepted++;
          else addError(`挑战 ${event.attempt}：有效数字不足 ${event.parsed_numbers}/${event.minimum_numbers}`);
          renderProgress(`当前已有 ${accepted}/3 份有效回答`);
        } else if (event.type === "challenge_error") {
          states[event.attempt - 1] = "error";
          addError(`挑战 ${event.attempt}：${event.error}`);
          renderProgress(`当前已有 ${accepted}/3 份有效回答`);
        }
      });
      states.forEach((state, index) => { if (state === "pending") states[index] = "skipped"; });
      renderProgress(`测试完成：${final.result.used_outputs}/3 份有效回答进入归因`);
      view.runResult = true;
      if (view.detailId === config.id) renderResult(final.result, byId("monitor-run-result"));
    } catch (error) {
      renderProgress("测试未完成");
      if (view.detailId === config.id) message(error.message);
    } finally {
      view.runBusy = false;
      renderDetail();
      // 测试期间按钮被禁用会丢失焦点，键盘用户完成后回到原按钮。
      if (refocus && document.activeElement === document.body && view.detailId === config.id) byId("monitor-run").focus();
      await refresh();
    }
  }

  async function showRecord(runId) {
    const version = ++recordVersion;
    byId("monitor-record-title").textContent = "测试详情";
    byId("monitor-record-meta").textContent = "正在读取…";
    byId("monitor-record-body").replaceChildren();
    recordDialog.showModal();
    try {
      const { run } = await request(`/api/test/history/${encodeURIComponent(runId)}`);
      if (version !== recordVersion) return;
      const snapshot = run.config_snapshot;
      byId("monitor-record-title").textContent = `测试详情 · ${runStatus[run.status] || run.status}`;
      byId("monitor-record-meta").textContent = [
        dateTime(run.started_at), run.source === "scheduled" ? "定时" : "手动", run.api_model,
        snapshot.stream ? "流式" : "完整响应", `温度 ${snapshot.temperature ?? "接口默认"}`,
        run.duration_seconds != null ? `耗时 ${duration(run.duration_seconds)}` : null, snapshot.base_url,
      ].filter(Boolean).join(" · ");
      const body = byId("monitor-record-body");
      if (run.result) {
        const panel = document.createElement("div");
        panel.className = "result-panel";
        // 先渲染再插入，避免 renderResult 的 scrollIntoView 把对话框标题和摘要滚出视野。
        renderResult(run.result, panel);
        body.append(panel);
        const errors = run.result.api_test?.errors || [];
        if (errors.length) {
          const list = document.createElement("ul");
          list.className = "test-errors";
          list.append(...errors.map((text) => Object.assign(document.createElement("li"), { textContent: text })));
          body.append(list);
        }
      } else {
        const note = document.createElement("div");
        note.className = `message ${run.status === "running" ? "working" : "error"}`;
        note.textContent = run.error || "本轮仍在测试，完成后可重新查看。";
        body.append(note);
      }
    } catch (error) {
      if (version === recordVersion) byId("monitor-record-meta").textContent = error.message;
    }
  }

  async function refresh() {
    try {
      await Promise.all([loadConfigs(), loadSchedule()]);
      await loadHistory();
      if (view.refreshFailed) { view.refreshFailed = false; message(); }
    } catch (error) {
      if (!view.loaded) {
        byId("monitor-loading").textContent = `配置读取失败：${error.message}`;
        // 直接打开详情地址时列表不可见，失败原因显示在详情页。
        if (view.detailId) message(`配置读取失败：${error.message}`);
      }
      else { view.refreshFailed = true; message(`刷新失败：${error.message}`); }
    }
  }

  // 详情页地址为 #/api/<配置 ID>；主动跳转写入历史，配置失效时用 replace，避免后退回到空详情。
  function go(detailId, replace = false) {
    const hash = detailId ? `#/api/${encodeURIComponent(detailId)}` : "#/api";
    if (location.hash !== hash) history[replace ? "replaceState" : "pushState"](null, "", hash);
    showView(detailId);
  }

  byId("monitor-new").addEventListener("click", () => editConfig());
  byId("monitor-empty-new").addEventListener("click", () => editConfig());
  for (const button of document.querySelectorAll("[data-monitor-layout]")) {
    button.addEventListener("click", () => setLayout(button.dataset.monitorLayout));
  }
  for (const id of ["monitor-grid", "monitor-table-rows"]) {
    byId(id).addEventListener("click", (event) => {
      const target = event.target.closest("[data-config-id]");
      if (target) go(target.dataset.configId);
    });
  }
  byId("monitor-back").addEventListener("click", () => go(null));
  byId("monitor-edit").addEventListener("click", () => editConfig(current()));
  byId("monitor-delete").addEventListener("click", deleteConfig);
  byId("monitor-run").addEventListener("click", runTest);
  byId("monitor-schedule-toggle").addEventListener("click", toggleSchedule);
  byId("monitor-records-refresh").addEventListener("click", () => loadHistory());
  byId("monitor-prev").addEventListener("click", () => { view.page = Math.max(0, view.page - 1); loadHistory(); });
  byId("monitor-next").addEventListener("click", () => { view.page++; loadHistory(); });
  byId("monitor-records-rows").addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-id]");
    if (button) showRecord(button.dataset.runId);
  });
  byId("monitor-config-form").addEventListener("submit", saveConfig);
  configDialog.addEventListener("cancel", (event) => { if (view.configBusy) event.preventDefault(); });
  configDialog.addEventListener("close", () => { byId("config-key").value = ""; });
  recordDialog.addEventListener("close", () => { recordVersion++; });
  for (const dialog of [configDialog, recordDialog]) {
    dialog.addEventListener("click", (event) => {
      // 点击遮罩或关闭按钮关闭；保存中不允许关闭配置表单。
      const close = event.target === dialog || event.target.closest("[data-close-dialog]");
      if (close && !(dialog === configDialog && view.configBusy)) dialog.close();
    });
  }
  // 定时任务由服务端运行，页面只在可见时轮询状态、卡片与当前页记录。
  async function poll() {
    if (!document.hidden && byId("workspace-api").classList.contains("active")) await refresh();
    window.setTimeout(poll, 5000);
  }
  setLayout(view.layout);
  window.setTimeout(poll, 5000);

  // 路由入口：进入 API 监测、刷新页面或浏览器前进后退时由 app.js 调用；首次数据也在这里读取。
  return {
    open(detailId) {
      if (detailId !== view.detailId) showView(detailId);
      refresh();
    },
  };
}
