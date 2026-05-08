const state = {
  runId: null,
  timer: null,
};

const $ = (id) => document.getElementById(id);

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
  try {
    const data = formDataBase();
    const result = await postForm("/api/browser/open", data);
    $("message").textContent = result.message;
  } catch (error) {
    $("message").textContent = error.message;
  }
});

$("runForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = formDataBase();
    if (!$("files").files.length) {
      throw new Error("请先选择 Excel 文件，或点击“使用 input 目录创建任务”。");
    }
    for (const file of $("files").files) {
      data.append("files", file);
    }
    data.append("retry_count", $("retryCount").value || "2");
    data.append("overwrite", $("overwrite").checked ? "true" : "false");
    const result = await postForm("/api/runs", data);
    state.runId = result.run_id;
    renderStatus(result);
  } catch (error) {
    $("message").textContent = error.message;
  }
});

$("createFromInput").addEventListener("click", async () => {
  try {
    const data = formDataBase();
    data.append("retry_count", $("retryCount").value || "2");
    data.append("overwrite", $("overwrite").checked ? "true" : "false");
    const result = await postForm("/api/runs/from-input", data);
    state.runId = result.run_id;
    renderStatus(result);
  } catch (error) {
    $("message").textContent = error.message;
  }
});

$("startRun").addEventListener("click", async () => {
  const result = await postJson(`/api/runs/${state.runId}/start`);
  renderStatus(result);
  if (!state.timer) {
    state.timer = setInterval(pollStatus, 1000);
  }
});

$("pauseRun").addEventListener("click", async () => renderStatus(await postJson(`/api/runs/${state.runId}/pause`)));
$("resumeRun").addEventListener("click", async () => renderStatus(await postJson(`/api/runs/${state.runId}/resume`)));
$("stopRun").addEventListener("click", async () => renderStatus(await postJson(`/api/runs/${state.runId}/stop`)));

loadConfig().catch((error) => {
  $("serviceState").textContent = "配置读取失败";
  $("message").textContent = error.message;
});
