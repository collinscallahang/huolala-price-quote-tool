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
  $("message").textContent = message || "";
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
  $("siteUrl").value = config.default_url || "";
  $("inputDir").textContent = config.default_input_dir || "-";
  $("outputRoot").textContent = config.output_root || "-";
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

function formDataBase() {
  const data = new FormData();
  data.append("site_url", $("siteUrl").value);
  return data;
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
  $("status").textContent = data.status || "未知";
  $("successTasks").textContent = data.success_tasks || 0;
  $("failedTasks").textContent = data.failed_tasks || 0;
  $("currentTask").textContent = data.current_task || "-";
  $("message").textContent = data.message || "";
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
    showNotice(`打开专用 Edge 失败：${error.message}`, "error");
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
    showNotice(error.message, "error");
  } finally {
    setBusy("createRun", false);
  }
});

$("files").addEventListener("change", () => {
  const files = Array.from($("files").files || []).map((file) => file.name);
  showNotice(files.length ? `已选择 ${files.length} 个 Excel：${files.join("、")}` : "");
});

$("createFromInput").addEventListener("click", async () => {
  setBusy("createFromInput", true);
  showNotice("正在读取 input 目录并创建任务。");
  try {
    const data = formDataBase();
    data.append("retry_count", $("retryCount").value || "2");
    data.append("overwrite", $("overwrite").checked ? "true" : "false");
    const result = await postForm("/api/runs/from-input", data);
    state.runId = result.run_id;
    renderStatus(result);
    showNotice(`任务已创建，共 ${result.total_tasks || 0} 个查价项。`, "success");
  } catch (error) {
    showNotice(error.message, "error");
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
    showNotice(error.message, "error");
  }
});

$("pauseRun").addEventListener("click", async () => {
  try {
    renderStatus(await postJson(`/api/runs/${state.runId}/pause`));
    showNotice("已暂停。");
  } catch (error) {
    showNotice(error.message, "error");
  }
});
$("resumeRun").addEventListener("click", async () => {
  try {
    renderStatus(await postJson(`/api/runs/${state.runId}/resume`));
    showNotice("已继续。");
  } catch (error) {
    showNotice(error.message, "error");
  }
});
$("stopRun").addEventListener("click", async () => {
  try {
    renderStatus(await postJson(`/api/runs/${state.runId}/stop`));
    showNotice("正在停止。");
  } catch (error) {
    showNotice(error.message, "error");
  }
});

loadConfig().catch((error) => {
  $("serviceState").textContent = "配置读取失败";
  showNotice(error.message, "error");
});
