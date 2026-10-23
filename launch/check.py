"""检查公开工程的依赖、模型、相机和数据库，不启动网页或浏览器。"""

import argparse
from contextlib import ExitStack
import importlib
from importlib.metadata import version
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    print(f"[检查] Python {platform.python_version()} / {platform.system()} {platform.machine()}", flush=True)
    modules = {}
    for name in ("numpy", "torch", "pybullet"):
        try:
            modules[name] = importlib.import_module(name)
        except (ImportError, OSError) as error:
            print(f"[失败] 无法加载 {name}：{error}", file=sys.stderr)
            print("请先执行：.venv/bin/python -m pip install -r requirements-cpu.txt", file=sys.stderr)
            return 1
        print(f"[通过] {name} {version(name)}", flush=True)

    required = (
        "frontend/index.html", "frontend/styles.css", "frontend/app.js", "frontend/api.js",
        "frontend/config.js", "frontend/coordinates.js", "frontend/frame_stream.js",
        "llm/checkpoints/intent_v2.pt", "robot/models/franka_panda/panda.urdf",
    )
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        print(f"[失败] 工程文件不完整：{', '.join(missing)}", file=sys.stderr)
        return 1

    from backend.server import StudioApplication
    from backend.simulation import SimulationService
    from backend.store import StudioStore
    from llm.model import IntentParser

    modules["torch"].set_num_threads(4)
    parser = IntentParser(device="cpu")
    prediction = parser.predict("抓取<OBJ>")
    if not prediction["accepted"] or prediction["action"] != "GRASP":
        raise RuntimeError(f"模型自检未通过：{prediction}")
    print("[通过] 已保存的四层 Transformer 可在 CPU 上识别抓取意图", flush=True)

    # 使用工程目录中的临时文件验证写入权限，避免读取或覆盖用户登记库。
    with tempfile.TemporaryDirectory(prefix=".startup-check-", dir=ROOT / "backend") as directory:
        with ExitStack() as cleanup:
            store = StudioStore(Path(directory) / "registry.sqlite3")
            cleanup.callback(store.close)
            simulation = SimulationService(real_time=False, width=320, height=200, fps=2)
            cleanup.callback(simulation.close)
            simulation.start()
            app = StudioApplication(simulation, store, parser)
            if app.dispatch("GET", "/api/v1/health").payload["status"] != "ok":
                raise RuntimeError("执行服务尚未就绪")
            scene = simulation.snapshot()
            if len(scene["objects"]) != 3:
                raise RuntimeError("场景未包含三个方块")
            if not simulation.frame(scene["frame_id"])["png"].startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("相机没有生成有效 PNG 图像")
            print("[通过] 仓库内 Panda 模型、三色方块和仿真相机可用", flush=True)
            for endpoint in ("objects", "placement-points", "tasks"):
                if app.dispatch("GET", "/api/v1/" + endpoint).payload != []:
                    raise RuntimeError(f"新建登记库的 {endpoint} 不是空列表")
            print("[通过] SQLite 可建库，物体、放置点和任务初始为空", flush=True)

    print("启动自检全部通过。可执行：.venv/bin/python -B launch/web.py", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"[失败] {error}", file=sys.stderr)
        raise SystemExit(1)
