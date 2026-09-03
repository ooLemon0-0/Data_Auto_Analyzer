const $ = (id) => document.getElementById(id);
const projectSelect = $("projectSelect");
const dateInput = $("dateInput");
const targetInput = $("targetInput");
const prepareBtn = $("prepareBtn");
const sinkAuthBtn = $("sinkAuthBtn");
const uploadBtn = $("uploadBtn");
const prevBtn = $("prevBtn");
const nextBtn = $("nextBtn");
const rotateBtn = $("rotateBtn");
const reviewImage = $("reviewImage");
const imageStage = document.querySelector(".image-stage");
const emptyState = $("emptyState");
const recognitionText = $("recognitionText");
const itemCounter = $("itemCounter");
const decisionBadge = $("decisionBadge");
const sourceKey = $("sourceKey");
const toast = $("toast");
const diagnosticBtn = $("diagnosticBtn");
const manualDiagnosticBtn = $("manualDiagnosticBtn");
let state = null;
let rotation = 0;
let renderedItemId = null;
let hasLoadedDiagnostics = false;
let diagnosticSearchMatches = [];
let projects = [];

function selectedProject() {
  return projects.find(project => project.id === projectSelect.value) || null;
}

function projectHasDiagnostics() {
  return Boolean(selectedProject()?.diagnostics_enabled);
}

function projectHasSink() {
  return Boolean(selectedProject()?.sink_enabled);
}

