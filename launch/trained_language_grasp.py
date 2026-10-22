"""用新版小模型及用户登记库解析并执行指令，不自动导入场景目标。"""

import argparse
import json
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import RequestError
from backend.server import DEFAULT_DATABASE, StudioApplication
from backend.simulation import SimulationService
from backend.store import StudioStore
from llm.model import INTENT_CHECKPOINT, IntentParser


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instruction", nargs="?")
    parser.add_argument("--checkpoint", type=Path, default=INTENT_CHECKPOINT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)
    simulation, store = None, None
    try:
        import torch
        torch.set_num_threads(4)
        predictor = IntentParser(args.checkpoint, args.device)
        simulation = SimulationService(real_time=not args.headless, gui=not args.headless)
        simulation.start()
        store = StudioStore(args.database)
        app = StudioApplication(simulation, store, predictor)
        text = args.instruction or input("请输入指令：")
        result = app.instructions.parse({"instruction": text})
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if result["status"] != "ready":
            return 2
        if args.parse_only:
            return 0
        job = app.instructions.execute({"parse_id": result["parse_id"], "source": "language"})
        if result["action"] == "STOP":
            print(json.dumps(job, ensure_ascii=False))
            return 0
        while job["status"] in ("queued", "running"):
            time.sleep(0.1)
            job = simulation.get_job(job["id"])
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0 if job["status"] == "succeeded" else 2
    except (RequestError, ValueError, RuntimeError, OSError) as error:
        print(f"任务失败：{error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
    finally:
        if simulation is not None:
            simulation.close()
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
