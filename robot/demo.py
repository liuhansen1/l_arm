"""Panda 场景与关节运动演示，用于检查 PyBullet 安装。"""

import argparse
import math
from pathlib import Path
import time

import pybullet as p
import pybullet_data

PANDA_URDF = Path(__file__).resolve().parent / "models/franka_panda/panda.urdf"


def create_scene():
    """返回机械臂、初始关节角度和颜色到方块 ID 的映射。"""
    p.setAdditionalSearchPath(pybullet_data.getDataPath())
    p.setGravity(0, 0, -9.81)
    p.setTimeStep(1 / 240)
    p.loadURDF("plane.urdf")
    robot = p.loadURDF(str(PANDA_URDF), useFixedBase=True)

    home = [0, -0.5, 0, -2.3, 0, 1.8, math.pi / 4]
    for joint, angle in enumerate(home):
        p.resetJointState(robot, joint, angle)
    for joint in (9, 10):
        p.resetJointState(robot, joint, 0.04)

    half_extents = [0.025, 0.025, 0.025]
    collision = p.createCollisionShape(p.GEOM_BOX, halfExtents=half_extents)
    colors = [(1, 0.1, 0.1, 1), (0.1, 0.8, 0.1, 1), (0.1, 0.3, 1, 1)]
    objects = {}
    for name, y, color in zip(("red", "green", "blue"), (-0.18, 0, 0.18), colors):
        visual = p.createVisualShape(
            p.GEOM_BOX, halfExtents=half_extents, rgbaColor=color
        )
        objects[name] = p.createMultiBody(
            baseMass=0.05,
            baseCollisionShapeIndex=collision,
            baseVisualShapeIndex=visual,
            basePosition=[0.55, y, 0.25],
        )
    return robot, home, objects


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true", help="无窗口运行")
    parser.add_argument(
        "--seconds", type=float, default=0, help="运行的仿真秒数；默认窗口持续运行"
    )
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("--seconds 必须是大于等于 0 的有限数字")
    duration = args.seconds or (2 if args.headless else math.inf)

    client = p.connect(p.DIRECT if args.headless else p.GUI)
    if client < 0:
        raise RuntimeError("无法连接 PyBullet")

    try:
        robot, home, _ = create_scene()
        if not args.headless:
            p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0)
            p.resetDebugVisualizerCamera(
                cameraDistance=1.5,
                cameraYaw=45,
                cameraPitch=-30,
                cameraTargetPosition=[0.3, 0, 0.3],
            )
        print("Panda 与三色方块已加载。此演示检查关节运动，尚未实现抓取或识别。")
        print("关闭窗口或按 Ctrl+C 退出。", flush=True)
        step = 0
        while p.isConnected(client) and step / 240 < duration:
            simulation_time = step / 240
            targets = home.copy()
            targets[0] = 0.25 * math.sin(simulation_time)
            p.setJointMotorControlArray(
                robot,
                list(range(7)),
                p.POSITION_CONTROL,
                targetPositions=targets,
                forces=[87, 87, 87, 87, 12, 12, 12],
            )
            opening = 0.025 + 0.015 * math.sin(simulation_time)
            p.setJointMotorControlArray(
                robot,
                [9, 10],
                p.POSITION_CONTROL,
                targetPositions=[opening, opening],
                forces=[20, 20],
            )
            p.stepSimulation()
            if not args.headless:
                time.sleep(1 / 240)
            step += 1
        print(f"仿真结束，已执行 {step} 步。")
    except KeyboardInterrupt:
        print("\n仿真已停止。")
    finally:
        if p.isConnected(client):
            p.disconnect(client)


if __name__ == "__main__":
    main()