function wait(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

function remotePhaseLabel(phase) {
  return {
    starting_vpn: "启动 aTrust…",
    waiting_vpn: "恢复 aTrust 会话…",
    launching_rdp: "启动远程桌面…",
    waiting_proxy: "等待目标端口服务…",
  }[phase] || "准备远程连接…";
}

async function ensureProjectRemoteAccess(project) {
  const connectionId = project?.remote_connection_id;
  if (!connectionId) return;

  // A new click starts a new detection attempt. A prompt dismissed during the
  // previous attempt is therefore allowed to appear again.
  hideRemoteSetupPrompt();
  let status = await api("/api/remote-access/launch", {
    method: "POST",
    body: JSON.stringify({connection_id: connectionId}),
  });
  const deadline = Date.now() + 6 * 60 * 1000;
  let lastPhase = "";
  let remoteHintShown = false;

  while (Date.now() < deadline) {
    if (status.operator_action && !remoteHintShown) {
      showRemoteSetupPrompt(status.operator_action);
      remoteHintShown = true;
    }
    if (status.phase === "failed") {
      throw new Error(status.message || "远程连接启动失败");
    }
    if (
      status.phase === "ready"
      || (status.phase === "rdp_started" && !status.proxy_target)
    ) {
      markRemoteSetupReady(status);
      return;
    }
    prepareBtn.textContent = remotePhaseLabel(status.phase);
    if (status.phase !== lastPhase && status.message) {
      notify(status.message);
      lastPhase = status.phase;
    }
    await wait(1500);
    status = await api(`/api/remote-access/${encodeURIComponent(connectionId)}/status`);
  }
  throw new Error("等待 aTrust、远程桌面和 SocksOverRDP 就绪超时");
}

function applyProjectCapabilities() {
  const diagnosticsEnabled = projectHasDiagnostics();
  const sinkEnabled = projectHasSink();
  manualDiagnosticBtn.hidden = !diagnosticsEnabled;
  diagnosticBtn.hidden = !diagnosticsEnabled;
  diagnosticBtn.disabled = !diagnosticsEnabled || !state?.current;
  sinkAuthBtn.hidden = !sinkEnabled;
  uploadBtn.hidden = !sinkEnabled;
}

function todayLocal() {
  const d = new Date();
  const offset = d.getTimezoneOffset();
  return new Date(d.getTime() - offset * 60000).toISOString().slice(0, 10);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function notify(message, options = {}) {
  toast.textContent = message;
  toast.classList.toggle("error", Boolean(options.error));
  toast.classList.add("show");
  clearTimeout(notify.timer);
  if (options.persistent) {
    toast.title = "点击关闭";
    toast.onclick = () => toast.classList.remove("show");
    return;
  }
  toast.title = "";
  toast.onclick = null;
  notify.timer = setTimeout(() => toast.classList.remove("show"), 3200);
}

function showRemoteSetupPrompt(action) {
  $("remoteSetupTitle").textContent = action.title || "瑞丰连接未完成";
  $("remoteSetupMessage").textContent = action.message || "";
  $("remoteSetupRoute").textContent =
    `当前检测：RDP ${action.rdp_target || "—"} 可达；`
    + `本机代理 ${action.proxy_target || "—"} 尚未建立。`;
  $("remoteSetupCommand").textContent = action.command || "";
  $("remoteSetupInstructions").hidden = false;
  $("remoteSetupCopy").hidden = false;
  $("remoteSetupDismiss").textContent = "我已运行，继续检测";
  $("remoteSetupModal").hidden = false;
}

function markRemoteSetupReady(status) {
  const modal = $("remoteSetupModal");
  if (modal.hidden) return;
  $("remoteSetupTitle").textContent = "瑞丰远程通道已建立";
  $("remoteSetupMessage").textContent =
    "已检测到 SocksOverRDP 通道。提示将保持显示，关闭后可查看本次拉取结果。";
  $("remoteSetupRoute").textContent =
    `当前检测：RDP ${status.rdp_target || "—"} 可达；`
    + `本机代理 ${status.proxy_target || "—"} 已建立。`;
  $("remoteSetupInstructions").hidden = true;
  $("remoteSetupCopy").hidden = true;
  $("remoteSetupDismiss").textContent = "关闭并查看拉取结果";
}

function hideRemoteSetupPrompt() {
  $("remoteSetupModal").hidden = true;
}

async function copyRemoteSetupCommand() {
  const command = $("remoteSetupCommand").textContent;
  if (!command) return;
  try {
    await navigator.clipboard.writeText(command);
    notify("现场部署命令已复制");
  } catch (_) {
    window.prompt("请复制下面的现场部署命令", command);
  }
}

function requestReviewPrepare() {
  return api("/api/review/prepare", {
    method: "POST",
    body: JSON.stringify({
      project_id: projectSelect.value,
      business_date: dateInput.value,
      target_size: Number(targetInput.value),
    }),
  });
}

async function prepareWithRemoteRecovery(project) {
  await ensureProjectRemoteAccess(project);
  try {
    return await requestReviewPrepare();
  } catch (originalError) {
    const connectionId = project?.remote_connection_id;
    if (!connectionId) throw originalError;

    let remoteStatus;
    try {
      remoteStatus = await api(
        `/api/remote-access/${encodeURIComponent(connectionId)}/status`
      );
    } catch (_) {
      throw originalError;
    }
    if (remoteStatus.proxy_reachable !== false) throw originalError;

    notify("检测到远程桌面通道中断，正在自动重新连接并重试…");
    await ensureProjectRemoteAccess(project);
    prepareBtn.textContent = "重新拉取中…";
    return requestReviewPrepare();
  }
}

function decisionText(value) {
  return {correct: "正确", incorrect: "错误", invalid: "无效"}[value] || "未审核";
}

function fitRotatedImage() {
  if (!reviewImage.naturalWidth || !reviewImage.naturalHeight || !imageStage) return;
  const pad = 24;
  const stageW = Math.max(1, imageStage.clientWidth - pad);
  const stageH = Math.max(1, imageStage.clientHeight - pad);
  const swapped = Math.abs(rotation % 180) === 90;
  const visualW = swapped ? reviewImage.naturalHeight : reviewImage.naturalWidth;
  const visualH = swapped ? reviewImage.naturalWidth : reviewImage.naturalHeight;
  const scale = Math.min(stageW / visualW, stageH / visualH, 1);
  reviewImage.style.width = `${reviewImage.naturalWidth * scale}px`;
  reviewImage.style.height = `${reviewImage.naturalHeight * scale}px`;
  reviewImage.style.transform = `rotate(${rotation}deg)`;
}

function rotateImage() {
  if (!state?.current || !reviewImage.naturalWidth) return;
  rotation = (rotation + 90) % 360;
  fitRotatedImage();
}

function render(s) {
  state = s;

  $("progressStat").textContent =
    `${s.valid_count} / ${s.target_size}`;

  $("correctStat").textContent =
    s.correct;

  $("incorrectStat").textContent =
    s.incorrect;

  $("invalidStat").textContent =
    s.invalid;

  $("accuracyStat").textContent =
    s.accuracy == null
      ? "—"
      : `${(s.accuracy * 100).toFixed(1)}%`;

  // ============================================================
  // 上传按钮
  //
  // uploaded 只代表：
  //   “这个日期以前成功上传过”
  //
  // 不再代表：
  //   “禁止再次上传”
  //
  // 唯一禁止上传的条件：
  //   审核尚未完成
  // ============================================================

  uploadBtn.disabled = !s.complete || !projectHasSink();

  if (s.uploaded) {
    uploadBtn.textContent = "重新上传";
  } else {
    uploadBtn.textContent = "审核完成并上传";
  }

  const item = s.current;

  if (!item) {
    renderedItemId = null;
    rotation = 0;

    rotateBtn.disabled = true;
    diagnosticBtn.disabled = true;

    reviewImage.style.display =
      "none";

    emptyState.style.display =
      "block";

    recognitionText.textContent =
      "—";

    return;
  }

  if (
    renderedItemId
    !== item.item_id
  ) {
    renderedItemId =
      item.item_id;

    rotation = 0;

    reviewImage.style.transform =
      "rotate(0deg)";
  }

  rotateBtn.disabled = false;
  diagnosticBtn.disabled = !projectHasDiagnostics();

  emptyState.style.display =
    "none";

  reviewImage.style.display =
    "block";

  reviewImage.src =
    `${item.image_url}?t=${Date.now()}`;

  recognitionText.textContent =
    item.recognition_text
    || "(空识别结果)";

  sourceKey.textContent =
    `source: ${item.source_key}`;

  itemCounter.textContent =
    `队列第 ${item.seq} 条 · `
    +
    `有效进度 `
    +
    `${s.valid_count}/${s.target_size}`;

  decisionBadge.className =
    `badge ${item.decision || ""}`;

  decisionBadge.textContent =
    decisionText(
      item.decision
    );

  prevBtn.disabled =
    s.current_index <= 0;

  nextBtn.disabled =
    s.current_index
    >= s.entries.length - 1;
}

async function loadProjects() {
  projects = await api("/api/projects");
  projectSelect.innerHTML = projects.map(p => `<option value="${p.id}" data-target="${p.daily_target}">${p.name}</option>`).join("");
  if (projects.length) targetInput.value = projects[0].daily_target;
  applyProjectCapabilities();
}

projectSelect.addEventListener("change", () => {
  const option = projectSelect.selectedOptions[0];
  if (option) targetInput.value = option.dataset.target || 50;
  applyProjectCapabilities();
});

prepareBtn.addEventListener("click", async () => {
  prepareBtn.disabled = true;
  try {
    const project = selectedProject();
    prepareBtn.textContent = "拉取中…";
    const data = await prepareWithRemoteRecovery(project);
    render(data);
    notify(`已载入当天数据，目标有效样本 ${data.target_size} 条`);
  } catch (err) {
    notify(`拉取失败：${err.message}`, {error: true, persistent: true});
  } finally {
    prepareBtn.disabled = false;
    prepareBtn.textContent = "拉取并开始";
  }
});

sinkAuthBtn.addEventListener("click", async () => {
  sinkAuthBtn.disabled = true;
  sinkAuthBtn.textContent = "等待轻推验证…";
  notify("将打开轻推/WPS。若出现二维码，请完成一次手机验证；成功后会复用该 Chrome Profile。 ");
  try {
    const data = await api("/api/sink/auth", {
      method: "POST",
      body: JSON.stringify({project_id: projectSelect.value}),
    });
    notify(data.authenticated ? "轻推/WPS 登录验证已完成，会话已保存" : "未确认到轻推登录状态");
  } catch (err) {
    notify(err.message);
  } finally {
    sinkAuthBtn.disabled = false;
    sinkAuthBtn.textContent = "轻推登录 / 验证";
  }
});

async function submitDecision(decision) {
  if (!state?.current) return;
  try {
    const data = await api("/api/review/decision", {
      method: "POST",
      body: JSON.stringify({
        project_id: state.project_id,
        business_date: state.business_date,
        queue_id: state.current.queue_id,
        decision,
      }),
    });
    render(data);
    if (decision === "invalid") notify("已标记无效，并自动补抽 1 条");
    if (data.complete) notify("今日有效样本审核完成，可以上传统计结果");
  } catch (err) { notify(err.message); }
}

document.querySelectorAll("[data-decision]").forEach(btn => {
  btn.addEventListener("click", () => submitDecision(btn.dataset.decision));
});

async function navigate(direction) {
  if (!state?.current) return;
  try {
    const data = await api("/api/review/navigate", {
      method: "POST",
      body: JSON.stringify({
        project_id: state.project_id,
        business_date: state.business_date,
        queue_id: state.current.queue_id,
        direction,
      }),
    });
    render(data);
  } catch (err) { notify(err.message); }
}

prevBtn.addEventListener("click", () => navigate("previous"));
nextBtn.addEventListener("click", () => navigate("next"));
rotateBtn.addEventListener("click", rotateImage);
reviewImage.addEventListener("load", fitRotatedImage);
window.addEventListener("resize", fitRotatedImage);

function escapeHtml(value) {
  return String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function drawDiagnosticOverlays(payload) {
  const img = $("diagnosticImage"), svg = $("diagnosticOverlay");
  const width = payload.image_width || img.naturalWidth, height = payload.image_height || img.naturalHeight;
  if (!width || !height) return;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = (payload.overlays || []).map(overlay => {
    if (overlay.role === "deskew") {
      const labelWidth = Math.max(420, overlay.label.length * 32);
      return `<g class="overlay-deskew ${escapeHtml(overlay.state || "unknown")}"><rect x="24" y="24" width="${labelWidth}" height="64" rx="12"/><text x="44" y="67">${escapeHtml(overlay.label)}</text></g>`;
    }
    if (overlay.box) {
      const b = overlay.box;
      return `<g><rect class="overlay-surface" x="${b.x1}" y="${b.y1}" width="${b.x2-b.x1}" height="${b.y2-b.y1}"/><text class="overlay-label" x="${b.x1+4}" y="${Math.max(16,b.y1-5)}">${escapeHtml(overlay.label)}</text></g>`;
    }
    const polygonPoints = overlay.polygon?.points || [];
    const points = polygonPoints.map(p => `${p.x},${p.y}`).join(" ");
    const labelX = polygonPoints.length ? Math.min(...polygonPoints.map(p => p.x)) : 0;
    const labelY = polygonPoints.length ? Math.min(...polygonPoints.map(p => p.y)) : 0;
    const boxY = Math.max(96, labelY - 58);
    const labelWidth = Math.max(460, overlay.label.length * 16);
    return `<g class="overlay-det-group ${escapeHtml(overlay.state || "unknown")}"><polygon class="overlay-det" points="${points}"/>${polygonPoints.length ? `<rect class="overlay-label-bg" x="${labelX}" y="${boxY-38}" width="${labelWidth}" height="48" rx="8"/><text class="overlay-label" x="${labelX+12}" y="${boxY-5}">${escapeHtml(overlay.label)}</text>` : ""}</g>`;
  }).join("");
}

function diagnosticEventText(event) {
  return event?.details?.ocr?.joined_text || "(空识别结果)";
}

function diagnosticEventTime(event) {
  const value = event?.trigger_time || event?.start_time || event?.finish_time;
  return value ? value.replace("T", " ").slice(0, 19) : "时间未知";
}

function renderDiagnosticPayload(payload, sourceName = "") {
  const event = payload.result?.event;
  $("rawLog").textContent = event?.raw_log || "";
  $("diagnosticMatchMeta").innerHTML = [
    sourceName ? `<span>日志源：${escapeHtml(sourceName)}</span>` : "",
    `<span>时间：${escapeHtml(diagnosticEventTime(event))}</span>`,
    `<span>识别：${escapeHtml(diagnosticEventText(event))}</span>`,
    event?.image_name ? `<span>图片：${escapeHtml(event.image_name)}</span>` : "",
  ].filter(Boolean).join("");

  const img = $("diagnosticImage");
  const visual = $("diagnosticVisual");
  const originalImageUrl = event?.image_url;
  $("diagnosticOverlay").innerHTML = "";
  $("diagnosticLoading").hidden = true;
  $("diagnosticContent").hidden = false;
  $("diagnosticError").hidden = true;

  if (!originalImageUrl) {
    visual.hidden = true;
    $("diagnosticMatchMeta").insertAdjacentHTML(
      "beforeend",
      '<span class="diagnostic-inline-warning">该日志事件没有原图 URL，仅展示原始日志</span>',
    );
    return;
  }

  visual.hidden = false;
  img.onload = () => drawDiagnosticOverlays(payload);
  img.onerror = () => {
    visual.hidden = true;
    $("diagnosticMatchMeta").insertAdjacentHTML(
      "beforeend",
      `<span class="diagnostic-inline-warning">原图加载失败：${escapeHtml(originalImageUrl)}</span>`,
    );
  };
  img.src = originalImageUrl;
}

function setDiagnosticLoading(title, hint = "") {
  $("diagnosticLoading").hidden = false;
  $("diagnosticLoadingTitle").textContent = title;
  $("diagnosticLoadingHint").textContent = hint;
  $("diagnosticError").hidden = true;
  $("diagnosticContent").hidden = true;
}

async function openDiagnostics() {
  if (!state?.current) return;
  const modal = $("diagnosticModal");
  modal.hidden = false;
  $("diagnosticTitle").textContent = "日志诊断";
  $("diagnosticSearchForm").hidden = true;
  $("diagnosticSearchResults").hidden = true;
  setDiagnosticLoading(
    hasLoadedDiagnostics ? "正在读取日志诊断…" : "首次加载需要连接现场并读取日志",
    hasLoadedDiagnostics ? "邻近时间的日志通常会直接使用缓存" : "第一次加载可能稍慢，请稍候",
  );
  try {
    const payload = await api(`/api/diagnostics/items/${state.current.item_id}`, {method:"POST", body:JSON.stringify({project_id:state.project_id, station:state.current.metadata?.station || null})});
    if (!payload.result.matched) throw new Error((payload.result.warnings || ["没有匹配事件"]).join("；"));
    renderDiagnosticPayload(payload);
  } catch (err) {
    $("diagnosticLoading").hidden = true; $("diagnosticError").hidden = false; $("diagnosticError").textContent = err.message;
  } finally {
    hasLoadedDiagnostics = true;
  }
}

function toDateTimeLocal(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 19);
}

function openManualDiagnostics() {
  const now = new Date();
  const start = new Date(now.getTime() - 5 * 60 * 1000);
  $("diagnosticModal").hidden = false;
  $("diagnosticTitle").textContent = "查日志";
  $("diagnosticSearchForm").hidden = false;
  $("diagnosticSearchResults").hidden = true;
  $("diagnosticLoading").hidden = true;
  $("diagnosticError").hidden = true;
  $("diagnosticContent").hidden = true;
  $("diagnosticStartTime").value = toDateTimeLocal(start);
  $("diagnosticEndTime").value = toDateTimeLocal(now);
  $("diagnosticExpectedResult").value = "";
  $("diagnosticImageName").value = "";
  diagnosticSearchMatches = [];
}

function showDiagnosticSearchMatch(index) {
  const match = diagnosticSearchMatches[index];
  if (!match) return;
  document.querySelectorAll(".diagnostic-result-item").forEach((button, buttonIndex) => {
    button.classList.toggle("active", buttonIndex === index);
  });
  renderDiagnosticPayload(match.payload, match.source_name);
}

$("diagnosticSearchForm").addEventListener("submit", async event => {
  event.preventDefault();
  const searchButton = $("diagnosticSearchBtn");
  searchButton.disabled = true;
  searchButton.textContent = "查询中…";
  $("diagnosticSearchResults").hidden = true;
  setDiagnosticLoading("正在查询主、备用服务器日志…", "时间段越长，读取时间可能越久");
  try {
    const result = await api("/api/diagnostics/search", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectSelect.value,
        start_time: $("diagnosticStartTime").value,
        end_time: $("diagnosticEndTime").value,
        expected_result: $("diagnosticExpectedResult").value.trim() || null,
        image_name: $("diagnosticImageName").value.trim() || null,
      }),
    });
    diagnosticSearchMatches = result.matches || [];
    $("diagnosticLoading").hidden = true;
    $("diagnosticSearchResults").hidden = false;
    const warnings = (result.warnings || []).join("；");
    $("diagnosticSearchSummary").textContent = diagnosticSearchMatches.length
      ? `找到 ${diagnosticSearchMatches.length} 条匹配日志${warnings ? `；${warnings}` : ""}`
      : warnings || "没有找到匹配日志";
    $("diagnosticResultList").innerHTML = diagnosticSearchMatches.map((match, index) => {
      const eventData = match.payload.result.event;
      return `<button class="diagnostic-result-item" type="button" data-index="${index}">
        <strong>${escapeHtml(diagnosticEventText(eventData))}</strong>
        <span>${escapeHtml(diagnosticEventTime(eventData))}</span>
        <small>${escapeHtml(match.source_name)}${eventData?.image_name ? ` · ${escapeHtml(eventData.image_name)}` : ""}</small>
      </button>`;
    }).join("");
    document.querySelectorAll(".diagnostic-result-item").forEach(button => {
      button.addEventListener("click", () => showDiagnosticSearchMatch(Number(button.dataset.index)));
    });
    if (diagnosticSearchMatches.length) showDiagnosticSearchMatch(0);
    else {
      $("diagnosticContent").hidden = true;
      $("diagnosticError").hidden = false;
      $("diagnosticError").textContent = warnings || "没有找到匹配日志";
    }
  } catch (err) {
    $("diagnosticLoading").hidden = true;
    $("diagnosticError").hidden = false;
    $("diagnosticError").textContent = err.message;
  } finally {
    searchButton.disabled = false;
    searchButton.textContent = "查询";
    hasLoadedDiagnostics = true;
  }
});

