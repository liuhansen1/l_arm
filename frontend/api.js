/** 后端唯一入口。UI 不直接使用 fetch；接口契约见 API.md。 */
import { pointOnGround, roundedPosition } from "./coordinates.js";
import { readFrameStream } from "./frame_stream.js";
// 新的手动登记预览库；旧版本浏览器数据仍保留在 v1 键中。
const STORAGE_KEY = "panda.frontend.preview.v2";
const ACTIONS = new Set(["GRASP", "MOVE", "PLACE", "WAIT"]);

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function words(values) {
  return [...new Set(values.map(v => String(v).normalize("NFKC").trim()).filter(Boolean))];
}

function validPosition(position) {
  return Array.isArray(position) && position.length === 3
    && position.every(value => typeof value === "number" && Number.isFinite(value));
}

function validateObject(value, objects) {
  if (!value.name?.trim()) throw new ApiError("请填写物体名称。");
  if (!value.scene_object_id && value.color !== "point") throw new ApiError("请先在画面中采集一个坐标。");
  if (!validPosition(value.position)) throw new ApiError("请填写三个有效的坐标数值。");
  const labels = words([value.name, ...(value.aliases || [])]);
  for (const object of objects.filter(o => o.id !== value.id)) {
    if (value.scene_object_id && object.scene_object_id === value.scene_object_id) {
      throw new ApiError("这个方块已经登记，请编辑已有物体。");
    }
    if (words([object.name, ...object.aliases]).some(label => labels.includes(label))) {
      throw new ApiError("名称或别名与其他物体重复，请换一个名称。");
    }
  }
}

function validateTask(value, objects, tasks, placementPoints = []) {
  if (!value.name?.trim()) throw new ApiError("请填写任务名称。");
  if (tasks.some(task => task.id !== value.id && task.name === value.name.trim())) {
    throw new ApiError("任务名称已存在。");
  }
  if (!value.steps?.length) throw new ApiError("请至少添加一个任务步骤。");
  for (const step of value.steps) {
    if (!ACTIONS.has(step.action)) throw new ApiError("任务中包含不支持的动作。");
    if (["GRASP", "PLACE"].includes(step.action) && !objects.some(o => o.id === step.object_id)) {
      throw new ApiError("请为抓取和放下步骤选择已登记的物体。");
    }
    if (step.action === "PLACE" && !placementPoints.some(p => p.id === step.placement_point_id)) {
      throw new ApiError("放下步骤需要选择已登记的放置点。");
    }
    if (step.action === "MOVE" && !validPosition(step.position)) {
      throw new ApiError("移动步骤需要有效的目标坐标。");
    }
    if (step.action === "WAIT" && (!Number.isFinite(step.seconds) || step.seconds <= 0)) {
      throw new ApiError("等待时间必须大于 0 秒。");
    }
  }
}

