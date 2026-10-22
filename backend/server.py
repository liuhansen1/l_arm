"""同一进程启动网页、SQLite 登记接口与实际 PyBullet 仿真。"""

import argparse
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from backend import RequestError
from backend.simulation import SimulationService
from backend.store import StudioStore
from backend.instructions import InstructionService, prepare_steps

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "backend/data/registry.sqlite3"


@dataclass
class Response:
    payload: object
    status: int = 200
    content_type: str = "application/json; charset=utf-8"


class StudioApplication:
    def __init__(self, simulation, store, parser=None):
        self.simulation = simulation
        self.store = store
        self.instructions = InstructionService(store, simulation, parser)

    def dispatch(self, method, url, body=None):
        parsed = urlsplit(url)
        path = parsed.path.removeprefix("/api/v1")
        body = {} if body is None else body
        if not isinstance(body, dict):
            raise RequestError("请求体必须是 JSON 对象。", 422)
        if method == "GET" and path == "/health":
            self.simulation.check_ready()
            capabilities = ["live_scene", "streaming_camera", "arbitrary_pick", "coordinate_grasp", "object_registry", "task_execution", "placement_points", "memory_clear"]
            if self.instructions.parser is not None:
                capabilities.append("language_parse")
            return Response({"status": "ok", "capabilities": capabilities, "model": "transformer"})
        if method == "GET" and path == "/simulation/scene":
            return Response(self.simulation.snapshot())
        if method == "GET" and path == "/simulation/frame.png":
            frame_id = parse_qs(parsed.query).get("frame_id", [None])[0]
            return Response(self.simulation.frame(frame_id)["png"], content_type="image/png")
        if method == "POST" and path == "/simulation/pick":
            return Response(self.simulation.pick(body))
        if method == "POST" and path in ("/simulation/stop", "/simulation/reset"):
            return Response(self.simulation.control(path.rsplit("/", 1)[1]))
        if method == "POST" and path == "/memory/clear":
            return Response(self.instructions.clear_memory())
        if method == "GET" and path == "/objects":
            return Response(self.store.objects())
        if method == "POST" and path == "/objects":
            return Response(self.store.save_object(body), 201)
        if method == "GET" and path == "/placement-points":
            return Response(self.store.placement_points())
        if method == "POST" and path == "/placement-points":
            return Response(self.store.save_placement_point(body), 201)
        if method == "GET" and path == "/tasks":
            return Response(self.store.tasks())
        if method == "POST" and path == "/tasks":
            return Response(self.store.save_task(body), 201)
        collections = {"objects": (self.store.save_object, self.store.delete_object),
                       "tasks": (self.store.save_task, self.store.delete_task),
                       "placement-points": (self.store.save_placement_point, self.store.delete_placement_point)}
        for collection, (save, delete) in collections.items():
            prefix = f"/{collection}/"
            if path.startswith(prefix) and method in ("PUT", "DELETE"):
                try:
                    identifier = int(path[len(prefix):])
                except ValueError as error:
                    raise RequestError("记录 ID 必须为整数。", 422) from error
                if method == "PUT":
                    return Response(save(body, identifier))
                delete(identifier)
                return Response(None, 204)
        if method == "POST" and path == "/jobs":
            if body.get("source") == "language":
                result = self.instructions.execute(body)
                return Response(result, 202 if "id" in result else 200)
            if body.get("source") == "task_builder" and body.get("action") == "RUN_TASK":
                with self.store.lock:
                    steps = prepare_steps("RUN_TASK", self.store.snapshot(), task_id=body.get("task_id"))
                    return Response(self.simulation.submit_program(steps), 202)
            if body.get("source") != "coordinate_test":
                raise RequestError("执行需要有效语言解析结果、已保存任务或明确的坐标试抓请求。", 422)
            if body.get("action") != "GRASP":
                raise RequestError("坐标试抓只支持 GRASP。", 422)
            object_id = body.get("object_id")
            obj = self.store.object(object_id) if object_id is not None else None
            scene_id = body.get("scene_object_id") or (obj["scene_object_id"] if obj else None)
            if obj and obj["scene_object_id"] != scene_id:
                raise RequestError("物体 ID 与所选方块不一致。", 409)
            if body.get("coordinate_frame", "world") != "world":
                raise RequestError("当前只支持世界坐标。", 422)
            if body.get("source") == "coordinate_test":
                if "grasp_position" not in body:
                    raise RequestError("坐标测试必须提供编辑后的 grasp_position。", 422)
                position = body["grasp_position"]
            elif obj:
                position = obj["position"]
            else:
                raise RequestError("请登记目标或提交坐标抓取请求。", 422)
            target = None if obj and obj["color"] == "point" else scene_id
            return Response(self.simulation.submit_grasp(target, position, body.get("frame_id")), 202)
        if method == "GET" and path.startswith("/jobs/"):
            return Response(self.simulation.get_job(path.removeprefix("/jobs/")))
        if method == "POST" and path == "/instructions/parse":
            return Response(self.instructions.parse(body))
        raise RequestError("接口不存在。", 404)


class StudioHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, application, **kwargs):
        self.application = application
        super().__init__(*args, directory=str(ROOT / "frontend"), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if origin in ("http://127.0.0.1:8080", "http://localhost:8080"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def handle_api(self):
        try:
            body = None
            if self.command in ("POST", "PUT"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise RequestError("Content-Length 不合法。") from error
                if not 0 <= length <= 65536:
                    raise RequestError("请求体超过 64 KiB。", 413)
                try:
                    body = json.loads(self.rfile.read(length)) if length else {}
                except (ValueError, UnicodeDecodeError) as error:
                    raise RequestError("请求体不是有效的 UTF-8 JSON。") from error
            response = self.application.dispatch(self.command, self.path, body)
        except RequestError as error:
            response = Response({"message": str(error)}, error.status)
        except Exception as error:
            self.log_error("API error: %r", error)
            response = Response({"message": "服务内部错误，请查看运行终端。"}, 500)
        data = response.payload if isinstance(response.payload, bytes) else json.dumps(response.payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if response.status == 204:
            data = b""
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器切换页面或取消图像加载时连接会正常中断。
            pass

    def do_GET(self):
        if urlsplit(self.path).path == "/api/v1/simulation/stream":
            self.stream_camera()
        elif urlsplit(self.path).path.startswith("/api/v1/"):
            self.handle_api()
        elif self.path == "/studio-config.json":
            data = json.dumps({"mode": "backend", "apiBaseUrl": "/api/v1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            super().do_GET()

    def stream_camera(self):
        try:
            self.application.simulation.check_ready()
        except RequestError as error:
            data = json.dumps({"message": str(error)}, ensure_ascii=False).encode("utf-8")
            self.send_response(error.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.connection.settimeout(5)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-panda-frames")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for packet in self.application.simulation.stream_packets():
                self.wfile.write(packet)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # 页面关闭、取消流或接收过慢时结束此连接，不阻塞物理线程。
            pass

    do_POST = handle_api
    do_PUT = handle_api
    do_DELETE = handle_api

    def log_message(self, format, *args):
        if "/simulation/frame.png" not in self.path and "/simulation/scene" not in self.path and "/simulation/stream" not in self.path and "/jobs/" not in self.path:
            super().log_message(format, *args)


def open_browser(url):
    try:
        if not webbrowser.open(url):
            print(f"未能自动打开浏览器，请手动访问：{url}", flush=True)
    except (OSError, webbrowser.Error) as error:
        print(f"自动打开浏览器失败：{error}。请手动访问：{url}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--gui", action="store_true", help="同时打开 PyBullet 原生窗口")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 至 65535 之间")
    simulation = SimulationService(gui=args.gui)
    store = None
    server = None
    try:
        started = time.perf_counter()
        print("[启动] 正在读取登记库并加载 Transformer…", flush=True)
        store = StudioStore(args.database)
        application = StudioApplication(simulation, store)
        # 连续相机渲染会拖慢 PyTorch 导入；模型先就绪，再启动物理线程。
        application.instructions.load()
        print(f"[启动] 登记库和模型已就绪，耗时 {time.perf_counter() - started:.2f} 秒。", flush=True)
        phase_started = time.perf_counter()
        print("[启动] 正在初始化机械臂和相机首帧…", flush=True)
        simulation.start()
        print(f"[启动] 仿真已就绪，耗时 {time.perf_counter() - phase_started:.2f} 秒。", flush=True)
        phase_started = time.perf_counter()
        print("[启动] 正在启动网页服务…", flush=True)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(StudioHandler, application=application))
        server.daemon_threads = False
        print(f"[启动] 网页服务已就绪，耗时 {time.perf_counter() - phase_started:.2f} 秒。", flush=True)
        url = f"http://127.0.0.1:{args.port}"
        print(f"[启动] 后端准备完成，共 {time.perf_counter() - started:.2f} 秒。", flush=True)
        print(f"PyBullet 已运行，打开工作台：{url}", flush=True)
        print("点选并登记目标 → 中文指令解析 / 任务编排 → 实际执行。Ctrl+C 停止服务。", flush=True)
        if not args.no_browser:
            # Chrome 的启动调用可能等待数秒，不应阻塞 HTTP 请求处理。
            threading.Thread(target=open_browser, args=(url,), name="studio-browser", daemon=True).start()
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print("\n正在停止网页和仿真。", flush=True)
        return 0
    except (OSError, RuntimeError, RequestError) as error:
        print(f"启动失败：{error}")
        return 1
    finally:
        simulation.close()
        if server is not None:
            server.server_close()
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
