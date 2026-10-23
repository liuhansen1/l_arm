import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { BackendApi, PreviewApi, previewParse } from "../api.js";
import { loadRuntimeConfig } from "../config.js";

const scene = JSON.parse(await readFile(new URL("../assets/scene.json", import.meta.url), "utf8"));
function memoryStorage() {
  const data = new Map();
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) };
}
async function fixture(storage = memoryStorage()) {
  const api = new PreviewApi({ sceneUrl: "fixture" }, {
    storage, fetcher: async () => ({ ok: true, json: async () => structuredClone(scene) }),
  });
  await api.init();
  return api;
}

async function register(api, color = "red", name = "红色方块", aliases = ["红色积木"]) {
  const pick = await api.pick({ frame_id: scene.frame_id, scene_object_id: `cube-${color}` });
  return api.saveObject({ name, aliases, color, scene_object_id: pick.scene_object_id,
    position: pick.grasp_position, coordinate_frame: "world", skill_ids: ["cube_grasp"] });
}

test("初始物体库为空，点选只返回坐标，不自动登记名称", async () => {
  const api = await fixture();
  assert.deepEqual(await api.listObjects(), []);
  assert.equal((await api.parseInstruction("抓取红色方块", "transformer")).status, "unmatched");
  for (const color of ["red", "green", "blue"]) {
    const pick = await api.pick({ frame_id: scene.frame_id, scene_object_id: `cube-${color}` });
    assert.ok(Math.abs(pick.grasp_position[0] - 0.55) < 0.0001);
    assert.ok(Math.abs(pick.grasp_position[2] - 0.025) < 0.001);
    assert.equal(pick.name, undefined);
    assert.deepEqual(await api.listObjects(), []);
  }
});

test("新名称及别名可以登记、持久保存并用于预览指令", async () => {
  const storage = memoryStorage();
  const api = await fixture(storage);
  const object = await register(api, "red", "客户零件 A", ["新零件", "新零件"]);
  const reopened = await fixture(storage);
  const result = await reopened.parseInstruction("帮我抓取新零件", "transformer");
  assert.equal(result.object_id, object.id);
  assert.equal(result.object_name, "客户零件 A");
  assert.equal(result.normalized_instruction, "帮我抓取<OBJ>");
  assert.equal(result.source, "preview_rules");
  assert.equal(result.confidence, null);
  assert.deepEqual((await reopened.listObjects())[0].aliases, ["新零件"]);
});

test("未知物体、否定、多个目标及额外空间条件不会生成可执行预览", async () => {
  const api = await fixture();
  await register(api);
  for (const instruction of ["抓取黄色方块", "不要抓取红色方块", "抓取红色方块和杯子", "抓取红色方块左边的方块", "抓取红色方块再放下"]) {
    const result = await api.parseInstruction(instruction, "transformer");
    assert.equal(result.status, "unmatched", instruction);
    assert.equal(result.action, undefined);
  }
  assert.equal((await api.parseInstruction("停止当前任务", "transformer")).action, "STOP");
});

test("重复名称和别名、无效坐标、重复方块绑定会被拒绝", async () => {
  const api = await fixture();
  const red = await register(api);
  await register(api, "green", "绿色方块", ["绿色积木"]);
  await assert.rejects(api.saveObject({ ...red, aliases: ["绿色积木"] }), /重复/);
  await assert.rejects(api.saveObject({ ...red, position: [0, NaN, 0] }), /坐标/);
  const { id, ...duplicate } = red;
  await assert.rejects(api.saveObject(duplicate), /已经登记/);
});

test("删除登记后仍可对同一个场景方块重新登记", async () => {
  const api = await fixture();
  const red = await register(api);
  await api.deleteObject(red.id);
  const { id, ...value } = red;
  const created = await api.saveObject({ ...value, name: "实验方块" });
  assert.notEqual(created.id, red.id);
  assert.equal((await api.listObjects()).length, 1);
});