/** 此匹配仅用于静态预览界面；不代表 Transformer 的推理结果。 */
export function previewParse(instruction, objects, tasks, model, placementPoints = []) {
  const text = instruction.normalize("NFKC").replace(/\s+/g, "");
  const base = { instruction, model, source: "preview_rules", confidence: null };
  const reject = message => ({ ...base, status: "unmatched", message });
  if (!text) return reject("请输入一条指令。");
  if (/^(停止|停止当前任务|停止任务|停下|急停|stop)[。！!]?$/i.test(text)) {
    return { ...base, status: "ready", action: "STOP", normalized_instruction: text };
  }
  // 预览只接受完整的单目标句式，避免已知名称掩盖未知目标或复杂条件。
  const candidates = [];
  for (const object of objects) {
    for (const label of words([object.name, ...object.aliases])) {
      const index = text.indexOf(label);
      if (index < 0) continue;
      const normalized = text.slice(0, index) + "<OBJ>" + text.slice(index + label.length);
      if (/^(请|请你|帮我|麻烦|麻烦你)?(抓取|抓起|抓|拿起|拿一下|拿|夹起)<OBJ>[。！!]?$/u.test(normalized)) {
        candidates.push({ ...base, status: "ready", action: "GRASP", object_id: object.id,
          object_name: object.name, normalized_instruction: normalized });
      }
    }
  }
  for (const point of placementPoints) {
    for (const label of words([point.name, ...point.aliases])) {
      const index = text.indexOf(label);
      if (index < 0) continue;
      const normalized = text.slice(0, index) + "<OBJ>" + text.slice(index + label.length);
      const destination = { placement_point_id: point.id, placement_point_name: point.name, placement_position: point.position };
      if (/^(请|请你|帮我|麻烦|麻烦你)?(放到|放在)<OBJ>[。！!]?$/u.test(normalized)) {
        candidates.push({ ...base, ...destination, status: "ready", action: "PLACE", normalized_instruction: normalized });
      }
      for (const object of objects) for (const name of words([object.name, ...object.aliases])) {
        const targetIndex = normalized.indexOf(name);
        if (targetIndex < 0) continue;
        const command = normalized.slice(0, targetIndex) + "<OBJ>" + normalized.slice(targetIndex + name.length);
        if (/^(请|请你|帮我|麻烦|麻烦你)?(把|将)?<OBJ>(放到|放在|放置到|放置在)<OBJ>[。！!]?$/u.test(command)) {
          candidates.push({ ...base, ...destination, status: "ready", action: "PLACE", object_id: object.id,
            object_name: object.name, normalized_instruction: command });
        }
      }
    }
  }
  for (const task of tasks) {
    for (const label of words([task.name, ...(task.aliases || [])])) {
      const index = text.indexOf(label);
      if (index < 0) continue;
      const normalized = text.slice(0, index) + "<TASK>" + text.slice(index + label.length);
      if (/^(请|帮我)?(执行|运行|开始)<TASK>[。！!]?$/u.test(normalized)) {
        candidates.push({ ...base, status: "ready", action: "RUN_TASK", task_id: task.id,
          task_name: task.name, normalized_instruction: normalized });
      }
    }
  }
  const unique = [...new Map(candidates.map(c => [JSON.stringify([c.action, c.object_id, c.task_id, c.placement_point_id]), c])).values()];
  return unique.length === 1 ? unique[0] : reject(unique.length > 1
    ? "匹配到多个目标，请使用唯一的物体名称。"
    : "未匹配到已登记目标或支持的预览句式。请先登记物体，再使用你填写的名称发出单目标指令。");
}

export class PreviewApi {
  constructor(config, { storage = globalThis.localStorage, fetcher = globalThis.fetch } = {}) {
    this.config = config;
    this.storage = storage;
    this.fetcher = fetcher;
    this.scene = null;
    this.data = null;
  }

  async init() {
    const response = await this.fetcher(this.config.sceneUrl);
    if (!response.ok) throw new ApiError("无法读取仿真预览，请通过 HTTP 服务打开页面。", response.status);
    this.scene = await response.json();
    const saved = this.storage.getItem(STORAGE_KEY);
    if (saved) {
      try {
        this.data = JSON.parse(saved);
        if (!Array.isArray(this.data.objects) || !Array.isArray(this.data.tasks)) throw new Error();
      } catch {
        throw new ApiError("本地预览数据无法读取。请在接口设置中清除预览数据。");
      }
    } else {
      this.data = {
        objects: [], tasks: [], next_object_id: 1, next_task_id: 1,
      };
      this.persist();
    }
    this.data.placement_points ??= [];
    this.data.next_placement_point_id ??= Math.max(0, ...this.data.placement_points.map(p => p.id)) + 1;
  }

  persist(data = this.data) {
    try { this.storage.setItem(STORAGE_KEY, JSON.stringify(data)); }
    catch { throw new ApiError("浏览器无法保存数据，请检查存储权限或剩余空间。"); }
  }

  async getScene() { return structuredClone(this.scene); }
  async listObjects() { return structuredClone(this.data.objects); }
  async listTasks() { return structuredClone(this.data.tasks); }
  async listPlacementPoints() { return structuredClone(this.data.placement_points); }

  async clearMemory() {
    const cleared = { objects: this.data.objects.length, placement_points: this.data.placement_points.length,
      tasks: this.data.tasks.length, parses: 0, jobs: 0 };
    const next = { ...this.data, objects: [], placement_points: [], tasks: [] };
    this.persist(next);
    this.data = next;
    return { status: "cleared", cleared, message: "本地预览的所有物体、放置点和任务已清除。" };
  }

