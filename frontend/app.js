import { createApi } from "./api.js";
import { loadConfig, loadRuntimeConfig } from "./config.js";
import { formatCoordinateInput, projectPosition, roundedPosition } from "./coordinates.js";

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
const aliases = text => [...new Set(text.split(/[,，、\n]/).map(v => v.trim()).filter(Boolean))];
const actionNames = { GRASP: "抓取", MOVE: "移动", PLACE: "放下", WAIT: "等待", RUN_TASK: "执行任务", STOP: "停止" };
const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "preview"]);
let config = loadConfig();
let api = null;
let scene = null;
let objects = [];
let placementPoints = [];
let tasks = [];
let selected = null;
let editingObjectId = null;
let editingPlacementId = null;
let registrationKind = "object";
let editingTaskId = null;
let steps = [];
const model = "transformer";
let parsed = null;
let connectionVersion = 0;
let parseVersion = 0;
let executionVersion = 0;
let pickVersion = 0;
let sceneVersion = 0;
let sceneTimer;
let cameraAbort;
let frameCount = 0;
let fpsStarted = performance.now();
let jobTimer;
let toastTimer;
let activeJob = null;
let jobRunning = false;
const pickingCanvas = document.createElement("canvas");
const pickingContext = pickingCanvas.getContext("2d", { willReadFrequently: true });

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000);
}

function report(error) { toast(error.message || "操作失败，请重试。", true); }
function bind(id, event, handler) {
  $(id).addEventListener(event, e => {
    try { Promise.resolve(handler(e)).catch(report); }
    catch (error) { report(error); }
  });
}
function requireApi() {
  if (!api) throw new Error("当前未连接，请在接口设置中重新连接。");
  return api;
}
function setConnection(status, label) {
  $("connection-status").className = `connection-pill ${status}`;
  $("connection-status").replaceChildren(document.createElement("i"), document.createTextNode(label));
}
function setActivity(text) { $("activity-text").textContent = text; }
function showRequest(value) {
  $("request-details").hidden = false;
  $("request-payload").textContent = JSON.stringify(value, null, 2);
}
function invalidateParsed() {
  parseVersion++;
  parsed = null;
  $("execute-button").disabled = true;
  $("parse-result").hidden = true;
  $("request-details").hidden = true;
}
function navigate(page) {
  const names = { workspace: "抓取工作台", objects: "物体库", placements: "放置点", tasks: "任务编排" };
  if (!(page in names)) page = "workspace";
  document.querySelectorAll("[data-page]").forEach(element => { element.hidden = element.dataset.page !== page; });
  document.querySelectorAll("[data-nav]").forEach(element => {
    element.classList.toggle("active", element.dataset.nav === page);
    if (element.dataset.nav === page) element.setAttribute("aria-current", "page");
    else element.removeAttribute("aria-current");
  });
  $("breadcrumb-page").textContent = names[page];
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("无法加载仿真画面，请检查图像接口。"));
    image.src = url;
  });
}

async function displayScene(nextScene, version, camera = sceneVersion, imageBlob = null) {
  if (!nextScene.frame_id || !nextScene.image_url || !Array.isArray(nextScene.objects)) {
    throw new Error("场景接口缺少 frame_id、image_url 或 objects。");
  }
  let image;
  if (imageBlob) {
    if (typeof createImageBitmap === "function") image = await createImageBitmap(imageBlob);
    else {
      const url = URL.createObjectURL(imageBlob);
      try { image = await loadImage(url); }
      finally { URL.revokeObjectURL(url); }
    }
  } else image = await loadImage(nextScene.image_url);
  if (imageBlob) await new Promise(requestAnimationFrame);
  if (version !== connectionVersion || camera !== sceneVersion) { image.close?.(); return; }
  scene = nextScene;
  const canvas = $("scene-image");
  if (canvas.width !== nextScene.width || canvas.height !== nextScene.height) {
    canvas.width = nextScene.width;
    canvas.height = nextScene.height;
  }
  canvas.getContext("2d", { alpha: false }).drawImage(image, 0, 0, canvas.width, canvas.height);
  image.close?.();
  $("camera-stage").style.aspectRatio = `${nextScene.width || 1280} / ${nextScene.height || 800}`;
  $("camera-stage").setAttribute("aria-busy", "false");
  $("scene-loading").hidden = true;
  if (config.mode === "preview") $("frame-caption").textContent = "原生仿真画面 · 静态预览";
  else {
    frameCount++;
    const now = performance.now();
    if (now - fpsStarted >= 1000) {
      $("frame-caption").textContent = `实时相机 · ${(frameCount * 1000 / (now - fpsStarted)).toFixed(1)} FPS`;
      frameCount = 0;
      fpsStarted = now;
    }
  }
  renderMarker();
  updateGraspControls();
}