test("清空登记后重新打开仍为空，旧预览数据保持原样", async () => {
  const storage = memoryStorage();
  storage.setItem("panda.frontend.preview.v1", "原有预览数据");
  const api = await fixture(storage);
  assert.deepEqual(await api.listObjects(), []);
  const object = await register(api);
  await api.deleteObject(object.id);
  const reopened = await fixture(storage);
  assert.deepEqual(await reopened.listObjects(), []);
  assert.equal(storage.getItem("panda.frontend.preview.v1"), "原有预览数据");
});

test("清除记忆一次删除物体、别名、放置点和引用任务，重新打开仍为空", async () => {
  const storage = memoryStorage();
  storage.setItem("panda.frontend.settings", "接口设置");
  const api = await fixture(storage);
  const red = await register(api);
  const point = await api.savePlacementPoint({ name: "终点", aliases: ["目的地"], position: [0.4, 0.2, 0] });
  await api.saveTask({ name: "抓放", aliases: ["归位"], steps: [
    { action: "GRASP", object_id: red.id }, { action: "PLACE", object_id: red.id, placement_point_id: point.id },
  ] });
  const result = await api.clearMemory();
  assert.equal(result.status, "cleared");
  assert.deepEqual(result.cleared, { objects: 1, placement_points: 1, tasks: 1, parses: 0, jobs: 0 });
  const reopened = await fixture(storage);
  assert.deepEqual(await reopened.listObjects(), []);
  assert.deepEqual(await reopened.listPlacementPoints(), []);
  assert.deepEqual(await reopened.listTasks(), []);
  assert.notEqual((await reopened.parseInstruction("抓取红色积木", "transformer")).status, "ready");
  assert.equal(storage.getItem("panda.frontend.settings"), "接口设置");
  assert.ok((await register(reopened)).id > red.id);
});

test("清除预览记忆写入失败时保留原数据并报告错误", async t => {
  const storage = memoryStorage();
  const api = await fixture(storage);
  const red = await register(api);
  t.mock.method(storage, "setItem", () => { throw new Error("storage full"); });
  await assert.rejects(api.clearMemory(), /无法保存/);
  assert.deepEqual(await api.listObjects(), [red]);
  const reopened = await fixture(storage);
  assert.deepEqual(await reopened.listObjects(), [red]);
});

test("后端清除记忆使用独立接口，失败时保留服务端错误", async t => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    return new Response(JSON.stringify(requests.length === 1 ? { status: "cleared" } : { message: "重置失败" }),
      { status: requests.length === 1 ? 200 : 503 });
  });
  const api = new BackendApi({ apiBaseUrl: "http://localhost:8080/api/v1" });
  assert.equal((await api.clearMemory()).status, "cleared");
  assert.equal(requests[0].url, "http://localhost:8080/api/v1/memory/clear");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), {});
  await assert.rejects(api.clearMemory(), /重置失败/);
});

test("点选过期画面或非方块对象时拒绝返回坐标", async () => {
  const api = await fixture();
  await assert.rejects(api.pick({ frame_id: "old", scene_object_id: "cube-red" }), /过期/);
  await assert.rejects(api.pick({ frame_id: scene.frame_id, scene_object_id: "robot" }), /不合法/);
  const pick = await api.pick({ frame_id: scene.frame_id, scene_object_id: "cube-blue" });
  assert.equal(pick.position_source, "object_center_preview");
});

test("动态组合任务及别名可识别，引用物体不能直接删除", async () => {
  const api = await fixture();
  const red = await register(api);
  const point = await api.savePlacementPoint({ name: "归位点", aliases: [], position: [0.45, 0.15, 0] });
  const task = await api.saveTask({ name: "零件归位", aliases: ["收拾零件"], steps: [
    { action: "GRASP", object_id: red.id },
    { action: "MOVE", position: [0.55, 0, 0.25] },
    { action: "WAIT", seconds: 1 },
    { action: "PLACE", object_id: red.id, placement_point_id: point.id },
  ] });
  const result = await api.parseInstruction("执行收拾零件", "transformer");
  assert.equal(result.action, "RUN_TASK");
  assert.equal(result.task_id, task.id);
  assert.equal(result.normalized_instruction, "执行<TASK>");
  await assert.rejects(api.deleteObject(red.id), /引用/);
  await api.deleteTask(task.id);
  await api.deleteObject(red.id);
});

