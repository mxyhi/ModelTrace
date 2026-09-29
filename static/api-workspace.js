import { readTestEvents } from "./test-controls.js";

const byId = (id) => document.getElementById(id);
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
const date = (value) => value ? new Date(value).toLocaleString() : "—";
const statusName = { running: "进行中", success: "成功", error: "失败", cancelled: "已取消" };

async function request(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败，请重试");
  return payload;
}

const jsonRequest = (method, values) => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
});

export function bindApiWorkspace(renderResult) {
  let configs = [];
  let selectedId = null;
  let editingId = null;
  let configBusy = false;
  let runBusy = false;
  let scheduleBusy = false;
  let schedule = null;
  let scheduleVersion = 0;
  let historyVersion = 0;
  let detailVersion = 0;
  let historyContents = "";
  let offset = 0;
  let total = 0;
  const limit = 20;
  const dialog = byId("api-config-dialog");
  const selected = () => configs.find((config) => config.id === selectedId);

  function message(text = "", error = true) {
    const element = byId("api-workspace-message");
    element.textContent = text;
    element.className = `message ${error ? "error" : "success"}`;
    element.hidden = !text;
  }

  function controls() {
    const config = selected();
    byId("api-run").disabled = !config || runBusy;
    byId("api-run").textContent = runBusy ? "正在测试…" : "立即测试";
    byId("api-edit-config").disabled = !config || configBusy || runBusy;
    byId("api-delete-config").disabled = !config || configBusy || runBusy;
    byId("api-new-config").disabled = configBusy;
    byId("api-schedule-start").disabled = !config || !schedule || schedule.state !== "stopped" || scheduleBusy;
    byId("api-schedule-stop").disabled = !schedule?.enabled || scheduleBusy;
    byId("api-config-save").disabled = configBusy;
    byId("api-config-cancel").disabled = configBusy;
    byId("api-config-save").textContent = configBusy ? "保存中…" : "保存配置";
  }

  function renderConfigs() {
    const config = selected();
    byId("api-config-list").innerHTML = configs.map((item) => `
      <button type="button" class="api-config-item ${item.id === selectedId ? "selected" : ""}" data-config-id="${escape(item.id)}" aria-pressed="${item.id === selectedId}">
        <strong>${escape(item.name)}</strong><span>${escape(item.api_model)}</span>
        <small>${item.stream ? "流式" : "完整响应"} · 间隔 ${escape(item.interval_minutes)} 分钟</small>
      </button>`).join("");
    byId("api-config-empty").hidden = configs.length > 0;
    byId("api-config-count").textContent = `${configs.length} 组配置`;
    byId("api-selected-name").textContent = config?.name || "先创建一组 API 配置";
    byId("api-selected-meta").textContent = config
      ? `${config.api_model} · ${config.stream ? "流式输出" : "完整响应"} · 间隔 ${config.interval_minutes} 分钟`
      : "保存接口与模型，之后可直接复用。";
    byId("api-selected-url").textContent = config?.base_url || "";
    const filter = byId("api-history-filter");
    const previous = filter.value;
    filter.innerHTML = '<option value="">全部配置</option>' + configs.map((item) => `<option value="${escape(item.id)}">${escape(item.name)}</option>`).join("");
    filter.value = configs.some((item) => item.id === previous) ? previous : "";
    controls();
  }

  async function loadConfigs() {
    const payload = await request("/api/test/configs");
    configs = payload.configs;
    if (!configs.some((item) => item.id === selectedId)) selectedId = configs[0]?.id || null;
    byId("api-key-storage").textContent = "配置与密钥保存在本机，历史记录在重启后仍保留。";
    renderConfigs();
  }

  function editConfig(config = null) {
    editingId = config?.id || null;
    byId("api-config-form").reset();
    byId("api-config-title").textContent = config ? "编辑 API 配置" : "新建 API 配置";
    for (const [field, key] of [["name", "name"], ["base", "base_url"], ["model", "api_model"], ["temperature", "temperature"]]) {
      byId(`config-${field}`).value = config?.[key] ?? "";
    }
    byId("config-stream").checked = config?.stream ?? true;
    byId("config-interval").value = config?.interval_minutes ?? 60;
    byId("config-key").required = !config;
    byId("config-key").placeholder = config ? "留空保留已保存的密钥" : "填写 API Key";
    byId("api-config-error").textContent = "";
    dialog.showModal();
    byId("config-name").focus();
  }

  async function saveConfig(event) {
    event.preventDefault();
    if (configBusy) return;
    configBusy = true;
    controls();
    byId("api-config-error").textContent = "";
    try {
      const values = {
        name: byId("config-name").value.trim(), base_url: byId("config-base").value.trim(),
        api_model: byId("config-model").value.trim(), api_key: byId("config-key").value,
        temperature: byId("config-temperature").value === "" ? null : Number(byId("config-temperature").value),
        stream: byId("config-stream").checked, interval_minutes: Number(byId("config-interval").value),
      };
      const payload = await request(`/api/test/configs${editingId ? `/${encodeURIComponent(editingId)}` : ""}`, jsonRequest(editingId ? "PATCH" : "POST", values));
      selectedId = payload.config.id;
      byId("config-key").value = "";
      dialog.close();
      await loadConfigs();
      message("配置已保存。正在运行的定时任务仍使用启动时的配置。", false);
      await loadHistory();
    } catch (error) {
      if (dialog.open) byId("api-config-error").textContent = error.message;
      else message(error.message);
    } finally {
      configBusy = false;
      controls();
    }
  }

  function renderSchedule() {
    if (!schedule) return;
    const labels = { stopped: "定时未开启", running: "正在测试", waiting: "等待下一轮", stopping: "正在停止，等待当前请求结束" };
    byId("api-schedule-status").textContent = labels[schedule.state] || schedule.state;
    byId("api-schedule-status").dataset.state = schedule.state;
    byId("api-schedule-meta").textContent = schedule.state === "stopped"
      ? "开启后立即运行首轮，每轮结束后再按配置间隔计时。"
      : `${schedule.config_name} · 间隔 ${schedule.interval_minutes} 分钟${schedule.next_run_at ? ` · 下次 ${date(schedule.next_run_at)}` : ""}`;
    controls();
  }

  async function loadSchedule() {
    const version = scheduleVersion;
    try {
      const value = await request("/api/test/schedule");
      if (version !== scheduleVersion || scheduleBusy) return;
      schedule = value;
      renderSchedule();
    } catch (error) {
      if (version === scheduleVersion) byId("api-schedule-status").textContent = `状态读取失败：${error.message}`;
    }
  }

  async function changeSchedule(method) {
    if (scheduleBusy || (method === "POST" && !selected())) return;
    scheduleBusy = true;
    scheduleVersion++;
    controls();
    message();
    try {
      schedule = await request("/api/test/schedule", method === "POST" ? jsonRequest(method, { config_id: selectedId }) : { method });
      renderSchedule();
    } catch (error) {
      message(error.message);
    } finally {
      scheduleBusy = false;
      controls();
      await Promise.all([loadSchedule(), loadHistory()]);
    }
  }

  async function loadHistory() {
    const version = ++historyVersion;
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (byId("api-history-filter").value) query.set("config_id", byId("api-history-filter").value);
    try {
      const history = await request(`/api/test/history?${query}`);
      if (version !== historyVersion) return;
      total = history.total;
      if (offset >= total && offset > 0) { offset = Math.max(0, Math.ceil(total / limit) - 1) * limit; return loadHistory(); }
      const contents = JSON.stringify(history.items);
      if (contents !== historyContents) {
        const focusedRun = document.activeElement?.dataset.runId;
        byId("api-history-body").innerHTML = history.items.map((run) => `
        <tr><td>${escape(date(run.started_at))}</td><td><strong>${escape(run.config_name)}</strong><small>${escape(run.api_model)}</small></td>
        <td>${run.source === "scheduled" ? "定时" : "手动"}</td><td><span class="api-status ${escape(run.status)}">${statusName[run.status] || escape(run.status)}</span></td>
        <td>${run.status === "success" ? `${escape(run.prediction)} · ${(run.probability * 100).toFixed(1)}%` : escape(run.error || "等待完成")}</td>
        <td><button class="button secondary small" type="button" data-run-id="${escape(run.id)}" aria-label="查看 ${escape(run.config_name)} ${escape(date(run.started_at))} 的测试详情">详情</button></td></tr>`).join("");
        historyContents = contents;
        if (focusedRun) byId("api-history-body").querySelector(`[data-run-id="${CSS.escape(focusedRun)}"]`)?.focus();
      }
      byId("api-history-empty").hidden = total > 0;
      byId("api-history-empty").textContent = byId("api-history-filter").value ? "此配置还没有测试记录。" : "暂无测试记录，选择配置后点击「立即测试」。";
      byId("api-history-table").hidden = total === 0;
      byId("api-history-page").textContent = total ? `${offset + 1}–${Math.min(offset + limit, total)} / 共 ${total} 条` : "共 0 条";
      byId("api-history-prev").disabled = offset === 0;
      byId("api-history-next").disabled = offset + limit >= total;
      byId("api-history-error").textContent = "";
    } catch (error) {
      if (version === historyVersion) byId("api-history-error").textContent = `历史读取失败：${error.message}`;
    }
  }

  async function showRun(id) {
    const version = ++detailVersion;
    const detail = byId("api-run-detail");
    detail.hidden = false;
    byId("api-detail-title").textContent = "正在读取测试详情…";
    byId("api-detail-summary").textContent = "";
    byId("api-detail-result").replaceChildren();
    try {
      const { run } = await request(`/api/test/history/${encodeURIComponent(id)}`);
      if (version !== detailVersion) return;
      byId("api-detail-title").textContent = `${run.config_name} · ${statusName[run.status] || run.status}`;
      byId("api-detail-summary").textContent = `${date(run.started_at)} · ${run.source === "scheduled" ? "定时" : "手动"} · ${run.api_model} · ${run.config_snapshot.stream ? "流式" : "完整响应"} · 温度 ${run.config_snapshot.temperature ?? "接口默认"}${run.config_snapshot.interval_minutes != null ? ` · 间隔 ${run.config_snapshot.interval_minutes} 分钟` : ""}${run.duration_seconds != null ? ` · 耗时 ${run.duration_seconds} 秒` : ""} · ${run.config_snapshot.base_url}`;
      if (run.result) renderResult(run.result, byId("api-detail-result"));
      else byId("api-detail-result").textContent = run.error || "本轮仍在测试，完成后可重新查看详情。";
      detail.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      if (version === detailVersion) byId("api-detail-title").textContent = error.message;
    }
  }

  async function runTest() {
    const config = selected();
    if (!config || runBusy) return;
    runBusy = true;
    controls();
    message();
    const states = Array(6).fill("pending");
    let accepted = 0;
    let attempt = 0;
    let body = "";
    byId("api-test-progress").hidden = false;
    byId("api-run-detail").hidden = true;
    byId("api-test-errors").replaceChildren();
    byId("api-stream-output").hidden = !config.stream;
    byId("api-stream-text").textContent = "等待上游输出…";
    byId("api-progress-status").textContent = `${config.name} · 准备测试`;
    const renderProgress = () => {
      const labels = { pending: "等待", working: "请求中", done: "有效", invalid: "数字不足", error: "接口失败", skipped: "无需调用" };
      byId("api-progress-count").textContent = `有效 ${accepted}/3 · 已尝试 ${attempt}/6`;
      byId("api-progress-fill").style.width = `${accepted / 3 * 100}%`;
      byId("api-progress-steps").innerHTML = states.map((state, index) => `<span class="progress-step ${state}"><b>${index + 1}</b>${labels[state]}</span>`).join("");
    };
    const errorItem = (text) => { const item = document.createElement("li"); item.textContent = text; byId("api-test-errors").append(item); };
    renderProgress();
    try {
      const response = await fetch("/api/test/runs", jsonRequest("POST", { config_id: config.id }));
      const final = await readTestEvents(response, (event) => {
        if (event.type === "challenge") {
          attempt = event.attempt;
          states[attempt - 1] = "working";
          body = "";
          byId("api-stream-text").textContent = "等待上游输出…";
          byId("api-progress-status").textContent = `${config.name} · 挑战 ${attempt}`;
        } else if (event.type === "delta") {
          body += event.text;
          byId("api-stream-text").textContent = body;
          byId("api-stream-title").textContent = `当前挑战输出 · ${body.length} 字符`;
        } else if (event.type === "challenge_result") {
          states[event.attempt - 1] = event.accepted ? "done" : "invalid";
          if (event.accepted) accepted++;
          else errorItem(`挑战 ${event.attempt}：有效数字不足 ${event.parsed_numbers}/${event.minimum_numbers}`);
        } else if (event.type === "challenge_error") {
          states[event.attempt - 1] = "error";
          errorItem(`挑战 ${event.attempt}：${event.error}`);
        }
        if (event.type !== "delta") renderProgress();
      });
      states.forEach((state, index) => { if (state === "pending") states[index] = "skipped"; });
      renderProgress();
      byId("api-progress-status").textContent = `${config.name} · 测试完成`;
      await showRun(final.run_id);
    } catch (error) {
      byId("api-progress-status").textContent = "测试未完成";
      message(error.message);
    } finally {
      runBusy = false;
      controls();
      await loadHistory();
    }
  }

  byId("api-new-config").addEventListener("click", () => editConfig());
  byId("api-edit-config").addEventListener("click", () => { if (selected()) editConfig(selected()); });
  byId("api-config-cancel").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => { if (configBusy) event.preventDefault(); });
  dialog.addEventListener("close", () => { byId("config-key").value = ""; });
  byId("api-config-form").addEventListener("submit", saveConfig);
  byId("api-config-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-config-id]");
    if (button) { selectedId = button.dataset.configId; renderConfigs(); }
  });
  byId("api-delete-config").addEventListener("click", async () => {
    const config = selected();
    if (!config || configBusy || !window.confirm(`删除配置“${config.name}”？已有测试历史会保留。`)) return;
    configBusy = true; controls();
    try {
      await request(`/api/test/configs/${encodeURIComponent(config.id)}`, { method: "DELETE" });
      await loadConfigs();
      offset = 0;
      await loadHistory();
      message("配置已删除，历史记录仍保留。", false);
    } catch (error) { message(error.message); }
    finally { configBusy = false; controls(); }
  });
  byId("api-run").addEventListener("click", runTest);
  byId("api-schedule-start").addEventListener("click", () => changeSchedule("POST"));
  byId("api-schedule-stop").addEventListener("click", () => changeSchedule("DELETE"));
  byId("api-history-filter").addEventListener("change", () => { offset = 0; loadHistory(); });
  byId("api-history-prev").addEventListener("click", () => { offset = Math.max(0, offset - limit); loadHistory(); });
  byId("api-history-next").addEventListener("click", () => { offset += limit; loadHistory(); });
  byId("api-history-refresh").addEventListener("click", () => loadHistory());
  byId("api-history-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-id]");
    if (button) showRun(button.dataset.runId);
  });
  byId("api-detail-close").addEventListener("click", () => { detailVersion++; byId("api-run-detail").hidden = true; });
  controls();
  loadConfigs().catch((error) => message(`配置读取失败：${error.message}`));
  // 页面只读取状态与历史；定时测试始终由服务端启动。
  async function poll() {
    await Promise.all([loadSchedule(), loadHistory()]);
    window.setTimeout(poll, 5000);
  }
  poll();
}
