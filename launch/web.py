"""一条命令启动网页工作台和真实 PyBullet 仿真。"""

from pathlib import Path
import importlib.util
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    if importlib.util.find_spec("torch") is None:
        interpreter = Path(__file__).resolve().parents[1] / ".venv_train/bin/python"
        if not interpreter.is_file() or Path(sys.executable).absolute() == interpreter.absolute():
            raise SystemExit("请使用安装了 PyTorch 的 Python 启动网页，例如 .venv_train/bin/python -B launch/web.py")
        os.execv(str(interpreter), [str(interpreter), "-B", str(Path(__file__).resolve()), *sys.argv[1:]])
    from backend.server import main
    raise SystemExit(main())
