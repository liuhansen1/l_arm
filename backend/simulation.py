"""单线程驱动物理仿真，向 HTTP 线程提供不可变相机帧和执行结果。"""

from collections import OrderedDict
from concurrent.futures import Future
import copy
import json
import math
import queue
import struct
import threading
import time
import uuid
import zlib

import numpy as np
import pybullet as p

from backend import RequestError
from robot.demo import create_scene
from robot.grasp import FINGER_JOINTS, GRASP_LINK, PandaController, grasp_cube
from robot.skills import move_safely, place_cube, placement_support


def png_bytes(pixels):
    height, width, channels = pixels.shape

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    rows = b"".join(b"\0" + row.tobytes() for row in pixels)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                         {3: 2, 4: 6}[channels], 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))


def validate_position(value, *, workspace=True):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) for v in value)):
        raise RequestError("坐标必须是三个有限数字，单位为米。", 422)
    value = [round(float(v), 3) + 0.0 for v in value]
    # 采集和登记不限制区域，执行时才检查演示工作区。
    if workspace and not (-0.1 <= value[0] <= 0.9 and -0.6 <= value[1] <= 0.6
            and 0 <= value[2] <= 0.6):
        raise RequestError("坐标超出本演示工作区：X[-0.1,0.9]、Y[-0.6,0.6]、Z[0,0.6] 米。", 422)
    return value


def validate_placement_position(value):
    return validate_position(value)


class Cancelled(Exception):
    pass


class LiveController(PandaController):
    def __init__(self, service, client, robot, home):
        super().__init__(client, robot, home, real_time=False)
        self.service = service
        self.last_position = None
        self.deadline = time.monotonic()
        self.simulation_steps = 0

    def step(self, count=1):
        for _ in range(count):
            if self.service.closing.is_set() or self.service.cancel.is_set():
                raise Cancelled()
            super().step()
            self.simulation_steps += 1
            self.deadline += 1 / 240
            # 渲染后先补齐物理步，不能让慢渲染挤占后续的所有控制周期。
            if not self.service.real_time or self.deadline >= time.monotonic() - 1 / 240:
                self.service.render()
            if self.service.real_time:
                delay = self.deadline - time.monotonic()
                if delay > 0:
                    self.service.cancel.wait(delay)
                elif delay < -1:
                    self.deadline = time.monotonic()

    def move_to(self, position, seconds=2):
        self.last_position = list(position)
        return super().move_to(position, seconds)