diagnosticBtn.addEventListener("click", openDiagnostics);
manualDiagnosticBtn.addEventListener("click", openManualDiagnostics);
$("diagnosticClose").addEventListener("click", () => $("diagnosticModal").hidden = true);
$("diagnosticModal").addEventListener("click", e => { if (e.target === $("diagnosticModal")) $("diagnosticModal").hidden = true; });
$("copyLogBtn").addEventListener("click", async e => { e.preventDefault(); try { await navigator.clipboard.writeText($("rawLog").textContent); notify("日志已复制"); } catch (_) { notify("当前浏览器不支持自动复制"); } });
$("remoteSetupCopy").addEventListener("click", copyRemoteSetupCommand);
$("remoteSetupClose").addEventListener("click", hideRemoteSetupPrompt);
$("remoteSetupDismiss").addEventListener("click", hideRemoteSetupPrompt);

uploadBtn.addEventListener(
  "click",
  async () => {

    if (!state) {
      return;
    }

    if (!state.complete) {
      notify(
        "当前审核尚未完成，不能上传"
      );
      return;
    }

    // 防止一次点击过程中重复点击，
    // 这里只是“上传进行中”的临时禁用。
    uploadBtn.disabled = true;

    const oldText =
      uploadBtn.textContent;

    uploadBtn.textContent =
      "正在同步…";

    try {

      const data = await api(
        "/api/review/upload",
        {
          method: "POST",

          body: JSON.stringify({
            project_id:
              state.project_id,

            business_date:
              state.business_date,
          }),
        }
      );

      if (data.uploaded) {

        // ==================================================
        // 上传成功只记录状态，
        // 不再永久 disabled。
        // ==================================================

        state.uploaded = true;

        notify(
          data.replaced_existing
            ? "上传完成，已覆盖该日期原有记录"
            : "上传完成"
        );

        // 重新调用 render。
        //
        // 因为：
        // state.uploaded = true
        //
        // 所以按钮会变成：
        //
        //   重新上传
        //
        // 但不会 disabled。
        render(state);

      } else {

        notify(
          `已落盘：${data.local_result}`
        );

        uploadBtn.disabled =
          false;

        uploadBtn.textContent =
          oldText;
      }

    } catch (err) {

      notify(
        err.message
      );

      // 上传失败后恢复按钮。
      uploadBtn.disabled =
        !state.complete;

      uploadBtn.textContent =
        state.uploaded
          ? "重新上传"
          : "审核完成并上传";
    }
  }
);

document.addEventListener("keydown", (event) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if (event.key === "1") submitDecision("correct");
  if (event.key === "2") submitDecision("incorrect");
  if (event.key === "3") submitDecision("invalid");
  if (event.key.toLowerCase() === "r") rotateImage();
  if (event.key === "ArrowLeft") navigate("previous");
  if (event.key === "ArrowRight") navigate("next");
});

dateInput.value = todayLocal();
loadProjects().catch(err => notify(err.message));