test("任务必须引用实际物体并提供有效参数", async () => {
  const api = await fixture();
  for (const step of [
    { action: "GRASP", object_id: 99 }, { action: "WAIT", seconds: 0 },
    { action: "MOVE", position: [1, 2] }, { action: "INVENTED" },
  ]) await assert.rejects(api.saveTask({ name: "无效任务", steps: [step] }));
});

test("放置点从表面坐标登记，独立持久保存并保持三位小数", async () => {
  const storage = memoryStorage();
  const api = await fixture(storage);
  assert.deepEqual(await api.listPlacementPoints(), []);
  const pick = await api.pick({ frame_id: scene.frame_id, x: 0.15, y: 0.8 });
  assert.equal(pick.hit_position[2], 0);
  const point = await api.savePlacementPoint({ name: "放置区A", aliases: ["目的地"],
    position: [0.451234, 0.156789, pick.hit_position[2]] });
  assert.deepEqual(point.position, [0.451, 0.157, 0]);
  const reopened = await fixture(storage);
  assert.deepEqual(await reopened.listPlacementPoints(), [point]);
  assert.deepEqual(await reopened.listObjects(), []);
  const cube = await api.pick({ frame_id: scene.frame_id, scene_object_id: "cube-red" });
  assert.equal(cube.hit_position[2], 0.05);
  assert.equal(cube.grasp_position[2], 0.025);
});

test("放置点别名与物体角色可以匹配，缺少目的地及否定指令不能执行", async () => {
  const api = await fixture();
  const red = await register(api);
  const point = await api.savePlacementPoint({ name: "放置区A", aliases: ["归位点"], position: [0.45, 0.15, 0] });
  const result = await api.parseInstruction("把红色积木放到归位点", "transformer");
  assert.equal(result.action, "PLACE");
  assert.equal(result.object_id, red.id);
  assert.equal(result.placement_point_id, point.id);
  assert.equal(result.normalized_instruction, "把<OBJ>放到<OBJ>");
  assert.equal(result.confidence, null);
  assert.equal((await api.parseInstruction("放到归位点", "transformer")).placement_point_id, point.id);
  for (const text of ["放下红色方块", "把红色方块放到未知位置", "不要把红色方块放到归位点", "把未知物体放到归位点"]) {
    assert.notEqual((await api.parseInstruction(text, "transformer")).status, "ready", text);
  }
});

test("方块顶部保存表面高度并复用 PLACE，支撑方块无需登记", async () => {
  const storage = memoryStorage();
  const api = await fixture(storage);
  const red = await register(api);
  const blue = await api.pick({ frame_id: scene.frame_id, scene_object_id: "cube-blue" });
  const point = await api.savePlacementPoint({ name: "蓝块顶部", aliases: ["堆叠点"], position: blue.hit_position });
  assert.equal(point.position[2], 0.05);
  assert.equal(blue.grasp_position[2], 0.025);
  const reopened = await fixture(storage);
  const result = await reopened.parseInstruction("把红色方块放到堆叠点", "transformer");
  assert.equal(result.status, "ready");
  assert.equal(result.action, "PLACE");
  assert.equal(result.placement_point_id, point.id);
  assert.equal(result.object_id, red.id);
  assert.deepEqual(result.placement_position, blue.hit_position);
  assert.equal((await reopened.listObjects()).length, 1);
});

test("任务放下步骤必须选择放置点，引用点不能删除且检查跨库名称冲突", async () => {
  const api = await fixture();
  const red = await register(api);
  const point = await api.savePlacementPoint({ name: "放置区A", aliases: ["归位点"], position: [0.45, 0.15, 0] });
  await assert.rejects(api.saveTask({ name: "缺少目的地", steps: [{ action: "PLACE", object_id: red.id }] }), /放置点/);
  const task = await api.saveTask({ name: "抓放任务", steps: [
    { action: "GRASP", object_id: red.id }, { action: "PLACE", object_id: red.id, placement_point_id: point.id }] });
  await assert.rejects(api.deletePlacementPoint(point.id), /引用/);
  await assert.rejects(api.savePlacementPoint({ name: "红色方块", position: [0.4, 0.2, 0] }), /重复/);
  await assert.rejects(api.saveTask({ name: "归位点", steps: [{ action: "WAIT", seconds: 1 }] }), /重复/);
  await api.deleteTask(task.id);
  const edited = await api.savePlacementPoint({ ...point, position: [0.4, 0.2, 0] });
  assert.deepEqual(edited.position, [0.4, 0.2, 0]);
  await api.deletePlacementPoint(point.id);
  assert.deepEqual(await api.listPlacementPoints(), []);
});