async function connect() {
  const version = ++connectionVersion;
  executionVersion++;
  sceneVersion++;
  clearTimeout(sceneTimer);
  cameraAbort?.abort();
  frameCount = 0;
  fpsStarted = performance.now();
  clearTimeout(jobTimer);
  api = null;
  scene = null;
  objects = [];
  placementPoints = [];
  tasks = [];
  selected = null;
  activeJob = null;
  jobRunning = false;
  invalidateParsed();
  clearObjectEditor();
  renderObjects();
  renderPlacements();
  renderTasks();
  resetTask();
  $("parse-button").disabled = true;
  $("refresh-scene").disabled = true;
  $("reset-scene").disabled = true;
  $("clear-memory").disabled = true;
  $("grasp-test-result").hidden = true;
  $("connection-error").hidden = true;
  $("scene-loading").hidden = false;
  $("scene-loading").textContent = "正在加载仿真画面…";
  $("scene-markers").replaceChildren();
  $("camera-stage").setAttribute("aria-busy", "true");
  setConnection("", "正在连接");
  setActivity("正在初始化工作台…");
  const preview = config.mode === "preview";
  $("inference-source").textContent = preview ? "规则预览 · 未调用模型" : "后端模型接口";
  $("execute-button").querySelector("span").textContent = preview ? "预览执行请求" : "执行任务";
  $("workspace-mode-note").textContent = preview ? "静态交互预览 · 不执行物理抓取" : "实时物理仿真 · 编辑坐标后可直接试抓";
  $("storage-note").innerHTML = `<span class="tiny-dot"></span>${preview ? "预览数据保存在当前浏览器" : "物体与放置点保存到后端"}`;
  try {
    const nextApi = createApi(config);
    await nextApi.init();
    const [nextScene, nextObjects, nextTasks, nextPoints] = await Promise.all([
      nextApi.getScene(), nextApi.listObjects(), nextApi.listTasks(), nextApi.listPlacementPoints(),
    ]);
    if (version !== connectionVersion) return;
    if (!Array.isArray(nextObjects) || !Array.isArray(nextTasks) || !Array.isArray(nextPoints)) throw new Error("物体、放置点和任务接口应直接返回 JSON 数组。");
    if (preview) {
      const picking = await loadImage(nextScene.picking_url);
      if (version !== connectionVersion) return;
      pickingCanvas.width = picking.width;
      pickingCanvas.height = picking.height;
      pickingContext.drawImage(picking, 0, 0);
    }
    await displayScene(nextScene, version);
    if (version !== connectionVersion) return;
    api = nextApi;
    objects = nextObjects;
    placementPoints = nextPoints;
    tasks = nextTasks;
    renderObjects();
    renderPlacements();
    renderTasks();
    resetTask();
    const languageReady = preview || !Array.isArray(api.health?.capabilities) || api.health.capabilities.includes("language_parse");
    $("parse-button").disabled = !languageReady;
    $("inference-source").textContent = preview ? "规则预览 · 未调用模型" : languageReady ? "Transformer · 七类动作意图" : "模型尚未就绪";
    $("refresh-scene").disabled = false;
    $("reset-scene").disabled = false;
    $("clear-memory").disabled = false;
    updateGraspControls();
    setConnection(preview ? "" : "connected", preview ? "本地预览" : "后端已连接");
    setActivity(preview ? "静态预览就绪" : "仿真已启动，点选方块并编辑坐标后可试抓");
    if (!preview) void startCamera(version);
  } catch (error) {
    if (version !== connectionVersion) return;
    setConnection("error", "连接失败");
    $("connection-error").textContent = `${error.message} 可在“接口设置”中修改地址，或切换为本地预览。`;
    $("connection-error").hidden = false;
    $("scene-loading").textContent = "画面未连接，请检查接口设置";
    setActivity("工作台未连接");
  }
}

async function startCamera(version, camera = sceneVersion) {
  if (!api?.health?.capabilities?.includes("streaming_camera")) { scheduleScene(version, camera); return; }
  cameraAbort?.abort();
  const controller = new AbortController();
  cameraAbort = controller;
  try {
    for await (const frame of api.streamFrames(controller.signal)) {
      if (version !== connectionVersion || camera !== sceneVersion) break;
      await displayScene(frame.metadata, version, camera, frame.image);
      setConnection("connected", "后端已连接");
      $("connection-error").hidden = true;
    }
    if (!controller.signal.aborted && version === connectionVersion && camera === sceneVersion) throw new Error("相机数据流已结束，请刷新重连。");
  } catch (error) {
    if (controller.signal.aborted || version !== connectionVersion || camera !== sceneVersion) return;
    setConnection("error", "画面连接中断");
    $("connection-error").textContent = error.message;
    $("connection-error").hidden = false;
    scene = null;
    updateGraspControls();
  }
}

function scheduleScene(version, camera = sceneVersion) {
  sceneTimer = setTimeout(async () => {
    if (version !== connectionVersion || camera !== sceneVersion || !api) return;
    try {
      await displayScene(await api.getScene(), version, camera);
      if (version !== connectionVersion || camera !== sceneVersion) return;
      setConnection("connected", "后端已连接");
      $("connection-error").hidden = true;
      scheduleScene(version, camera);
    } catch (error) {
      if (version !== connectionVersion || camera !== sceneVersion) return;
      setConnection("error", "画面连接中断");
      $("connection-error").textContent = `${error.message} 当前保留最后一帧，点选已暂停，请点击刷新重连。`;
      $("connection-error").hidden = false;
      scene = null;
      updateGraspControls();
    }
  }, 125);
}

function updateGraspControls() {
  const blocked = !api || !scene || jobRunning || scene.is_running || scene.requires_reset || scene.held_object_id != null;
  $("test-grasp").disabled = registrationKind !== "object" || !selected || blocked;
  $("test-grasp").title = scene?.requires_reset ? "请先重置场景，再点选测试" : "使用输入框中当前坐标执行物理抓取";
}

