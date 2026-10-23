"""启动 Panda 场景与关节演示。"""

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robot.demo import main


if __name__ == "__main__":
    main()