class SimulationService:
    def __init__(self, *, real_time=True, width=640, height=400, fps=24, gui=False):
        self.real_time = real_time
        self.width, self.height, self.fps = width, height, fps
        self.gui = gui
        self.lock = threading.RLock()
        self.frame_ready = threading.Condition(self.lock)
        self.commands = queue.Queue()
        self.ready = threading.Event()
        self.closing = threading.Event()
        self.cancel = threading.Event()
        self.thread = None
        self.failure = None
        self.frames = OrderedDict()
        self.jobs = OrderedDict()
        self.active_job = None
        self.requires_reset = False
        self.control_pending = False
        self.scene_id = None
        self.state_version = 0
        self.held_object_id = None
        self.frame_number = 0
        self.last_render = 0
        self.client = None
        self.controller = None
        self.bodies = {}
        self.view = p.computeViewMatrixFromYawPitchRoll([0.3, 0, 0.3], 1.5, 45, -32, 0, 2)
        self.projection = p.computeProjectionMatrixFOV(48, width / height, 0.02, 4)
        self.view_array = np.array(self.view).reshape(4, 4, order="F")
        self.projection_array = np.array(self.projection).reshape(4, 4, order="F")
        self.inverse_camera = np.linalg.inv(self.projection_array @ self.view_array)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="panda-simulation", daemon=True)
        self.thread.start()
        if not self.ready.wait(15):
            self.close()
            raise RuntimeError("PyBullet 场景初始化超时")
        self.check_ready()

    def check_ready(self):
        if self.failure:
            raise RequestError(f"仿真已停止：{self.failure}", 503)
        if not self.ready.is_set() or self.closing.is_set():
            raise RequestError("仿真尚未就绪或正在关闭。", 503)

    def close(self):
        self.closing.set()
        self.cancel.set()
        with self.frame_ready:
            self.frame_ready.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("仿真线程未能正常退出")

    def create(self):
        p.resetSimulation()
        robot, home, self.bodies = create_scene()
        p.setPhysicsEngineParameter(numSolverIterations=150)
        self.controller = LiveController(self, self.client, robot, home)
        # 启动/重置时先离线落稳，不让浏览器看到下落中的初始场景。
        for _ in range(240):
            PandaController.step(self.controller)
        self.controller.deadline = time.monotonic()
        if self.gui:
            p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0)
            p.resetDebugVisualizerCamera(1.5, 45, -32, [0.3, 0, 0.3])
        with self.lock:
            self.scene_id = uuid.uuid4().hex[:12]
            self.frames.clear()
            self.requires_reset = False
            self.held_object_id = None
            self.state_version += 1
        self.render(force=True)

    def run(self):
        try:
            self.client = p.connect(p.GUI if self.gui else p.DIRECT)
            if self.client < 0:
                raise RuntimeError("无法连接 PyBullet")
            self.create()
            self.ready.set()
            while not self.closing.is_set():
                try:
                    kind, value, future = self.commands.get_nowait()
                except queue.Empty:
                    try:
                        self.controller.step()
                    except Cancelled:
                        pass
                    if not self.real_time:
                        self.closing.wait(0.002)
                    continue
                try:
                    if kind == "grasp":
                        self.run_grasp(value)
                        result = None
                    elif kind == "program":
                        self.run_program(value)
                        result = None
                    elif kind == "stop":
                        self.cancel.clear()
                        self.controller.arm_target = [p.getJointState(self.controller.robot, j)[0] for j in range(7)]
                        self.controller.opening = sum(p.getJointState(self.controller.robot, j)[0] for j in FINGER_JOINTS) / 2
                        self.controller.step()
                        with self.lock:
                            self.state_version += 1
                        result = {"status": "stopped", "message": "机械臂已停止并保持当前位置。"}
                    elif kind == "reset":
                        self.cancel.clear()
                        self.create()
                        result = {"status": "ready", "message": "机械臂和三色方块已重置，可以重新点选测试。"}
                    else:
                        raise RuntimeError("未知仿真命令")
                    if future is not None:
                        with self.lock:
                            self.control_pending = False
                        future.set_result(result)
                except Exception as error:
                    if future is not None:
                        with self.lock:
                            self.control_pending = False
                        future.set_exception(error)
                    else:
                        raise
        except Exception as error:
            self.failure = str(error)
            self.ready.set()
        finally:
            with self.frame_ready:
                self.frame_ready.notify_all()
            if self.client is not None and p.isConnected(self.client):
                p.disconnect(self.client)
            while True:
                try:
                    _, _, future = self.commands.get_nowait()
                    if future is not None:
                        future.set_exception(RequestError("仿真服务已停止。", 503))
                except queue.Empty:
                    break

    def render(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_render < 1 / self.fps:
            return
        _, _, rgba, depth, segmentation = p.getCameraImage(
            self.width, self.height, viewMatrix=self.view, projectionMatrix=self.projection,
            renderer=p.ER_BULLET_HARDWARE_OPENGL if self.gui else p.ER_TINY_RENDERER,
            lightDirection=[-3, -4, 8], shadow=0,
            flags=p.ER_SEGMENTATION_MASK_OBJECT_AND_LINKINDEX)
        rgba = np.asarray(rgba, dtype=np.uint8).reshape(self.height, self.width, 4)
        depth = np.asarray(depth).reshape(self.height, self.width).copy()
        segmentation = np.asarray(segmentation).reshape(self.height, self.width).copy()
        objects = []
        poses = {}
        for color, body in self.bodies.items():
            position, orientation = p.getBasePositionAndOrientation(body)
            rotation = np.array(p.getMatrixFromQuaternion(orientation)).reshape(3, 3)
            # 使用上表面中心的投影作为颜色按钮的点选位置。
            top = np.array(position) + rotation @ np.array([0, 0, 0.025])
            clip = self.projection_array @ self.view_array @ np.append(top, 1)
            screen = clip[:2] / clip[3]
            scene_object_id = f"cube-{color}"
            objects.append({"scene_object_id": scene_object_id, "color": color,
                            "screen_position": [float((screen[0] + 1) / 2), float((1 - screen[1]) / 2)]})
            poses[scene_object_id] = (np.array(position), rotation, body)
        self.frame_number += 1
        frame_id = f"{self.scene_id}-{self.frame_number}"
        metadata = {"frame_id": frame_id, "scene_id": self.scene_id,
                    "width": self.width, "height": self.height,
                    "image_url": f"/api/v1/simulation/frame.png?frame_id={frame_id}",
                    "coordinate_frame": "world", "unit": "m", "objects": objects,
                    "camera": {"view_matrix": self.view, "projection_matrix": self.projection},
                    "target_fps": self.fps, "simulation_time": self.controller.simulation_steps / 240}
        frame = {"metadata": metadata, "png": png_bytes(rgba[:, :, :3]), "depth": depth,
                 "segmentation": segmentation, "poses": poses}
        with self.frame_ready:
            self.frames[frame_id] = frame
            while len(self.frames) > max(24, self.fps * 3):
                self.frames.popitem(last=False)
            self.frame_ready.notify_all()
        self.last_render = now

    def stream_packets(self):
        """每帧：4字节大端 JSON 长度、JSON 元数据（含 image_size）、PNG。"""
        previous = None
        while not self.closing.is_set():
            with self.frame_ready:
                self.frame_ready.wait_for(
                    lambda: self.closing.is_set() or self.failure or (
                        self.frames and next(reversed(self.frames)) != previous), timeout=1)
                if self.closing.is_set() or self.failure:
                    return
                if not self.frames:
                    continue
                frame = next(reversed(self.frames.values()))
                if frame["metadata"]["frame_id"] == previous:
                    continue
                metadata = copy.deepcopy(frame["metadata"])
                metadata.update(is_running=self.active_job is not None, requires_reset=self.requires_reset,
                                state_version=self.state_version, held_object_id=self.held_object_id,
                                image_size=len(frame["png"]))
                previous = metadata["frame_id"]
            data = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            yield struct.pack(">I", len(data)) + data + frame["png"]

    def snapshot(self):
        self.check_ready()
        with self.lock:
            if not self.frames:
                raise RequestError("相机帧尚未生成。", 503)
            metadata = copy.deepcopy(next(reversed(self.frames.values()))["metadata"])
            metadata.update(is_running=self.active_job is not None, requires_reset=self.requires_reset,
                            state_version=self.state_version, held_object_id=self.held_object_id)
            return metadata

    def frame(self, frame_id):
        self.check_ready()
        if not isinstance(frame_id, str) or not frame_id:
            raise RequestError("必须提供有效的相机帧 ID。", 422)
        with self.lock:
            frame = self.frames.get(frame_id)
            if frame is None:
                raise RequestError("画面已过期，请刷新场景。", 409)
            return frame

    def pick(self, value):
        frame = self.frame(value.get("frame_id"))
        for axis in ("x", "y"):
            v = value.get(axis)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v < 1:
                raise RequestError("点选坐标必须是 [0,1) 范围内的数字。", 422)
        x, y = int(value["x"] * self.width), int(value["y"] * self.height)
        body = int(frame["segmentation"][y, x])
        selected = next((name for name, pose in frame["poses"].items() if body >= 0 and pose[2] == (body & 0xFFFFFF)), None)
        if value.get("scene_object_id", selected) != selected:
            raise RequestError("预期物体与实际像素不一致，请在画面上重新点选。", 409)
        ndc = np.array([2 * (x + 0.5) / self.width - 1,
                        1 - 2 * (y + 0.5) / self.height,
                        2 * float(frame["depth"][y, x]) - 1, 1])
        world = self.inverse_camera @ ndc
        hit = world[:3] / world[3]
        source = "depth_surface"
        normal = None
        if selected is not None:
            center, rotation, _ = frame["poses"][selected]
            local = rotation.T @ (hit - center)
            axis = int(np.argmax(np.abs(local)))
            local_normal = np.zeros(3)
            local_normal[axis] = 1 if local[axis] >= 0 else -1
            normal = rotation @ local_normal
            grasp = hit - normal * 0.025
            source = "depth_surface_offset"
        elif body < 0 or (body & 0xFFFFFF) == 0:
            # 地面或无深度的背景采用明确的 Z=0 工作平面，不假造物体。
            near = self.inverse_camera @ np.array([ndc[0], ndc[1], -1, 1])
            far = self.inverse_camera @ np.array([ndc[0], ndc[1], 1, 1])
            near, far = near[:3] / near[3], far[:3] / far[3]
            direction = far - near
            if abs(direction[2]) < 1e-10 or -near[2] / direction[2] < 0:
                raise RequestError("该视线与工作平面不相交，请调整相机。", 422)
            hit = near + direction * (-near[2] / direction[2])
            hit[2] = 0
            grasp = hit
            source = "ground_plane_projection"
        else:
            grasp = hit
        return {"scene_object_id": selected, "color": selected.removeprefix("cube-") if selected else "point",
                "frame_id": value["frame_id"], "coordinate_frame": "world",
                "screen_position": [value["x"], value["y"]],
                "hit_position": validate_position(hit.tolist(), workspace=False),
                "surface_normal": normal.tolist() if normal is not None else None,
                "grasp_position": validate_position(grasp.tolist(), workspace=False), "position_source": source}

    def submit_grasp(self, scene_object_id, position, frame_id=None):
        self.check_ready()
        position = validate_position(position)
        with self.lock:
            if self.active_job is not None or self.control_pending:
                raise RequestError("已有抓取测试正在执行。", 409)
            if self.requires_reset:
                raise RequestError("请先重置场景，再点选并进行下一次抓取测试。", 409)
            if self.held_object_id is not None:
                raise RequestError("夹爪正持有物体，请先放下或重置后再试抓。", 409)
            if scene_object_id is not None and (not isinstance(scene_object_id, str) or scene_object_id not in {f"cube-{color}" for color in self.bodies}):
                raise RequestError("场景中没有这个方块。", 404)
            if frame_id is not None and (not isinstance(frame_id, str) or not frame_id.startswith(f"{self.scene_id}-")):
                raise RequestError("场景已重置，请重新点选物体。", 409)
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "action": "GRASP", "status": "queued", "message": "抓取测试已接收。",
                   "scene_object_id": scene_object_id, "requested_position": position,
                   "current_step": 0, "total_steps": 5}
            self.jobs[job_id] = job
            while len(self.jobs) > 100:
                self.jobs.popitem(last=False)
            self.active_job = job_id
            self.state_version += 1
            self.commands.put(("grasp", job_id, None))
            return copy.deepcopy(job)

    def run_grasp(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            job.update(status="running", message="正在准备抓取测试。")
            position = list(job["requested_position"])
            body = self.bodies[job["scene_object_id"].removeprefix("cube-")] if job["scene_object_id"] else None
        initial_positions = {target: list(p.getBasePositionAndOrientation(target)[0]) for target in self.bodies.values()}
        result = {"requested_position": position, "object_initial_position": None,
                  "coordinate_error_m": None,
                  "minimum_lift_m": None, "held_seconds": None}

        def progress(step, message):
            with self.lock:
                job.update(current_step=step, message=f"{step}/5 {message}")

        try:
            lift = grasp_cube(self.controller, body, position=position, progress=progress, candidates=self.bodies.values())
            result.update(success=True, minimum_lift_m=lift, held_seconds=3)
            status, message = "succeeded", f"抓取成功：最低抬升 {lift * 100:.1f} cm，并保持双指接触 3 秒。"
        except Cancelled:
            result["success"] = False
            status, message = "cancelled", "抓取测试已停止。"
        except (RuntimeError, ValueError, p.error) as error:
            result["success"] = False
            status, message = "failed", f"抓取失败：{error}"
        verified_body = body if body is not None else self.controller.grasped_target
        result.update(final_lift_m=None, bilateral_contact=False, detected_scene_object_id=None)
        if verified_body is not None:
            initial = initial_positions[verified_body]
            actual = p.getBasePositionAndOrientation(verified_body)[0]
            result.update(object_initial_position=initial, coordinate_error_m=math.dist(position, initial),
                          final_lift_m=actual[2] - initial[2], bilateral_contact=self.controller.fingers_touch(verified_body),
                          detected_scene_object_id=next(f"cube-{color}" for color, target in self.bodies.items() if target == verified_body))
        if self.controller.last_position is not None:
            actual_grasp = p.getLinkState(self.controller.robot, GRASP_LINK, computeForwardKinematics=True)[4]
            result["end_effector_error_m"] = math.dist(actual_grasp, self.controller.last_position)
        self.render(force=True)
        with self.lock:
            job.update(status=status, message=f"{message} 再次测试请先重置场景。", result=result)
            self.active_job = None
            self.requires_reset = True
            self.state_version += 1

    def validate_program(self, steps):
        """只读元数据检查，HTTP 线程不能调用任何物理查询。"""
        self.check_ready()
        with self.lock:
            if self.active_job is not None or self.control_pending:
                raise RequestError("已有任务或控制操作正在执行。", 409)
            if self.requires_reset:
                raise RequestError("请先重置场景，再执行任务。", 409)
            if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
                raise RequestError("任务需要 1 至 50 个步骤。", 422)
            held = self.held_object_id
            poses = copy.deepcopy(next(reversed(self.frames.values()))["poses"])
            held_scene = next((step["object"]["scene_object_id"] for step in steps
                               if held is not None and step.get("object", {}).get("id") == held), None)
            for step in steps:
                action = step.get("action")
                if action == "GRASP":
                    if held is not None:
                        raise RequestError("夹爪已经持有物体，请先放下再抓取。", 409)
                    obj = step.get("object")
                    if not obj:
                        raise RequestError("抓取缺少登记目标。", 422)
                    if obj["color"] != "point" and obj["scene_object_id"] not in {f"cube-{color}" for color in self.bodies}:
                        raise RequestError("场景中没有这个物体。", 404)
                    validate_position(step.get("position"))
                    held = obj["id"]
                    held_scene = obj["scene_object_id"]
                elif action == "PLACE":
                    if held is None or (step.get("object_id") is not None and held != step["object_id"]):
                        raise RequestError("放置目标与当前持物不一致，或夹爪没有持物。", 409)
                    if not step.get("placement_point"):
                        raise RequestError("放下步骤必须选择已登记的放置点，请编辑任务补齐。", 422)
                    position = validate_placement_position(step.get("placement_position"))
                    if position[2] > 0.003:
                        excluded = (poses[held_scene][2],) if held_scene in poses else ()
                        try:
                            placement_support(position, poses, excluded=excluded)
                        except ValueError as error:
                            raise RequestError(str(error), 422) from error
                    if held_scene in poses:
                        # 只用于后续步骤的预检查；执行时仍读取真实物理姿态。
                        poses[held_scene] = (np.array([position[0], position[1], position[2] + 0.025]),
                                             np.eye(3), poses[held_scene][2])
                    held = None
                    held_scene = None
                elif action == "MOVE":
                    validate_position(step.get("position"))
                elif action == "WAIT":
                    seconds = step.get("seconds")
                    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 60:
                        raise RequestError("等待时间必须在 0 至 60 秒之间。", 422)
                else:
                    raise RequestError("任务包含不支持的动作。", 422)

    def submit_program(self, steps, *, action="RUN_TASK", expected_state=None):
        with self.lock:
            if expected_state is not None and expected_state != self.state_version:
                raise RequestError("机械臂状态已改变，请重新解析。", 409)
            self.validate_program(steps)
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "action": action, "status": "queued", "message": "执行任务已接收。",
                   "current_step": 0, "total_steps": len(steps), "steps": copy.deepcopy(steps), "mode": "program"}
            self.jobs[job_id] = job
            while len(self.jobs) > 100:
                self.jobs.popitem(last=False)
            self.active_job = job_id
            self.state_version += 1
            self.commands.put(("program", job_id, None))
            return copy.deepcopy(job)

    def run_program(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            job.update(status="running", message="正在执行任务。")
        results = []
        status, message = "succeeded", "所有步骤已实际完成。"
        try:
            for index, step in enumerate(job["steps"], 1):
                self.controller.step()
                action = step["action"]
                with self.lock:
                    job.update(current_step=index, message=f"{index}/{len(job['steps'])} 正在执行 {action}")
                if action == "GRASP":
                    obj = step["object"]
                    body = self.bodies.get(obj["scene_object_id"].removeprefix("cube-")) if obj["color"] != "point" else None

                    def progress(part, detail):
                        with self.lock:
                            job["message"] = f"{index}/{len(job['steps'])} GRASP · {part}/5 {detail}"

                    lift = grasp_cube(self.controller, body, position=step["position"], progress=progress, candidates=self.bodies.values())
                    with self.lock:
                        self.held_object_id = obj["id"]
                    detail = {"minimum_lift_m": lift, "held_seconds": 3,
                              "requested_position": step["position"]}
                elif action == "MOVE":
                    detail = move_safely(self.controller, step["position"], self.controller.grasped_target)
                elif action == "PLACE":
                    detail = place_cube(self.controller, self.controller.grasped_target, self.bodies.values(),
                                        step["placement_position"])
                    support_body = detail.pop("support_body_id")
                    detail["support_scene_object_id"] = next((f"cube-{color}" for color, body in self.bodies.items()
                                                              if body == support_body), None)
                    detail["placement_point_id"] = step["placement_point_id"]
                    with self.lock:
                        self.held_object_id = None
                elif action == "WAIT":
                    self.controller.wait(step["seconds"])
                    if self.controller.grasped_target is not None and not self.controller.fingers_touch(self.controller.grasped_target):
                        raise RuntimeError("等待过程中夹持物滑落")
                    detail = {"seconds": step["seconds"]}
                results.append({"step": index, "action": action, "status": "succeeded", "result": detail})
                with self.lock:
                    job["step_results"] = copy.deepcopy(results)
        except Cancelled:
            status, message = "cancelled", "任务已停止，后续步骤未执行。"
        except (RuntimeError, ValueError, p.error) as error:
            status, message = "failed", f"第 {job['current_step']} 步失败：{error}；后续步骤未执行。"
        if status != "succeeded":
            results.append({"step": job["current_step"], "status": status, "message": message})
        self.render(force=True)
        with self.lock:
            self.requires_reset = status != "succeeded"
            self.state_version += 1
            job.update(status=status, message=message, step_results=results,
                       requires_reset=self.requires_reset,
                       result={"success": status == "succeeded", "steps": copy.deepcopy(results),
                               "held_object_id": self.held_object_id})
            self.active_job = None

    def get_job(self, job_id):
        self.check_ready()
        with self.lock:
            if job_id not in self.jobs:
                raise RequestError("任务不存在或结果已过期。", 404)
            return copy.deepcopy(self.jobs[job_id])

    def control(self, kind):
        self.check_ready()
        if kind not in ("stop", "reset"):
            raise RequestError("不支持的仿真控制操作。", 422)
        with self.lock:
            if self.control_pending:
                raise RequestError("正在处理停止或重置请求，请稍后重试。", 409)
            self.control_pending = True
            self.cancel.set()
            future = Future()
            self.commands.put((kind, None, future))
        try:
            return future.result(timeout=10)
        except TimeoutError as error:
            raise RequestError("仿真控制响应超时，请检查服务终端。", 503) from error
