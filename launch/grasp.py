"""启动指定颜色方块的机械臂抓取。"""

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robot.grasp import main


if __name__ == "__main__":
    raise SystemExit(main())