  checkNames(value, kind) {
    const key = name => String(name).normalize("NFKC").replace(/\s+/g, "").toLowerCase();
    const labels = [value.name, ...(value.aliases || [])].map(key);
    for (const collection of ["objects", "tasks", "placement_points"]) {
      for (const item of this.data[collection]) {
        if (collection === kind && item.id === value.id) continue;
        if ([item.name, ...item.aliases].some(name => labels.includes(key(name)))) {
          throw new ApiError("名称或别名与已有物体、放置点或任务重复。");
        }
      }
    }
  }

  async pick({ scene_object_id, frame_id, x, y }) {
    if (frame_id !== this.scene.frame_id) throw new ApiError("画面已过期，请刷新后重新点选。");
    const object = this.scene.objects.find(o => o.scene_object_id === scene_object_id);
    if (!object) {
      if (scene_object_id || ![x, y].every(v => Number.isFinite(v) && v >= 0 && v < 1)) throw new ApiError("点选位置不合法。");
      const position = pointOnGround(x, y, this.scene.camera);
      return { scene_object_id: null, color: "point", grasp_position: position, hit_position: position,
        screen_position: [x, y], coordinate_frame: "world", frame_id, position_source: "work_plane_preview" };
    }
    return { scene_object_id, color: object.color, grasp_position: roundedPosition(object.position),
      hit_position: roundedPosition([object.position[0], object.position[1], object.position[2] + 0.025]),
      screen_position: [x, y], coordinate_frame: "world", frame_id, position_source: "object_center_preview" };
  }

  async saveObject(value) {
    validateObject(value, this.data.objects);
    this.checkNames(value, "objects");
    const object = { ...structuredClone(value), name: value.name.trim(), position: roundedPosition(value.position),
      scene_object_id: value.scene_object_id || `point-${globalThis.crypto.randomUUID()}`,
      aliases: words(value.aliases || []).filter(alias => alias !== value.name.trim()) };
    if (value.id != null) {
      const index = this.data.objects.findIndex(o => o.id === value.id);
      if (index < 0) throw new ApiError("物体已不存在，请刷新。");
      this.data.objects[index] = object;
    } else {
      object.id = this.data.next_object_id++;
      this.data.objects.push(object);
    }
    this.persist();
    return structuredClone(object);
  }

  async deleteObject(id) {
    if (this.data.tasks.some(task => task.steps.some(step => step.object_id === id))) {
      throw new ApiError("有任务正在引用此物体，请先修改或删除相应任务。");
    }
    this.data.objects = this.data.objects.filter(o => o.id !== id);
    this.persist();
  }

  async saveTask(value) {
    validateTask(value, this.data.objects, this.data.tasks, this.data.placement_points);
    this.checkNames(value, "tasks");
    const task = { ...structuredClone(value), name: value.name.trim(), aliases: words(value.aliases || []) };
    if (value.id != null) {
      const index = this.data.tasks.findIndex(t => t.id === value.id);
      if (index < 0) throw new ApiError("任务已不存在，请刷新。");
      this.data.tasks[index] = task;
    } else {
      task.id = this.data.next_task_id++;
      this.data.tasks.push(task);
    }
    this.persist();
    return structuredClone(task);
  }

  async deleteTask(id) {
    this.data.tasks = this.data.tasks.filter(t => t.id !== id);
    this.persist();
  }

  async savePlacementPoint(value) {
    if (!value.name?.trim() || !validPosition(value.position)) throw new ApiError("放置点需要名称和三个有效坐标。");
    this.checkNames(value, "placement_points");
    const point = { ...structuredClone(value), name: value.name.trim(), aliases: words(value.aliases || []),
      position: roundedPosition(value.position), coordinate_frame: "world" };
    if (value.id != null) {
      const index = this.data.placement_points.findIndex(p => p.id === value.id);
      if (index < 0) throw new ApiError("放置点不存在，请刷新。");
      this.data.placement_points[index] = point;
    } else {
      point.id = this.data.next_placement_point_id++;
      this.data.placement_points.push(point);
    }
    this.persist();
    return structuredClone(point);
  }

  async deletePlacementPoint(id) {
    if (this.data.tasks.some(t => t.steps.some(step => step.placement_point_id === id))) {
      throw new ApiError("有任务引用此放置点，请先修改相应任务。");
    }
    this.data.placement_points = this.data.placement_points.filter(p => p.id !== id);
    this.persist();
  }