function renderGraspResult(job) {
  if (job.mode === "program") {
    const names = { queued: "任务等待执行", running: "任务执行中", succeeded: "任务执行成功", failed: "任务执行失败", cancelled: "任务已停止" };
    const card = $("grasp-test-result");
    card.hidden = false;
    card.className = `grasp-test-result ${job.status}`;
    const results = job.step_results || [];
    card.innerHTML = `<div class="grasp-result-heading"><strong>${esc(names[job.status] || job.status)}</strong><span>${job.current_step || 0} / ${job.total_steps}</span></div><p>${esc(job.message || "")}</p>${results.length ? `<div class="grasp-metrics">${results.map(step => `<div><span>第 ${esc(step.step)} 步 · ${esc(actionNames[step.action] || "中断")}</span><strong>${esc(step.status === "succeeded" ? "已完成" : step.status === "cancelled" ? "已停止" : "失败")}</strong></div>`).join("")}</div>` : ""}`;
    return;
  }
  if (job.action !== "GRASP" && !job.scene_object_id && !job.result) return;
  const result = job.result;
  const names = { queued: "等待执行", running: "正在试抓", succeeded: "抓取成功", failed: "抓取失败", cancelled: "测试已停止" };
  const card = $("grasp-test-result");
  card.hidden = false;
  card.className = `grasp-test-result ${job.status}`;
  const measure = (value, scale, suffix) => Number.isFinite(value) ? `${(value * scale).toFixed(1)} ${suffix}` : "—";
  const metrics = result ? [
    ["坐标与中心误差", measure(result.coordinate_error_m, 1000, "mm")],
    ["夹爪到位误差", measure(result.end_effector_error_m, 1000, "mm")],
    [result.minimum_lift_m == null ? "最终抬升" : "最低抬升", measure(result.minimum_lift_m ?? result.final_lift_m, 100, "cm")],
    ["双指稳定夹持", result.held_seconds == null ? "未通过" : `${result.held_seconds} s`],
  ] : [];
  card.innerHTML = `<div class="grasp-result-heading"><strong>${esc(names[job.status] || job.status)}</strong><span>${job.current_step || 0} / ${job.total_steps || 5}</span></div><p>${esc(job.message || "")}</p>${metrics.length ? `<div class="grasp-metrics">${metrics.map(([label, value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>` : ""}`;
}

function clearObjectEditor() {
  pickVersion++;
  selected = null;
  editingObjectId = null;
  editingPlacementId = null;
  $("object-form").reset();
  for (const id of ["object-name", "object-aliases", "object-x", "object-y", "object-z", "save-object", "test-grasp"]) $(id).disabled = true;
  $("selection-title").textContent = "选择一个位置";
  $("selection-description").textContent = "点击画面任意位置采集坐标";
  $("selected-object").classList.remove("has-selection");
  $("selection-check").hidden = true;
  updateRegistrationLabels();
  document.querySelectorAll("[data-color]").forEach(button => button.classList.remove("selected"));
}

function updateRegistrationLabels() {
  const placement = registrationKind === "placement";
  $("registration-kind").value = registrationKind;
  $("target-name-label").textContent = placement ? "放置点名称" : "物体名称";
  $("target-alias-label").textContent = placement ? "放置点别名" : "物体别名";
  $("object-name").placeholder = placement ? "例如：放置区A" : "请输入你为物体取的名称";
  $("target-coordinate-label").textContent = placement ? "放置表面坐标" : "抓取坐标";
  $("save-target-label").textContent = placement ? "保存放置点" : "保存物体";
  $("target-help").textContent = placement ? "点击空白地面或方块顶部中央，名称和别名可用于放置指令。" : "多个别名使用逗号分隔，由你定义这个物体的名称。";
  $("camera-hint").textContent = placement ? "点击地面或方块顶部，采集放置表面坐标" : "点击任意位置，采集三维坐标";
  $("test-grasp").hidden = placement;
  $("object-editor-title").textContent = placement ? (editingPlacementId == null ? "放置点登记" : "编辑放置点") : (editingObjectId == null ? "物体登记" : "编辑物体");
  if (!selected) $("position-source").textContent = placement ? "支持地面和水平方块顶部；堆叠请点选顶部中央。" : "点选位置后显示坐标来源。";
}

function startRegistration(kind) {
  registrationKind = kind;
  clearObjectEditor();
  renderMarker();
  updateGraspControls();
}

async function pickObject(sceneObject, x, y, useSavedPosition = false) {
  const currentApi = requireApi();
  if (!scene) throw new Error("画面不可用，请先刷新场景。");
  const version = connectionVersion;
  const selectionVersion = ++pickVersion;
  const pick = await currentApi.pick({ frame_id: scene.frame_id, x, y,
    ...(sceneObject ? { scene_object_id: sceneObject.scene_object_id } : {}) });
  if (version !== connectionVersion || selectionVersion !== pickVersion) return;
  if (!Array.isArray(pick.grasp_position) || pick.grasp_position.length !== 3
    || !pick.grasp_position.every(Number.isFinite)) throw new Error("点选接口没有返回有效的物体和抓取坐标。");
  const registered = registrationKind === "placement" ? placementPoints.find(p => p.id === editingPlacementId)
    : objects.find(object => object.scene_object_id === pick.scene_object_id);
  fillObjectEditor(pick, registered, useSavedPosition);
}

function fillObjectEditor(pick, registered, useSavedPosition = false) {
  selected = { ...pick, color: pick.color || "point" };
  const placement = registrationKind === "placement";
  editingObjectId = placement ? null : registered?.id ?? null;
  editingPlacementId = placement ? registered?.id ?? null : null;
  for (const id of ["object-name", "object-aliases", "object-x", "object-y", "object-z", "save-object", "test-grasp"]) $(id).disabled = false;
  $("object-name").value = registered?.name || "";
  $("object-aliases").value = (registered?.aliases || []).join("，");
  const position = useSavedPosition && registered ? registered.position : placement ? pick.hit_position : pick.grasp_position;
  if (!Array.isArray(position) || position.length !== 3 || !position.every(Number.isFinite)) throw new Error("画面没有返回有效的表面坐标，请重新点选。");
  ["x", "y", "z"].forEach((axis, i) => { $(`object-${axis}`).value = Number(position[i]).toFixed(3); });
  selected.grasp_position = roundedPosition(position);
  if (useSavedPosition) selected.screen_position = projectPosition(position, scene?.camera) || pick.screen_position;
  $("selection-title").textContent = registered?.name || (placement ? "未命名放置点" : "未命名物体");
  $("selection-description").textContent = registered ? "已登记 · 可修改名称、别名和坐标" : "未登记 · 填写名称后保存";
  $("selection-check").hidden = false;
  $("selected-object").classList.add("has-selection");
  updateRegistrationLabels();
  const descriptions = {
    depth_surface_offset: "方块深度坐标，经表面偏移换算；记录精度 0.001 m。",
    depth_surface: "命中场景表面的深度坐标；记录精度 0.001 m。",
    ground_plane_projection: "地面或背景：视线投影到地面 Z=0；记录精度 0.001 m。",
    saved_position: "用户保存的坐标；记录精度 0.001 m。",
    work_plane_preview: "静态预览：投影到地面 Z=0，未测量实际表面。",
    object_center_preview: "静态预览：方块中心吸附，未进行实时测量。",
  };
  $("position-source").textContent = descriptions[pick.position_source] || "采集坐标保留三位小数，可手动修改。";
  if (placement) {
    $("position-source").textContent = "记录所点支撑表面的坐标，保留三位小数；支持地面和水平方块顶部，执行时验证支撑与稳定性。";
    if (pick.surface_normal && pick.surface_normal[2] < 0.996) $("position-source").textContent += " 当前命中侧面或倾斜面，请重新点选顶部中央。";
  }
  document.querySelectorAll("[data-color]").forEach(button => button.classList.toggle("selected", button.dataset.color === pick.color));
  renderMarker();
  updateGraspControls();
}

function renderMarker() {
  const point = selected?.screen_position || (selected && projectPosition(selected.grasp_position, scene?.camera));
  if (!point) { $("scene-markers").replaceChildren(); return; }
  let marker = $("scene-markers").firstElementChild;
  if (!marker) {
    marker = document.createElement("span");
    marker.className = "scene-marker";
    const label = document.createElement("span");
    label.className = "scene-marker-label";
    marker.append(label);
    $("scene-markers").append(marker);
  }
  marker.style.left = `${point[0] * 100}%`;
  marker.classList.toggle("placement", registrationKind === "placement");
  marker.style.top = `${point[1] * 100}%`;
  marker.firstElementChild.textContent = registrationKind === "placement"
    ? placementPoints.find(item => item.id === editingPlacementId)?.name || "放置点"
    : objects.find(item => item.id === editingObjectId)?.name || "采集点";
}

function readPosition(prefix) {
  return ["x", "y", "z"].map(axis => {
    const input = $(`${prefix}-${axis}`);
    if (input.value.trim() === "" || !Number.isFinite(input.valueAsNumber)) throw new Error("请填写三个有效的坐标数值。");
    formatCoordinateInput(input);
    return input.valueAsNumber;
  });
}

function readObject() {
  if (!selected) throw new Error("请先采集一个坐标。");
  const existing = objects.find(o => o.id === editingObjectId);
  return { ...(existing || {}), ...(editingObjectId == null ? {} : { id: editingObjectId }),
    name: $("object-name").value.trim(), aliases: aliases($("object-aliases").value),
    color: selected.color, scene_object_id: selected.scene_object_id,
    position: readPosition("object"), coordinate_frame: "world", skill_ids: existing?.skill_ids || ["cube_grasp"] };
}

function renderObjects() {
  $("object-count").textContent = objects.length;
  $("library-total").textContent = objects.length;
  $("alias-total").textContent = objects.reduce((sum, object) => sum + object.aliases.length, 0);
  const first = objects[0];
  $("instruction").placeholder = first ? `例如：帮我抓取${first.name}` : "请先点选并登记物体，再输入抓取指令";
  $("example-grasp").disabled = !first;
  $("example-grasp").dataset.example = first ? `帮我抓取${first.name}` : "";
  $("example-grasp").textContent = first ? `抓取${first.name} ↗` : "登记物体后生成示例";
  const alias = first?.aliases[0];
  $("example-alias").hidden = !alias;
  $("example-alias").dataset.example = alias ? `拿起${alias}` : "";
  $("example-alias").textContent = alias ? `拿起${alias} ↗` : "";
  const query = $("object-search").value.trim().toLowerCase();
  const filtered = objects.filter(object => [object.name, ...object.aliases].some(label => label.toLowerCase().includes(query)));
  $("objects-empty").hidden = filtered.length > 0;
  $("objects-empty").textContent = objects.length ? "没有匹配的物体，请调整搜索内容。" : "物体库为空。点击“登记物体”，在画面中点选目标并填写名称后保存。";
  $("object-table").innerHTML = filtered.map(object => `<tr>
    <td><div class="table-object"><span class="object-thumbnail"><span class="cube-dot ${esc(object.color)}"></span></span><div><strong>${esc(object.name)}</strong><small>ID ${esc(object.id)}</small></div></div></td>
    <td>${object.aliases.map(alias => `<span class="alias-tag">${esc(alias)}</span>`).join("") || "—"}</td>
    <td class="position-text">${object.position.map(value => Number(value).toFixed(3)).join(" / ")}</td>
    <td><span class="skill-label">${esc(object.skill_ids?.join(" · ") || "未绑定")}</span></td>
    <td><div class="table-actions"><button class="icon-button" data-edit-object="${esc(object.id)}" aria-label="编辑${esc(object.name)}" title="编辑"><svg class="icon"><use href="#i-edit"/></svg></button><button class="icon-button" data-delete-object="${esc(object.id)}" aria-label="删除${esc(object.name)}" title="删除"><svg class="icon"><use href="#i-trash"/></svg></button></div></td></tr>`).join("");
}

function objectOptions(selectedId) {
  return `<option value="">选择物体</option>${objects.map(o => `<option value="${esc(o.id)}" ${o.id === selectedId ? "selected" : ""}>${esc(o.name)}</option>`).join("")}`;
}

function renderPlacements() {
  $("placement-count").textContent = placementPoints.length;
  const query = $("placement-search").value.trim().toLowerCase();
  const filtered = placementPoints.filter(p => [p.name, ...p.aliases].some(n => n.toLowerCase().includes(query)));
  $("placements-empty").hidden = filtered.length > 0;
  $("placements-empty").textContent = placementPoints.length ? "没有匹配的放置点，请调整搜索内容。" : "尚未登记放置点。点击“登记放置点”，在画面点选后保存。";
  $("placement-table").innerHTML = filtered.map(point => `<tr><td><div class="table-object"><span class="object-thumbnail"><span class="placement-dot"></span></span><div><strong>${esc(point.name)}</strong><small>ID ${esc(point.id)}</small></div></div></td><td>${point.aliases.map(n => `<span class="alias-tag">${esc(n)}</span>`).join("") || "—"}</td><td class="position-text">${point.position.map(n => Number(n).toFixed(3)).join(" / ")}</td><td><div class="table-actions"><button class="icon-button" data-edit-placement="${point.id}" aria-label="编辑${esc(point.name)}"><svg class="icon"><use href="#i-edit"/></svg></button><button class="icon-button" data-delete-placement="${point.id}" aria-label="删除${esc(point.name)}"><svg class="icon"><use href="#i-trash"/></svg></button></div></td></tr>`).join("");
  const first = placementPoints[0];
  $("example-place").hidden = !first;
  $("example-place").dataset.example = first ? `放到${first.name}` : "";
  $("example-place").textContent = first ? `放到${first.name} ↗` : "";
}

function placementOptions(selectedId) {
  return `<option value="">选择放置点</option>${placementPoints.map(p => `<option value="${p.id}" ${p.id === selectedId ? "selected" : ""}>${esc(p.name)} · ${p.position.map(v => v.toFixed(3)).join(" / ")}</option>`).join("")}`;
}

function newStep(action = "GRASP") {
  return { action, object_id: objects[0]?.id ?? null, placement_point_id: null, position: [0.55, 0, 0.25], seconds: 1 };
}

function stepFields(step) {
  if (step.action === "GRASP") return `<select data-step-object aria-label="步骤目标物体" required>${objectOptions(step.object_id)}</select>`;
  if (step.action === "PLACE") return `<label>物体<select data-step-object aria-label="放置物体" required>${objectOptions(step.object_id)}</select></label><label>放置到<select data-step-placement aria-label="步骤放置点" required>${placementOptions(step.placement_point_id)}</select></label>`;
  if (step.action === "MOVE") return ["X", "Y", "Z"].map((axis, i) => `<label>${axis}<input data-step-position="${i}" aria-label="移动目标${axis}" type="number" step="0.001" value="${esc(step.position?.[i] ?? 0)}" required></label>`).join("");
  return `<label>等待<input data-step-seconds aria-label="等待秒数" type="number" step="0.1" min="0.1" value="${esc(step.seconds ?? 1)}" required>秒</label>`;
}

function renderSteps() {
  $("task-steps").innerHTML = steps.map((step, index) => `<div class="task-step" data-step="${index}">
    <div class="step-top"><span class="step-number">${String(index + 1).padStart(2, "0")}</span><select data-step-action aria-label="第${index + 1}步动作">${["GRASP", "MOVE", "PLACE", "WAIT"].map(action => `<option value="${action}" ${step.action === action ? "selected" : ""}>${actionNames[action]} / ${action}</option>`).join("")}</select><div class="step-tools"><button type="button" class="icon-button" data-step-up aria-label="上移步骤" ${index === 0 ? "disabled" : ""}>↑</button><button type="button" class="icon-button" data-step-down aria-label="下移步骤" ${index === steps.length - 1 ? "disabled" : ""}>↓</button><button type="button" class="icon-button" data-step-delete aria-label="删除步骤"><svg class="icon"><use href="#i-close"/></svg></button></div></div>
    <div class="step-fields ${step.action === "PLACE" ? "placement-fields" : ""}">${stepFields(step)}</div>
  </div>`).join("");
}

function resetTask() {
  editingTaskId = null;
  $("task-form").reset();
  $("task-editor-title").textContent = "创建任务";
  steps = [newStep()];
  renderSteps();
}

function taskPayload() {
  return { ...(editingTaskId == null ? {} : { id: editingTaskId }),
    name: $("task-name").value.trim(), aliases: aliases($("task-aliases").value),
    steps: steps.map(step => step.action === "MOVE"
      ? { action: "MOVE", position: roundedPosition(step.position), coordinate_frame: "world" }
      : step.action === "WAIT" ? { action: "WAIT", seconds: step.seconds }
        : step.action === "PLACE" ? { action: "PLACE", object_id: step.object_id, placement_point_id: step.placement_point_id }
        : { action: step.action, object_id: step.object_id }) };
}

function renderTasks() {
  $("task-count").textContent = tasks.length;
  $("tasks-empty").hidden = tasks.length > 0;
  $("saved-tasks").innerHTML = tasks.map(task => `<article class="saved-task"><div class="saved-task-heading"><h3>${esc(task.name)}</h3><div><button class="icon-button" data-edit-task="${esc(task.id)}" aria-label="编辑${esc(task.name)}"><svg class="icon"><use href="#i-edit"/></svg></button><button class="icon-button" data-delete-task="${esc(task.id)}" aria-label="删除${esc(task.name)}"><svg class="icon"><use href="#i-trash"/></svg></button></div></div><small>${task.aliases?.length ? esc(task.aliases.join(" / ")) : "可通过任务名称执行"}</small><div class="task-sequence">${task.steps.map(step => `<span>${esc(actionNames[step.action] || step.action)}</span>`).join(" → ")}</div><div class="saved-task-bottom"><span>${task.steps.length} 个步骤</span><button class="button button-secondary" data-run-task="${esc(task.id)}"><svg class="icon"><use href="#i-play"/></svg>${config.mode === "preview" ? "预览执行" : "执行任务"}</button></div></article>`).join("");
}

function renderParsed(value) {
  const result = $("parse-result");
  result.hidden = false;
  result.classList.toggle("rejected", value.status !== "ready");
  if (value.status !== "ready") {
    result.innerHTML = `<p>${esc(value.message || "当前指令无法执行，请补充目标信息。")}</p>`;
    return;
  }
  const source = value.source === "preview_rules" ? "规则预览" : "Transformer";
  const target = value.placement_point_name ? `${value.object_name || "当前持物"} → ${value.placement_point_name}` : value.object_name || value.task_name || "当前任务";
  result.innerHTML = `<div class="result-item"><span class="result-label">有效命令</span><span class="result-value"><code>${esc(value.model_input || value.normalized_instruction || "—")}</code></span></div><span class="result-arrow">→</span><div class="result-item"><span class="result-label">动作 · ${esc(source)}</span><span class="result-value">${esc(value.action)}</span></div><span class="result-arrow">→</span><div class="result-item"><span class="result-label">匹配目标</span><span class="result-value">${esc(target)}</span></div>`;
}

async function executeRequest(request) {
  if (jobRunning) throw new Error("已有任务正在执行，请等待完成或停止当前任务。");
  const currentApi = requireApi();
  const version = connectionVersion;
  const execution = ++executionVersion;
  jobRunning = true;
  updateGraspControls();
  if (request.source === "coordinate_test") $("grasp-test-result").hidden = true;
  $("execute-button").disabled = true;
  setActivity("正在提交执行请求…");
  try {
    const job = await currentApi.execute(request);
    if (version !== connectionVersion || execution !== executionVersion) return;
    if (!job.id || !job.status) throw new Error("执行接口没有返回有效的任务 ID 和状态。");
    activeJob = job;
    renderGraspResult(job);
    showRequest({ request, response: job });
    setActivity(job.message || `任务状态：${job.status}`);
    if (terminalStatuses.has(job.status)) {
      jobRunning = false;
      toast(job.message || (job.status === "succeeded" ? "任务已完成。" : "任务已结束。"), job.status === "failed");
    } else {
      pollJob(job.id, version, execution);
    }
  } catch (error) {
    if (version !== connectionVersion || execution !== executionVersion) return;
    jobRunning = config.mode === "backend" && (!error.status || error.status >= 500);
    setActivity(jobRunning ? "执行状态未知，请先停止任务再提交新请求。" : "执行请求失败，请检查目标和参数。");
    throw error;
  } finally {
    if (version === connectionVersion && execution === executionVersion) {
      $("execute-button").disabled = jobRunning || parsed?.status !== "ready";
      updateGraspControls();
    }
  }
}

function pollJob(id, version, execution) {
  clearTimeout(jobTimer);
  jobTimer = setTimeout(async () => {
    if (version !== connectionVersion || execution !== executionVersion || !api) return;
    try {
      const job = await api.getJob(id);
      if (version !== connectionVersion || execution !== executionVersion) return;
      activeJob = job;
      renderGraspResult(job);
      setActivity(job.message || `任务状态：${job.status}`);
      showRequest({ job });
      if (terminalStatuses.has(job.status)) {
        jobRunning = false;
        if (scene) scene.requires_reset = job.requires_reset ?? job.mode !== "program";
        if (config.mode === "backend") { parseVersion++; parsed = null; }
        updateGraspControls();
        $("execute-button").disabled = parsed?.status !== "ready";
        toast(job.message || "任务已结束。", job.status === "failed");
      } else pollJob(id, version, execution);
    } catch (error) {
      if (version !== connectionVersion || execution !== executionVersion) return;
      setActivity("任务状态连接中断，运行状态未知。可尝试停止任务。");
      report(error);
      // 状态未知时保持禁止提交新任务，直到停止或重连。
    }
  }, 500);
}

bind("camera-stage", "click", async event => {
  if (!scene) throw new Error("场景尚未就绪，请等待加载或重新连接。");
  const rect = $("camera-stage").getBoundingClientRect();
  const x = Math.min(0.999999, Math.max(0, (event.clientX - rect.left) / rect.width));
  const y = Math.min(0.999999, Math.max(0, (event.clientY - rect.top) / rect.height));
  let object;
  if (config.mode === "preview") {
    const code = pickingContext.getImageData(Math.floor(x * pickingCanvas.width), Math.floor(y * pickingCanvas.height), 1, 1).data[0];
    object = scene.objects.find(o => o.pick_code === code);
  }
  await pickObject(object, x, y);
});

document.querySelectorAll("[data-color]").forEach(button => button.addEventListener("click", async () => {
  try {
    const object = scene?.objects.find(o => o.color === button.dataset.color);
    if (!object?.screen_position) throw new Error("场景中没有可选的对应方块，请刷新场景。");
    await pickObject(object, ...object.screen_position);
  } catch (error) { report(error); }
}));

bind("object-form", "submit", async event => {
  event.preventDefault();
  const button = $("save-object");
  if (button.disabled) return;
  const kind = registrationKind;
  const selection = pickVersion;
  const value = kind === "placement"
    ? { ...(editingPlacementId == null ? {} : { id: editingPlacementId }), name: $("object-name").value.trim(),
      aliases: aliases($("object-aliases").value), position: readPosition("object"), coordinate_frame: "world" }
    : readObject();
  const version = connectionVersion;
  button.disabled = true;
  try {
    const object = kind === "placement" ? await requireApi().savePlacementPoint(value) : await requireApi().saveObject(value);
    if (version !== connectionVersion) return;
    if (kind === "placement") placementPoints = await api.listPlacementPoints();
    else objects = await api.listObjects();
    if (version !== connectionVersion) return;
    invalidateParsed();
    renderObjects();
    renderPlacements();
    renderSteps();
    renderTasks();
    if (selection !== pickVersion || kind !== registrationKind) return;
    if (kind === "placement") editingPlacementId = object.id;
    else { editingObjectId = object.id; selected.scene_object_id = object.scene_object_id; }
    selected.grasp_position = object.position;
    $("selection-description").textContent = "已保存 · 可继续修改名称、别名和坐标";
    updateRegistrationLabels();
    $("selection-title").textContent = object.name;
    renderMarker();
    const noun = kind === "placement" ? "放置点" : "物体";
    toast(config.mode === "preview" ? `${noun}已保存到当前浏览器。` : `${noun}已保存到后端。`);
  } finally { if (version === connectionVersion) button.disabled = !selected; }
});

bind("test-grasp", "click", async () => {
  if (registrationKind !== "object") throw new Error("放置点不能用于坐标试抓。");
  if (!selected || !scene) throw new Error("请重新点选场景中的位置。");
  await executeRequest({ action: "GRASP", ...(editingObjectId == null ? {} : { object_id: editingObjectId }),
    scene_object_id: selected.scene_object_id, grasp_position: readPosition("object"),
    frame_id: selected.frame_id, coordinate_frame: "world", source: "coordinate_test" });
});

bind("object-search", "input", renderObjects);
bind("object-table", "click", async event => {
  const edit = event.target.closest("[data-edit-object]");
  const remove = event.target.closest("[data-delete-object]");
  if (!edit && !remove) return;
  const object = objects.find(o => String(o.id) === (edit?.dataset.editObject || remove?.dataset.deleteObject));
  if (!object) return;
  if (edit) {
    startRegistration("object");
    location.hash = "workspace";
    if (object.color === "point") {
      if (!scene) throw new Error("请先重新连接相机画面。");
      fillObjectEditor({ scene_object_id: object.scene_object_id, color: "point", frame_id: scene.frame_id,
        grasp_position: object.position, position_source: "saved_position" }, object, true);
      $("object-name").focus();
      return;
    }
    const item = scene?.objects.find(o => o.scene_object_id === object.scene_object_id);
    if (!item?.screen_position) throw new Error("当前画面中没有这个物体，请刷新场景。");
    location.hash = "workspace";
    await pickObject(item, ...item.screen_position, true);
    $("object-name").focus();
  } else if (confirm(`删除“${object.name}”的登记信息？仿真中的方块仍会保留。`)) {
    await requireApi().deleteObject(object.id);
    objects = await api.listObjects();
    invalidateParsed();
    if (editingObjectId === object.id) { clearObjectEditor(); renderMarker(); }
    renderObjects(); renderSteps();
    toast("物体登记已删除。");
  }
});

bind("registration-kind", "change", event => startRegistration(event.target.value));
bind("new-target", "click", () => startRegistration(registrationKind));
bind("add-placement", "click", () => {
  location.hash = "workspace";
  startRegistration("placement");
  toast("点击空白地面或方块顶部中央，填写放置点名称后保存。");
});
bind("placement-search", "input", renderPlacements);
bind("placement-table", "click", async event => {
  const edit = event.target.closest("[data-edit-placement]");
  const remove = event.target.closest("[data-delete-placement]");
  const point = placementPoints.find(p => String(p.id) === (edit?.dataset.editPlacement || remove?.dataset.deletePlacement));
  if (!point) return;
  if (edit) {
    if (!scene) throw new Error("请先连接相机画面。");
    location.hash = "workspace";
    startRegistration("placement");
    fillObjectEditor({ color: "point", frame_id: scene.frame_id, hit_position: point.position,
      grasp_position: point.position, position_source: "saved_position" }, point, true);
    $("object-name").focus();
  } else if (confirm(`删除放置点“${point.name}”？`)) {
    await requireApi().deletePlacementPoint(point.id);
    placementPoints = await api.listPlacementPoints();
    invalidateParsed();
    if (editingPlacementId === point.id) { clearObjectEditor(); renderMarker(); }
    renderPlacements(); renderSteps(); renderTasks();
    toast("放置点已删除。");
  }
});

bind("task-steps", "change", event => {
  const row = event.target.closest("[data-step]");
  if (!row) return;
  const step = steps[Number(row.dataset.step)];
  if (event.target.hasAttribute("data-step-action")) {
    Object.assign(step, newStep(event.target.value));
    renderSteps();
  } else if (event.target.hasAttribute("data-step-object")) {
    step.object_id = objects.find(o => String(o.id) === event.target.value)?.id ?? null;
  } else if (event.target.hasAttribute("data-step-placement")) {
    step.placement_point_id = placementPoints.find(p => String(p.id) === event.target.value)?.id ?? null;
  } else if (event.target.hasAttribute("data-step-position")) {
    formatCoordinateInput(event.target);
    step.position[Number(event.target.dataset.stepPosition)] = event.target.valueAsNumber;
  }
});
bind("task-steps", "keydown", event => {
  if (event.key === "Enter" && event.target.hasAttribute("data-step-position")) {
    formatCoordinateInput(event.target);
    steps[Number(event.target.closest("[data-step]").dataset.step)].position[Number(event.target.dataset.stepPosition)] = event.target.valueAsNumber;
  }
});
bind("task-steps", "input", event => {
  const row = event.target.closest("[data-step]");
  if (!row) return;
  const step = steps[Number(row.dataset.step)];
  if (event.target.hasAttribute("data-step-position")) step.position[Number(event.target.dataset.stepPosition)] = event.target.valueAsNumber;
  if (event.target.hasAttribute("data-step-seconds")) step.seconds = event.target.valueAsNumber;
});
bind("task-steps", "click", event => {
  const row = event.target.closest("[data-step]");
  if (!row) return;
  const index = Number(row.dataset.step);
  if (event.target.closest("[data-step-delete]")) steps.splice(index, 1);
  else if (event.target.closest("[data-step-up]") && index > 0) [steps[index - 1], steps[index]] = [steps[index], steps[index - 1]];
  else if (event.target.closest("[data-step-down]") && index < steps.length - 1) [steps[index], steps[index + 1]] = [steps[index + 1], steps[index]];
  else return;
  renderSteps();
});
bind("add-step", "click", () => { steps.push(newStep()); renderSteps(); });
bind("new-task", "click", () => { resetTask(); $("task-name").focus(); });
bind("sample-task", "click", () => {
  resetTask();
  $("task-name").value = "抓取演示";
  $("task-aliases").value = "拿起方块";
  steps = [newStep("GRASP"), newStep("WAIT")];
  renderSteps();
});
bind("task-form", "submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  if (button.disabled) return;
  button.disabled = true;
  try {
    await requireApi().saveTask(taskPayload());
    tasks = await api.listTasks();
    invalidateParsed(); renderTasks(); resetTask();
    toast("任务已保存，可使用“执行 + 任务名称”发起请求。");
  } finally { button.disabled = false; }
});

bind("saved-tasks", "click", async event => {
  const button = event.target.closest("[data-edit-task], [data-delete-task], [data-run-task]");
  if (!button) return;
  const id = button.dataset.editTask || button.dataset.deleteTask || button.dataset.runTask;
  const task = tasks.find(t => String(t.id) === id);
  if (!task) return;
  if (button.hasAttribute("data-edit-task")) {
    editingTaskId = task.id;
    $("task-name").value = task.name;
    $("task-aliases").value = (task.aliases || []).join("，");
    steps = task.steps.map(step => ({ ...newStep(step.action), ...structuredClone(step) }));
    $("task-editor-title").textContent = "编辑任务";
    renderSteps(); $("task-name").focus();
  } else if (button.hasAttribute("data-delete-task")) {
    if (!confirm(`删除任务“${task.name}”？`)) return;
    await requireApi().deleteTask(task.id);
    tasks = await api.listTasks();
    invalidateParsed(); renderTasks();
    if (editingTaskId === task.id) resetTask();
    toast("任务已删除。");
  } else {
    location.hash = "workspace";
    await executeRequest({ action: "RUN_TASK", task_id: task.id, source: "task_builder" });
  }
});

document.querySelectorAll("[data-example]").forEach(button => button.addEventListener("click", () => {
  $("instruction").value = button.dataset.example;
  invalidateParsed(); $("instruction").focus();
}));
bind("instruction", "input", invalidateParsed);
bind("instruction-form", "submit", async event => {
  event.preventDefault();
  if ($("parse-button").disabled) return;
  const currentApi = requireApi();
  const text = $("instruction").value.trim();
  if (!text) throw new Error("请输入一条指令。");
  const version = ++parseVersion;
  const connection = connectionVersion;
  $("parse-button").disabled = true;
  $("execute-button").disabled = true;
  parsed = null;
  setActivity(config.mode === "preview" ? "正在生成规则预览…" : "模型正在理解指令…");
  try {
    const result = await currentApi.parseInstruction(text, model);
    if (version !== parseVersion || connection !== connectionVersion) return;
    parsed = result;
    renderParsed(result);
    $("execute-button").disabled = result.status !== "ready" || jobRunning;
    setActivity(result.status === "ready" ? "解析完成，请检查目标后发起执行" : "未生成执行请求，请调整指令");
    showRequest({ instruction: text, model, response: result });
  } finally { if (connection === connectionVersion) $("parse-button").disabled = false; }
});

bind("execute-button", "click", async () => {
  if (parsed?.status !== "ready") throw new Error("请先解析一条有效指令。");
  if (parsed.action === "STOP") { await stopTask(); return; }
  const request = { action: parsed.action, source: "language", model,
    instruction: parsed.instruction, ...(parsed.parse_id ? { parse_id: parsed.parse_id } : {}) };
  for (const key of ["object_id", "task_id", "position", "placement_point_id"]) {
    if (parsed[key] != null) request[key] = parsed[key];
  }
  await executeRequest(request);
});

async function stopTask() {
  const currentApi = requireApi();
  const interruptedJob = activeJob && !terminalStatuses.has(activeJob.status) ? activeJob.id : null;
  const version = connectionVersion;
  const execution = ++executionVersion;
  invalidateParsed();
  clearTimeout(jobTimer);
  jobRunning = true;
  setActivity("正在请求停止任务…");
  const result = await currentApi.stop();
  if (version !== connectionVersion || execution !== executionVersion) return;
  if (!["stopped", "cancelled", "preview"].includes(result?.status)) {
    throw new Error("后端尚未确认停止完成，请检查任务状态。");
  }
  jobRunning = false;
  updateGraspControls();
  activeJob = null;
  setActivity(result.message || "停止请求已发送。");
  showRequest({ action: "STOP", response: result });
  toast(result.message || "停止请求已发送。");
  if (interruptedJob && config.mode === "backend") {
    const stoppedJob = await currentApi.getJob(interruptedJob);
    if (version === connectionVersion && execution === executionVersion) {
      activeJob = stoppedJob;
      renderGraspResult(stoppedJob);
    }
  }
}
document.querySelectorAll(".stop-button").forEach(button => button.addEventListener("click", () => stopTask().catch(report)));
bind("refresh-scene", "click", async () => {
  if (!api) { await connect(); return; }
  clearTimeout(sceneTimer);
  cameraAbort?.abort();
  const version = connectionVersion;
  const camera = ++sceneVersion;
  await displayScene(await api.getScene(), version, camera);
  if (version === connectionVersion) {
    $("connection-error").hidden = true;
    if (config.mode === "backend") { setConnection("connected", "后端已连接"); void startCamera(version, camera); }
    toast("场景画面已刷新。");
  }
});

bind("reset-scene", "click", async () => {
  const currentApi = requireApi();
  const version = connectionVersion;
  const camera = ++sceneVersion;
  executionVersion++;
  clearTimeout(sceneTimer);
  cameraAbort?.abort();
  clearTimeout(jobTimer);
  jobRunning = true;
  updateGraspControls();
  $("reset-scene").disabled = true;
  invalidateParsed();
  setActivity("正在重置机械臂和方块…");
  try {
    const response = await currentApi.reset();
    if (version !== connectionVersion) return;
    clearObjectEditor();
    $("grasp-test-result").hidden = true;
    activeJob = null;
    jobRunning = false;
    await displayScene(await currentApi.getScene(), version, camera);
    setActivity(response.message);
    toast(response.message);
  } finally {
    if (version === connectionVersion) {
      $("reset-scene").disabled = false;
      updateGraspControls();
      if (config.mode === "backend") void startCamera(version, camera);
    }
  }
});

function openSettings() {
  $("connection-mode").value = config.mode;
  $("api-base-url").value = config.apiBaseUrl;
  $("settings-dialog").showModal();
}
bind("settings-button", "click", openSettings);
bind("top-settings", "click", openSettings);
bind("close-settings", "click", () => $("settings-dialog").close());
bind("settings-form", "submit", async event => {
  event.preventDefault();
  const url = new URL($("api-base-url").value);
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("后端地址需要使用 http 或 https。");
  if (jobRunning) throw new Error("请先停止当前任务，再切换接口。");
  const next = { ...config, mode: $("connection-mode").value, apiBaseUrl: url.href.replace(/\/+$/, "") };
  localStorage.setItem("panda.frontend.settings", JSON.stringify(next));
  config = next;
  $("settings-dialog").close();
  await connect();
});
bind("clear-memory", "click", async () => {
  const currentApi = requireApi();
  const scope = config.mode === "backend" ? "后端数据库" : "当前浏览器的本地预览";
  const resetNote = config.mode === "backend" ? "\n正在运行的任务会停止，机械臂和方块会恢复初始状态。" : "";
  if (!confirm(`清除${scope}的所有记忆？\n物体、别名、坐标、放置点和任务将永久删除。${resetNote}\n训练数据和模型权重会保留。`)) return;
  $("clear-memory").disabled = true;
  document.body.inert = true;
  connectionVersion++;
  executionVersion++;
  sceneVersion++;
  pickVersion++;
  clearTimeout(sceneTimer);
  clearTimeout(jobTimer);
  cameraAbort?.abort();
  invalidateParsed();
  setActivity("正在清除记忆并恢复工作台…");
  try {
    const response = await currentApi.clearMemory();
    if (response?.status !== "cleared") throw new Error("后端未确认清除完成，请检查服务状态。");
    registrationKind = "object";
    for (const id of ["instruction", "object-search", "placement-search"]) $(id).value = "";
    $("settings-dialog").close();
    location.hash = "workspace";
    await connect();
    toast(response.message);
  } catch (error) {
    await connect();
    throw error;
  } finally {
    document.body.inert = false;
    $("clear-memory").disabled = !api;
  }
});
bind("clear-preview", "click", async () => {
  if (!confirm("清除当前浏览器的预览物体、放置点和任务，恢复空物体库？")) return;
  localStorage.removeItem("panda.frontend.preview.v2");
  $("settings-dialog").close();
  if (config.mode === "preview") await connect();
  toast("本地预览数据已清除。");
});
for (const id of ["add-object", "guide-button"]) bind(id, "click", () => {
  location.hash = "workspace";
  startRegistration("object");
  toast("点击场景中的红、绿、蓝方块，登记或编辑物体。");
});
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
for (const axis of ["x", "y", "z"]) {
  const input = $(`object-${axis}`);
  const format = () => {
    formatCoordinateInput(input);
    const inputs = ["x", "y", "z"].map(value => $(`object-${value}`));
    if (selected && inputs.every(value => value.value.trim() && Number.isFinite(value.valueAsNumber))) {
      selected.grasp_position = roundedPosition(inputs.map(value => value.valueAsNumber));
      selected.screen_position = projectPosition(selected.grasp_position, scene?.camera);
      renderMarker();
    }
  };
  input.addEventListener("change", format);
  input.addEventListener("keydown", event => { if (event.key === "Enter") format(); });
}
window.addEventListener("beforeunload", () => { clearTimeout(sceneTimer); clearTimeout(jobTimer); cameraAbort?.abort(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { cameraAbort?.abort(); clearTimeout(sceneTimer); }
  else if (api && config.mode === "backend") void startCamera(connectionVersion, ++sceneVersion);
});
navigate(location.hash.slice(1));
try {
  config = await loadRuntimeConfig(config);
  await connect();
} catch (error) {
  setConnection("error", "启动配置失败");
  $("connection-error").textContent = error.message;
  $("connection-error").hidden = false;
}
