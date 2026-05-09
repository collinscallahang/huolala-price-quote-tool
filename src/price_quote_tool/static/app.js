const state = {
  runId: null,
  timer: null,
};

const $ = (id) => document.getElementById(id);

function showNotice(message, type = "") {
  const notice = $("actionNotice");
  if (notice) {
    notice.textContent = message || "";
    notice.className = `notice ${message ? "show" : ""} ${type}`.trim();
  }
}

function userMessage(rawMessage) {
  const message = String(rawMessage || "").trim();
  if (!message) return "";
  if (message.includes("BrowserType.launch_persistent_context") || message.includes("Target page, context or browser has been closed")) {
    return "专用 Edge 启动失败，请先关闭正在运行的专用 Edge 窗口后重试。";
  }
  if (message === "Not Found" || message.includes("404")) {
    return "当前后台服务还是旧版本，请关闭正在运行的启动窗口后，重新双击“打开查价工具网页.hta”。";
  }
  if (message.includes("Timed out") || message.includes("Timeout")) {
    return "等待页面响应超时，请检查网页是否已打开并完成登录。";
  }
  if (message.includes("地址需人工确认")) {
    return message.split(/\r?\n/)[0];
  }
  return message.split(/\r?\n/)[0].slice(0, 120);
}

function displayStatus(status) {
  const labels = {
    created: "已创建",
    running: "运行中",
    paused: "已暂停",
    stopping: "正在停止",
    stopped: "已停止",
    completed: "已完成",
    failed: "失败",
  };
  return labels[status] || status || "未知";
}

function setBusy(buttonId, busy) {
  const button = $(buttonId);
  if (button) {
    button.disabled = busy;
  }
}

async function loadConfig() {
  const response = await fetch("/api/config");
  const config = await response.json();
  $("serviceState").textContent = `本地服务已连接${config.app_version ? ` · v${config.app_version}` : ""}`;
  $("siteUrl").value = config.default_url || "";
  $("inputDirPath").value = config.default_input_dir || "input";
  $("outputRootPath").value = config.output_root || "output";
  if (!config.default_input_dir || !config.output_root) {
    showNotice("当前后台服务可能还是旧版本，请关闭服务窗口后重新双击“打开查价工具网页.hta”。", "error");
  }
  await loadInputFiles();
}

async function loadInputFiles() {
  const response = await fetch("/api/input-files");
  const data = await response.json();
  const files = data.files || [];
  $("inputFiles").textContent = files.length ? files.join("、") : "未找到 .xlsx";
}

async function postForm(url, formData) {
  const response = await fetch(url, { method: "POST", body: formData });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || response.statusText);
  }
  return response.json();
}

async function postJson(url) {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || response.statusText);
  }
  return response.json();
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || response.statusText);
  }
  return response.json();
}

function formDataBase() {
  const data = new FormData();
  data.append("site_url", $("siteUrl").value);
  return data;
}

async function chooseFolder(targetId) {
  try {
    const result = await getJson("/api/folder/select");
    if (result.path) {
      $(targetId).value = result.path;
      showNotice("已选择文件夹，保存后生效。");
      return;
    }
  } catch (error) {
    if (!String(error.message || "").includes("Not Found") && !String(error.message || "").includes("404")) {
      showNotice(userMessage(error.message), "error");
    }
  }

  const current = $(targetId).value || "";
  const typedPath = window.prompt("请输入或粘贴文件夹完整路径，例如 D:\\批量查价工具\\input", current);
  if (typedPath && typedPath.trim()) {
    $(targetId).value = typedPath.trim();
    showNotice("已填写文件夹路径，点击“保存目录设置”后生效。");
  } else {
    showNotice("没有选择文件夹。");
  }
}

async function savePathSettings() {
  const data = new FormData();
  data.append("default_input_dir", $("inputDirPath").value.trim());
  data.append("output_root", $("outputRootPath").value.trim());
  const result = await postForm("/api/config/paths", data);
  $("inputDirPath").value = result.default_input_dir || $("inputDirPath").value;
  $("outputRootPath").value = result.output_root || $("outputRootPath").value;
  await loadInputFiles();
  showNotice(result.message || "目录设置已保存。", "success");
}

function updateButtons(status) {
  const hasRun = Boolean(state.runId);
  $("startRun").disabled = !hasRun || ["running", "completed", "stopped"].includes(status);
  $("pauseRun").disabled = !hasRun || status !== "running";
  $("resumeRun").disabled = !hasRun || status !== "paused";
  $("stopRun").disabled = !hasRun || !["running", "paused"].includes(status);
}

