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
const MAX_MODELS = 20;
const CARD_MODELS = 4;
const MODEL_HINT = `逗号、空格或换行分隔，可一次粘贴多个；最多 ${MAX_MODELS} 个，测试时逐个进行`;
const splitModels = (text) => text.split(/[\s,，]+/).filter(Boolean);
const resultText = (run) => `${escape(run.prediction)} · ${percent(run.probability)}`;
const statusPill = (run) => `<span class="monitor-pill" data-tone="${escape(run?.status || "idle")}">${run ? runStatus[run.status] || escape(run.status) : "未测试"}</span>`;
const lastOutcome = (run) => run.status === "success" ? resultText(run)
  : escape(run.error || (run.status === "running" ? "等待完成" : runStatus[run.status] || run.status));
// 表格中「最近测试」「最近成功结果」两列，列表表格与详情页模型概览共用。失败原因可能很长，截断并用 title 展示全文。
function lastRunCell(last) {
  const note = last && last.status !== "success" ? ` · ${lastOutcome(last)}` : "";
  return `${statusPill(last)}${last ? `<small title="${escape(dateTime(last.started_at))}${note}">${escape(dateTime(last.started_at))}${note}</small>` : ""}`;
}
const successCell = (success) => success ? `<strong>${resultText(success)}</strong><small>${escape(dateTime(success.started_at))}</small>`
  : '<span class="monitor-muted">暂无成功记录</span>';
const TABLE_HEAD = "<thead><tr><th>配置</th><th>接口模型</th><th>定时</th><th>最近测试</th><th>最近成功结果</th><th>记录</th></tr></thead>";

async function request(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败，请重试");
  return payload;
}

const jsonRequest = (method, values) => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
});
const jobActive = (job) => job?.state === "running" || job?.state === "stopping";
// 任务中模型状态变化（开始、结束）时需要刷新模型列表与测试记录。
const jobModelsKey = (job) => job ? job.id + job.models.map((model) => model.status).join() : "";