test("后端适配器使用独立放置点接口并保留任务中的目的地 ID", async t => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: options.method === "DELETE" ? 204 : 200, json: async () => ({}) };
  });
  const api = new BackendApi({ apiBaseUrl: "http://localhost:8080/api/v1" });
  await api.listPlacementPoints();
  await api.savePlacementPoint({ name: "目的地", position: [0.456789, 0.2, 0] });
  await api.savePlacementPoint({ id: 7, name: "目的地", position: [0.4, 0.2, 0] });
  await api.deletePlacementPoint(7);
  await api.saveTask({ name: "放下", steps: [{ action: "PLACE", object_id: 1, placement_point_id: 7 }] });
  assert.equal(requests[0].url, "http://localhost:8080/api/v1/placement-points");
  assert.deepEqual(JSON.parse(requests[1].options.body).position, [0.457, 0.2, 0]);
  assert.equal(requests[2].url, "http://localhost:8080/api/v1/placement-points/7");
  assert.equal(requests[3].options.method, "DELETE");
  assert.equal(JSON.parse(requests[4].options.body).steps[0].placement_point_id, 7);
});

test("预览执行明确返回 preview，不伪造机械臂成功或模型置信度", async () => {
  const api = await fixture();
  const result = await api.execute({ action: "GRASP", object_id: 1 });
  assert.equal(result.status, "preview");
  assert.match(result.message, /没有实际运动/);
  const objects = await api.listObjects();
  assert.equal(previewParse("抓取红色方块", objects, [], "transformer").source, "preview_rules");
});

test("后端适配器发送真实接口请求并保留错误，不降级为预览", async t => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    return { ok: false, status: 409, json: async () => ({ message: "名称冲突" }) };
  });
  const api = new BackendApi({ apiBaseUrl: "http://localhost:8000/api/v1/" });
  await assert.rejects(api.saveObject({ id: 2, name: "测试", position: [0.55, 0, 0.025] }), /名称冲突/);
  assert.equal(requests[0].url, "http://localhost:8000/api/v1/objects/2");
  assert.equal(requests[0].options.method, "PUT");
  assert.equal(JSON.parse(requests[0].options.body).name, "测试");
});

test("后端场景相对图像地址按 API 地址解析", async t => {
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, status: 200,
    json: async () => ({ ...scene, image_url: "/api/v1/simulation/frame.png?frame_id=1" }) }));
  const api = new BackendApi({ apiBaseUrl: "http://localhost:8000/api/v1" });
  assert.equal((await api.getScene()).image_url, "http://localhost:8000/api/v1/simulation/frame.png?frame_id=1");
});

test("仿真启动配置覆盖旧预览设置，并跟随实际启动端口", async () => {
  const config = await loadRuntimeConfig({ mode: "preview", apiBaseUrl: "http://localhost:8000/api/v1" },
    async () => ({ ok: true, status: 200, json: async () => ({ mode: "backend", apiBaseUrl: "/api/v1" }) }),
    "http://127.0.0.1:8090/");
  assert.equal(config.mode, "backend");
  assert.equal(config.apiBaseUrl, "http://127.0.0.1:8090/api/v1");
});

test("缺少运行配置可使用显式设置，配置服务出错不会静默降级", async () => {
  const original = { mode: "backend", apiBaseUrl: "http://127.0.0.1:8080/api/v1" };
  assert.equal(await loadRuntimeConfig(original, async () => ({ status: 404 }), "http://localhost/"), original);
  await assert.rejects(loadRuntimeConfig(original, async () => ({ status: 503 }), "http://localhost/"), /启动配置/);
});