function renderStatus(data) {
  const total = data.total_tasks || 0;
  const completed = data.completed_tasks || 0;
  $("status").textContent = displayStatus(data.status);
  $("successTasks").textContent = data.success_tasks || 0;
  $("failedTasks").textContent = data.failed_tasks || 0;
  $("currentTask").textContent = data.current_task || "-";
  $("message").textContent = userMessage(data.message);
  $("progressText").textContent = `${completed} / ${total}`;
  $("progress").value = total ? Math.round((completed / total) * 100) : 0;
  updateButtons(data.status);

  const downloads = $("downloads");
  downloads.innerHTML = "";
  for (const file of data.result_files || []) {
    const name = file.split(/[\\/]/).pop();
    const link = document.createElement("a");
    link.href = `/api/runs/${data.run_id}/download/${encodeURIComponent(name)}`;
    link.textContent = name;
    downloads.appendChild(link);
  }
}

async function pollStatus() {
  if (!state.runId) return;
  const response = await fetch(`/api/runs/${state.runId}/status`);
  const data = await response.json();
  renderStatus(data);
  if (["completed", "failed", "stopped"].includes(data.status)) {
    clearInterval(state.timer);
    state.timer = null;
  }
}

$("openBrowser").addEventListener("click", async () => {
  setBusy("openBrowser", true);
  showNotice("正在打开专用 Edge，请稍等。");
  try {
    const data = formDataBase();
    const result = await postForm("/api/browser/open", data);
    showNotice(result.message || "专用 Edge 已打开。", "success");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  } finally {
    setBusy("openBrowser", false);
  }
});

$("runForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy("createRun", true);
  showNotice("正在上传 Excel 并创建任务。");
  try {
    const data = formDataBase();
    if (!$("files").files.length) {
      throw new Error("请先在网页里选择一个或多个 Excel 文件。input 目录只是备用入口。");
    }
    for (const file of $("files").files) {
      data.append("files", file);
    }
    data.append("retry_count", $("retryCount").value || "2");
    data.append("overwrite", $("overwrite").checked ? "true" : "false");
    const result = await postForm("/api/runs", data);
    state.runId = result.run_id;
    renderStatus(result);
    showNotice(`任务已创建，共 ${result.total_tasks || 0} 个查价项。`, "success");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  } finally {
    setBusy("createRun", false);
  }
});

$("files").addEventListener("change", () => {
  const files = Array.from($("files").files || []).map((file) => file.name);
  showNotice(files.length ? `已选择 ${files.length} 个 Excel：${files.join("、")}` : "");
});

$("chooseInputDir").addEventListener("click", async () => {
  setBusy("chooseInputDir", true);
  try {
    await chooseFolder("inputDirPath");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  } finally {
    setBusy("chooseInputDir", false);
  }
});

$("chooseOutputRoot").addEventListener("click", async () => {
  setBusy("chooseOutputRoot", true);
  try {
    await chooseFolder("outputRootPath");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  } finally {
    setBusy("chooseOutputRoot", false);
  }
});

$("savePaths").addEventListener("click", async () => {
  setBusy("savePaths", true);
  showNotice("正在保存目录设置。");
  try {
    await savePathSettings();
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  } finally {
    setBusy("savePaths", false);
  }
});

$("refreshInputFiles").addEventListener("click", async () => {
  setBusy("refreshInputFiles", true);
  try {
    await loadInputFiles();
    showNotice("备用目录文件已刷新。", "success");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  } finally {
    setBusy("refreshInputFiles", false);
  }
});

$("createFromInput").addEventListener("click", async () => {
  setBusy("createFromInput", true);
  showNotice("正在保存目录并读取备用 input 目录。");
  try {
    await savePathSettings();
    const data = formDataBase();
    data.append("retry_count", $("retryCount").value || "2");
    data.append("overwrite", $("overwrite").checked ? "true" : "false");
    const result = await postForm("/api/runs/from-input", data);
    state.runId = result.run_id;
    renderStatus(result);
    showNotice(`任务已创建，共 ${result.total_tasks || 0} 个查价项。`, "success");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  } finally {
    setBusy("createFromInput", false);
  }
});

$("startRun").addEventListener("click", async () => {
  try {
    showNotice("正在启动查价。");
    const result = await postJson(`/api/runs/${state.runId}/start`);
    renderStatus(result);
    showNotice("查价已启动。", "success");
    if (!state.timer) {
      state.timer = setInterval(pollStatus, 1000);
    }
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  }
});

$("pauseRun").addEventListener("click", async () => {
  try {
    renderStatus(await postJson(`/api/runs/${state.runId}/pause`));
    showNotice("已暂停。");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  }
});
$("resumeRun").addEventListener("click", async () => {
  try {
    renderStatus(await postJson(`/api/runs/${state.runId}/resume`));
    showNotice("已继续。");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  }
});
$("stopRun").addEventListener("click", async () => {
  try {
    renderStatus(await postJson(`/api/runs/${state.runId}/stop`));
    showNotice("正在停止。");
  } catch (error) {
    showNotice(userMessage(error.message), "error");
  }
});

loadConfig().catch((error) => {
  $("serviceState").textContent = "配置读取失败";
  showNotice(userMessage(error.message), "error");
});