// API 监测：配置列表（卡片 / 表格）→ 配置详情（操作、进度、分页测试记录）。
export function bindApiMonitor(renderResult) {
  const view = {
    configs: [], loaded: false, detailId: null, schedule: null, page: 0, total: 0,
    layout: localStorage.getItem(LAYOUT_KEY) === "table" ? "table" : "cards",
    listHtml: "", rowsHtml: "", modelsHtml: "", editingId: null, job: null, resultJobId: null, refocus: false,
    startBusy: false, configBusy: false, scheduleBusy: false, refreshFailed: false,
    draftModels: [], fetchedModels: [],
  };
  let scheduleVersion = 0;
  let modelsVersion = 0;
  let historyVersion = 0;
  let recordVersion = 0;
  let jobVersion = 0;
  let jobTimer = 0;
  const renderedHtml = {};
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

  const listContainer = () => byId(view.layout === "table" ? "monitor-table-content" : "monitor-grid");
  const schedulePill = (config) => {
    const schedule = activeSchedule(config);
    return schedule ? `<span class="monitor-pill" data-tone="${schedule.state === "stopping" ? "cancelled" : "running"}">${scheduleStatus[schedule.state]}</span>` : "";
  };

  // 卡片按模型列出最近状态：有成功记录时展示最近成功结果，否则展示最近一次的原因。
  function cardModelHtml(model) {
    const last = model.last_run;
    const success = model.last_success;
    const detail = success ? `最近成功 ${resultText(success)}` : last ? lastOutcome(last) : "尚未测试";
    return `<span class="monitor-card-model">
      <span class="monitor-card-line"><span class="monitor-card-model-name">${escape(model.api_model)}</span>${statusPill(last)}</span>
      <span class="monitor-card-detail" data-tone="${success ? "success" : ""}" title="${detail}">${detail}</span>
    </span>`;
  }

  function cardHtml(config) {
    const more = config.models.length - CARD_MODELS;
    return `<button type="button" class="monitor-card" data-config-id="${escape(config.id)}">
      <span class="monitor-card-head"><strong>${escape(config.name)}</strong>${schedulePill(config)}</span>
      <span class="monitor-card-url">${escape(config.base_url)}</span>
      <span class="monitor-card-models">${config.models.slice(0, CARD_MODELS).map(cardModelHtml).join("")}${more > 0 ? `<span class="monitor-card-more">另有 ${more} 个模型</span>` : ""}</span>
      <span class="monitor-card-foot"><span class="monitor-card-line">
        <span>${config.last_run ? `最近 ${escape(dateTime(config.last_run.started_at))}` : "尚未测试"}</span>
        <span>${config.api_models.length} 个模型 · ${config.run_count} 条记录</span>
      </span></span>
    </button>`;
  }

  // 每个配置一个 tbody、每个模型一行，配置、定时和记录列跨行；整组可点击进入详情，名称按钮承担键盘焦点。
  function rowsHtml(config) {
    const span = config.models.length;
    const rows = config.models.map((model, index) => `<tr>
      ${index ? "" : `<td rowspan="${span}"><button class="monitor-link" type="button" data-config-id="${escape(config.id)}">${escape(config.name)}</button><small>${escape(config.base_url)}</small></td>`}
      <td class="monitor-model-name">${escape(model.api_model)}</td>
      ${index ? "" : `<td rowspan="${span}">${schedulePill(config) || '<span class="monitor-muted">未开启</span>'}</td>`}
      <td>${lastRunCell(model.last_run)}</td>
      <td>${successCell(model.last_success)}</td>
      ${index ? "" : `<td rowspan="${span}">${config.run_count} 条</td>`}
    </tr>`).join("");
    return `<tbody data-config-id="${escape(config.id)}">${rows}</tbody>`;
  }

  function renderList() {
    byId("monitor-loading").hidden = view.loaded;
    byId("monitor-empty").hidden = !view.loaded || view.configs.length > 0;
    byId("monitor-grid").hidden = !view.configs.length || view.layout !== "cards";
    byId("monitor-table").hidden = !view.configs.length || view.layout !== "table";
    const container = listContainer();
    const html = view.layout === "table" ? TABLE_HEAD + view.configs.map(rowsHtml).join("") : view.configs.map(cardHtml).join("");
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
    byId("monitor-model-count").textContent = `${config.api_models.length} 个`;
    byId("monitor-run-meta").textContent = config.api_models.length > 1
      ? `逐个测试全部 ${config.api_models.length} 个模型，每个模型目标 3 份有效回答、最多尝试 6 个挑战`
      : "目标 3 份有效回答，最多尝试 6 个挑战";
    byId("monitor-stream").textContent = config.stream ? "流式" : "完整响应";
    byId("monitor-temperature").textContent = config.temperature ?? "接口默认";
    byId("monitor-interval").textContent = `${config.interval_minutes} 分钟`;
    const testing = jobActive(view.job);
    byId("monitor-run").disabled = view.startBusy || testing;
    byId("monitor-run").textContent = testing ? "正在测试…" : view.startBusy ? "正在启动…" : "立即测试";
    byId("monitor-edit").disabled = view.configBusy || testing;
    byId("monitor-delete").disabled = byId("monitor-edit").disabled;
    renderJob();
    renderModelRows(config);
    renderSchedule();
  }

  function setHtml(id, html) {
    // 轮询时内容不变就不重绘，避免打断用户选中、复制错误信息。
    if (renderedHtml[id] === html) return;
    byId(id).innerHTML = html;
    renderedHtml[id] = html;
  }

  // 手动测试进度来自服务端任务快照：进行中展示当前模型的挑战进度，结束后多个模型展示汇总、单个模型展示归因结果。
  function renderJob() {
    const job = view.job;
    byId("monitor-progress").hidden = !job;
    if (!job) {
      byId("monitor-run-result").hidden = true;
      return;
    }
    const active = jobActive(job);
    const total = job.models.length;
    const model = job.models[job.current];
    const summary = total > 1 && !active;
    const succeeded = job.models.filter((item) => item.status === "success").length;
    let status;
    if (active) {
      const prefix = total > 1 ? `模型 ${job.current + 1}/${total} · ${model.api_model}：` : "";
      status = prefix + (job.state === "stopping" ? "正在停止，等待当前请求结束……"
        : !job.attempt ? "正在生成挑战，准备调用模型"
        : job.steps[job.attempt - 1] === "working" ? `正在进行第 ${job.attempt} 次尝试，等待模型完整输出……`
        : `当前已有 ${job.accepted}/3 份有效回答`);
    } else if (summary) {
      status = `${job.state === "stopped" ? "已停止" : "全部完成"}：${succeeded}/${total} 个模型测试成功，各模型结果见下方「模型」列表`;
    } else {
      status = model.status === "success" ? `测试完成：${job.result.used_outputs}/3 份有效回答进入归因`
        : job.state === "stopped" ? "测试已停止" : "测试未完成";
    }
    byId("monitor-progress-status").textContent = status;
    byId("monitor-progress-count").textContent = summary ? `成功 ${succeeded}/${total}` : `有效 ${job.accepted}/3 · 已尝试 ${job.attempt}/6`;
    byId("monitor-progress-fill").style.width = `${(summary ? succeeded / total : job.accepted / 3) * 100}%`;
    setHtml("monitor-progress-steps", summary ? "" : job.steps.map((state, index) => `<span class="progress-step ${state}"><b>${index + 1}</b>挑战 ${index + 1} · ${stepLabels[state]}</span>`).join(""));
    setHtml("monitor-test-errors", job.errors.map((text) => `<li>${escape(text)}</li>`).join(""));
    const stop = byId("monitor-stop");
    stop.hidden = !active;
    stop.disabled = job.state === "stopping";
    stop.textContent = job.state === "stopping" ? "正在停止…" : "停止测试";
    byId("monitor-stream-output").hidden = !job.stream;
    const text = job.text || (active ? "等待上游输出…" : "");
    if (byId("monitor-stream-text").textContent !== text) byId("monitor-stream-text").textContent = text;
    byId("monitor-stream-title").textContent = job.text ? `当前挑战输出 · ${job.text.length} 字符` : "当前挑战输出";
    // 单个模型成功后只渲染一次归因结果。
    if (!active && total === 1 && model.status === "success" && view.resultJobId !== job.id) {
      view.resultJobId = job.id;
      renderResult(job.result, byId("monitor-run-result"));
    }
    byId("monitor-run-result").hidden = view.resultJobId !== job.id;
  }

  function renderModelRows(config) {
    const html = config.models.map((model) => `<tr>
      <td class="monitor-model-name">${escape(model.api_model)}</td>
      <td>${lastRunCell(model.last_run)}</td>
      <td>${successCell(model.last_success)}</td>
      <td><button class="monitor-link" type="button" data-test-model="${escape(model.api_model)}"${view.startBusy || jobActive(view.job) ? " disabled" : ""}>单独测试<span class="visually-hidden"> ${escape(model.api_model)}</span></button></td>
    </tr>`).join("");
    if (html === view.modelsHtml) return;
    const focused = document.activeElement?.dataset.testModel;
    byId("monitor-models-rows").innerHTML = html;
    view.modelsHtml = html;
    if (focused) byId("monitor-models-rows").querySelector(`[data-test-model="${CSS.escape(focused)}"]`)?.focus();
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
    view.modelsHtml = "";
    view.job = null;
    byId("monitor-records-rows").replaceChildren();
    byId("monitor-records-table").hidden = true;
    byId("monitor-pager").hidden = true;
    byId("monitor-records-count").textContent = "";
    byId("monitor-records-empty").hidden = false;
    byId("monitor-records-empty").textContent = "正在读取测试记录…";
    renderDetail();
    byId("monitor-name").focus();
    loadHistory();
    loadJob();
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
        <td>${escape(dateTime(run.started_at))}</td>
        <td class="monitor-model-name">${escape(run.api_model)}</td>
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
    view.draftModels = [...(config?.api_models || [])];
    view.fetchedModels = [];
    modelsVersion++;
    byId("monitor-config-form").reset();
    byId("monitor-config-title").textContent = config ? "编辑配置" : "新建配置";
    for (const [field, key] of [["name", "name"], ["base", "base_url"], ["temperature", "temperature"]]) {
      byId(`config-${field}`).value = config?.[key] ?? "";
    }
    byId("config-stream").checked = config?.stream ?? true;
    byId("config-interval").value = config?.interval_minutes ?? 60;
    byId("config-key").required = !config;
    byId("config-key").placeholder = config ? "留空则保留已保存的密钥" : "";
    byId("config-fetch-models").disabled = false;
    byId("config-fetch-models").textContent = "获取模型";
    byId("config-model-picker").hidden = true;
    modelHint();
    renderDraftModels();
    byId("monitor-config-error").textContent = "";
    configDialog.showModal();
    byId("config-name").focus();
  }

  function modelHint(text = MODEL_HINT, tone = "") {
    byId("config-models-hint").textContent = text;
    byId("config-models-hint").dataset.tone = tone;
  }

  // 已选模型渲染为标签；获取到的列表只同步勾选状态，不重绘，避免键盘勾选时丢失焦点。
  function renderDraftModels() {
    const input = byId("config-model-input");
    byId("config-model-tags").innerHTML = view.draftModels.map((model) => `<li><span>${escape(model)}</span><button type="button" data-remove-model="${escape(model)}" aria-label="移除 ${escape(model)}">×</button></li>`).join("");
    input.setCustomValidity(view.draftModels.length || input.value.trim() ? "" : "请至少添加一个模型");
    for (const box of byId("config-model-options").querySelectorAll("input")) box.checked = view.draftModels.includes(box.value);
  }

  function renderModelOptions() {
    const query = byId("config-model-search").value.trim().toLowerCase();
    const shown = view.fetchedModels.filter((model) => model.toLowerCase().includes(query));
    byId("config-model-options").innerHTML = shown.length
      ? shown.map((model) => `<label class="monitor-model-option"><input type="checkbox" value="${escape(model)}"${view.draftModels.includes(model) ? " checked" : ""}><span>${escape(model)}</span></label>`).join("")
      : '<p class="monitor-muted">没有匹配的模型</p>';
  }

  function addModels(models) {
    let skipped = 0;
    for (const model of models) {
      if (view.draftModels.includes(model)) continue;
      if (view.draftModels.length >= MAX_MODELS) skipped++;
      else view.draftModels.push(model);
    }
    if (skipped) modelHint(`最多 ${MAX_MODELS} 个模型，已忽略 ${skipped} 个`, "error");
    renderDraftModels();
  }

  function removeModel(model) {
    view.draftModels = view.draftModels.filter((item) => item !== model);
    modelHint();
    renderDraftModels();
  }

  // 输入框中的文字按分隔符拆成模型；回车、保存或输入分隔符时提交。
  function commitModelInput() {
    const input = byId("config-model-input");
    const models = splitModels(input.value);
    input.value = "";
    addModels(models);
  }

  async function fetchModels() {
    const button = byId("config-fetch-models");
    const version = ++modelsVersion;
    button.disabled = true;
    button.textContent = "获取中…";
    modelHint("正在获取模型列表…");
    try {
      const { models } = await request("/api/test/models", jsonRequest("POST", {
        base_url: byId("config-base").value.trim(), api_key: byId("config-key").value, config_id: view.editingId,
      }));
      if (version !== modelsVersion) return;
      view.fetchedModels = models;
      byId("config-model-search").value = "";
      byId("config-model-picker").hidden = !models.length;
      renderModelOptions();
      if (!models.length) return modelHint("接口没有返回模型，请手动填写", "error");
      modelHint(`已获取 ${models.length} 个模型，勾选即可添加；也可继续手动填写`);
      byId("config-model-search").focus();
    } catch (error) {
      if (version === modelsVersion) modelHint(`获取模型失败：${error.message}`, "error");
    } finally {
      if (version === modelsVersion) {
        button.disabled = false;
        button.textContent = "获取模型";
      }
    }
  }

  async function saveConfig(event) {
    event.preventDefault();
    if (view.configBusy) return;
    view.configBusy = true;
    byId("monitor-config-save").disabled = true;
    byId("monitor-config-save").textContent = "保存中…";
    byId("monitor-config-error").textContent = "";
    commitModelInput();
    try {
      const values = {
        name: byId("config-name").value.trim(), base_url: byId("config-base").value.trim(),
        api_models: view.draftModels, api_key: byId("config-key").value,
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

  // 手动测试在服务端后台运行，刷新或关闭页面不影响；进行中时每秒读取进度，地址中的配置 ID 可在配置列表加载前使用。
  async function loadJob() {
    window.clearTimeout(jobTimer);
    const version = ++jobVersion;
    const configId = view.detailId;
    if (!configId) return;
    let job;
    try {
      ({ job } = await request(`/api/test/manual/${encodeURIComponent(configId)}`));
    } catch {
      // 读取失败时稍后重试；常规刷新失败由 refresh 提示。
      if (version === jobVersion && jobActive(view.job)) jobTimer = window.setTimeout(loadJob, 3000);
      return;
    }
    if (version !== jobVersion) return;
    // 只展示进行中的任务，或本页一直在跟踪、刚结束的任务；此前已结束的结果见模型列表和测试记录。
    const tracked = Boolean(job) && view.job?.id === job.id;
    const next = job && (jobActive(job) || tracked) ? job : null;
    const finished = tracked && jobActive(view.job) && !jobActive(job);
    const changed = jobModelsKey(next) !== jobModelsKey(view.job);
    view.job = next;
    renderDetail();
    // 测试期间按钮被禁用会丢失焦点，键盘用户完成后回到原按钮。
    if (finished && view.refocus && document.activeElement === document.body) byId("monitor-run").focus();
    if (changed) refresh();
    if (jobActive(next)) jobTimer = window.setTimeout(loadJob, 1000);
  }

  // 手动测试：服务端按顺序测试所选模型，每个模型一条测试记录；单个模型失败不影响后续模型。
  async function runTest(models) {
    const config = current();
    if (!config || view.startBusy || jobActive(view.job)) return;
    view.startBusy = true;
    view.refocus = document.activeElement === byId("monitor-run");
    message();
    renderDetail();
    try {
      const { job } = await request(`/api/test/manual/${encodeURIComponent(config.id)}`, jsonRequest("POST", { api_models: models || config.api_models }));
      if (view.detailId === config.id) view.job = job;
    } catch (error) {
      if (view.detailId === config.id) message(error.message);
    } finally {
      view.startBusy = false;
      renderDetail();
    }
    await loadJob();
  }

  async function stopTest() {
    const config = current();
    if (!config || !jobActive(view.job)) return;
    try {
      const { job } = await request(`/api/test/manual/${encodeURIComponent(config.id)}`, { method: "DELETE" });
      if (job && view.job?.id === job.id) view.job = job;
      renderDetail();
    } catch (error) {
      message(error.message);
    }
    await loadJob();
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
  for (const id of ["monitor-grid", "monitor-table-content"]) {
    byId(id).addEventListener("click", (event) => {
      const target = event.target.closest("[data-config-id]");
      if (target) go(target.dataset.configId);
    });
  }
  byId("monitor-back").addEventListener("click", () => go(null));
  byId("monitor-edit").addEventListener("click", () => editConfig(current()));
  byId("monitor-delete").addEventListener("click", deleteConfig);
  byId("monitor-run").addEventListener("click", () => runTest());
  byId("monitor-stop").addEventListener("click", stopTest);
  byId("monitor-models-rows").addEventListener("click", (event) => {
    const button = event.target.closest("[data-test-model]");
    if (button) runTest([button.dataset.testModel]);
  });
  byId("monitor-schedule-toggle").addEventListener("click", toggleSchedule);
  byId("monitor-records-refresh").addEventListener("click", () => loadHistory());
  byId("monitor-prev").addEventListener("click", () => { view.page = Math.max(0, view.page - 1); loadHistory(); });
  byId("monitor-next").addEventListener("click", () => { view.page++; loadHistory(); });
  byId("monitor-records-rows").addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-id]");
    if (button) showRecord(button.dataset.runId);
  });
  byId("monitor-config-form").addEventListener("submit", saveConfig);
  const modelInput = byId("config-model-input");
  modelInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      commitModelInput();
    } else if (event.key === "Backspace" && !modelInput.value && view.draftModels.length) {
      removeModel(view.draftModels.at(-1));
    }
  });
  // 输入或粘贴中出现分隔符时立即拆成标签。
  modelInput.addEventListener("input", () => {
    if (/[\s,，]/.test(modelInput.value)) commitModelInput();
    else renderDraftModels();
  });
  byId("config-model-tags").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-model]");
    if (!button) return;
    removeModel(button.dataset.removeModel);
    modelInput.focus();
  });
  modelInput.parentElement.addEventListener("click", (event) => { if (event.target === event.currentTarget) modelInput.focus(); });
  byId("config-fetch-models").addEventListener("click", fetchModels);
  byId("config-model-search").addEventListener("input", renderModelOptions);
  byId("config-model-search").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
  byId("config-model-options").addEventListener("change", (event) => {
    const box = event.target;
    if (!box.checked) return removeModel(box.value);
    addModels([box.value]);
    box.checked = view.draftModels.includes(box.value);
  });
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
  // 定时与手动测试都由服务端运行，页面只在可见时轮询状态、卡片与当前页记录。
  async function poll() {
    if (!document.hidden && byId("workspace-api").classList.contains("active")) {
      await refresh();
      // 发现其他页面发起的手动测试；进行中时由 loadJob 自行每秒轮询。
      if (!jobActive(view.job)) loadJob();
    }
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