  async parseInstruction(instruction, model) {
    return previewParse(instruction, this.data.objects, this.data.tasks, model, this.data.placement_points);
  }

  async execute(request) {
    return { id: `preview-${Date.now()}`, status: "preview",
      message: "请求预览已生成。尚未连接执行后端，机械臂没有实际运动。", request };
  }

  async getJob() { throw new ApiError("预览模式没有实际执行任务。"); }
  async stop() { return { status: "preview", message: "已清除待执行指令。预览模式中没有运行中的机械臂。" }; }
  async reset() { return { status: "preview", message: "已回到静态场景预览，未执行物理重置。" }; }
}

export class BackendApi {
  constructor(config) {
    this.base = config.apiBaseUrl.replace(/\/+$/, "");
  }

  async request(path, { method = "GET", body, timeout = 10000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(`${this.base}${path}`, {
        method, signal: controller.signal,
        headers: { Accept: "application/json", ...(body == null ? {} : { "Content-Type": "application/json" }) },
        ...(body == null ? {} : { body: JSON.stringify(body) }),
      });
      if (response.status === 204 && response.ok) return null;
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new ApiError(data?.message || `接口请求失败（${response.status}）`, response.status);
      if (data == null) throw new ApiError("后端没有返回有效的 JSON 数据。");
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error.name === "AbortError" ? "后端响应超时，请检查服务状态。"
        : "无法连接后端，请检查服务地址和跨域配置。");
    } finally { clearTimeout(timer); }
  }

  async init() { this.health = await this.request("/health"); }
  async getScene() {
    const scene = await this.request("/simulation/scene");
    if (scene.image_url) scene.image_url = new URL(scene.image_url, `${this.base}/`).href;
    return scene;
  }
  async *streamFrames(signal) {
    const response = await fetch(`${this.base}/simulation/stream`, { signal, cache: "no-store" });
    if (!response.ok || !response.body) throw new ApiError(`无法打开相机数据流（${response.status}）。`, response.status);
    yield* readFrameStream(response.body);
  }
  pick(payload) { return this.request("/simulation/pick", { method: "POST", body: payload }); }
  listObjects() { return this.request("/objects"); }
  saveObject(value) {
    return this.request(value.id == null ? "/objects" : `/objects/${encodeURIComponent(value.id)}`,
      { method: value.id == null ? "POST" : "PUT", body: { ...value, position: roundedPosition(value.position) } });
  }
  deleteObject(id) { return this.request(`/objects/${encodeURIComponent(id)}`, { method: "DELETE" }); }
  listPlacementPoints() { return this.request("/placement-points"); }
  savePlacementPoint(value) {
    return this.request(value.id == null ? "/placement-points" : `/placement-points/${encodeURIComponent(value.id)}`,
      { method: value.id == null ? "POST" : "PUT", body: { ...value, position: roundedPosition(value.position) } });
  }
  deletePlacementPoint(id) { return this.request(`/placement-points/${encodeURIComponent(id)}`, { method: "DELETE" }); }
  listTasks() { return this.request("/tasks"); }
  saveTask(value) {
    return this.request(value.id == null ? "/tasks" : `/tasks/${encodeURIComponent(value.id)}`,
      { method: value.id == null ? "POST" : "PUT", body: value });
  }
  deleteTask(id) { return this.request(`/tasks/${encodeURIComponent(id)}`, { method: "DELETE" }); }
  parseInstruction(instruction, model) {
    return this.request("/instructions/parse", { method: "POST", body: { instruction, model }, timeout: 120000 });
  }
  execute(payload) { return this.request("/jobs", { method: "POST", body: payload }); }
  getJob(id) { return this.request(`/jobs/${encodeURIComponent(id)}`); }
  stop() { return this.request("/simulation/stop", { method: "POST", body: {} }); }
  reset() { return this.request("/simulation/reset", { method: "POST", body: {}, timeout: 15000 }); }
  clearMemory() { return this.request("/memory/clear", { method: "POST", body: {}, timeout: 15000 }); }
}

export function createApi(config) {
  return config.mode === "backend" ? new BackendApi(config) : new PreviewApi(config);
}
