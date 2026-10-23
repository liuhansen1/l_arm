"""使用 Panda 双指夹爪抓起指定颜色的方块。"""

import argparse
import math
import sys
import time

import pybullet as p

from robot.demo import create_scene


COLOR_NAMES = {"red": "红色", "green": "绿色", "blue": "蓝色"}
TIME_STEP = 1 / 240
ARM_JOINTS = list(range(7))
FINGER_JOINTS = [9, 10]
GRASP_LINK = 11
ARM_FORCES = [87, 87, 87, 87, 12, 12, 12]


class PandaController:
    def __init__(self, client, robot, home, real_time):
        self.client = client
        self.robot = robot
        self.arm_target = home.copy()
        self.opening = 0.04
        self.real_time = real_time
        self.grasped_target = None
        self.orientation = p.getQuaternionFromEuler([math.pi, 0, 0])

    def step(self, count=1):
        for _ in range(count):
            if not p.isConnected(self.client):
                raise ConnectionError("仿真窗口已关闭")
            p.setJointMotorControlArray(
                self.robot,
                ARM_JOINTS,
                p.POSITION_CONTROL,
                targetPositions=self.arm_target,
                forces=ARM_FORCES,
            )
            p.setJointMotorControlArray(
                self.robot,
                FINGER_JOINTS,
                p.POSITION_CONTROL,
                targetPositions=[self.opening, self.opening],
                forces=[20, 20],
                positionGains=[0.05, 0.05],
            )
            p.stepSimulation()
            if self.real_time:
                time.sleep(TIME_STEP)

    def wait(self, seconds):
        self.step(math.ceil(seconds / TIME_STEP))

    def move_to(self, position, seconds=2):
        start = p.getLinkState(
            self.robot, GRASP_LINK, computeForwardKinematics=True
        )[4]
        steps = math.ceil(seconds / TIME_STEP)
        for step in range(steps):
            t = (step + 1) / steps
            # 平滑改变末端目标位置，减小起停时对方块的冲击。
            fraction = t * t * (3 - 2 * t)
            waypoint = [a + fraction * (b - a) for a, b in zip(start, position)]
            joints = p.calculateInverseKinematics(
                self.robot,
                GRASP_LINK,
                waypoint,
                self.orientation,
                maxNumIterations=100,
                residualThreshold=1e-5,
            )
            self.arm_target = list(joints[:7])
            self.step()
        self.wait(0.5)
        actual = p.getLinkState(
            self.robot, GRASP_LINK, computeForwardKinematics=True
        )[4]
        if math.dist(actual, position) > 0.01:
            raise RuntimeError("夹爪未能到达目标位置，请检查可达性和碰撞")

    def fingers_touch(self, target):
        return all(
            any(
                contact[9] > 0.1
                for contact in p.getContactPoints(
                    bodyA=self.robot, bodyB=target, linkIndexA=finger
                )
            )
            for finger in FINGER_JOINTS
        )


def grasp_cube(controller, target, *, position=None, progress=None, candidates=None):
    """position 是用户指定的方块中心抓取坐标；真值仅用于结果验证。"""
    if position is not None:
        if len(position) != 3 or not all(math.isfinite(v) for v in position):
            raise ValueError("抓取坐标必须包含三个有限数值")
        position = tuple(position)
    targets = [target] if target is not None else list(candidates or [])
    if not targets or (target is None and position is None):
        raise ValueError("任意坐标试抓需要指定坐标和可验证的候选物体")
    controller.grasped_target = None

    def report(step, message):
        print(f"{step}/5 {message}", flush=True)
        if progress is not None:
            progress(step, message)

    report(1, "等待方块落稳，并张开夹爪。")
    controller.opening = 0.04
    controller.wait(1)
    initial_heights = {body: p.getBasePositionAndOrientation(body)[0][2] for body in targets}
    x, y, grasp_height = p.getBasePositionAndOrientation(target)[0] if position is None else position

    report(2, "移动到目标方块上方。")
    controller.move_to([x, y, grasp_height + 0.225])

    report(3, "缓慢下降，使方块位于两指之间。")
    # 本场景为 5 cm 方块；抓取参考点略高于方块中心，避免指尖碰地。
    controller.move_to([x, y, grasp_height + 0.005])

    report(4, "闭合夹爪，并检查双指接触。")
    controller.opening = 0
    controller.wait(1)
    touching = [body for body in targets if controller.fingers_touch(body)]
    if len(touching) != 1:
        raise RuntimeError("夹爪没有同时夹住目标方块的两侧")
    target = touching[0]
    controller.grasped_target = target
    initial_height = initial_heights[target]

    report(5, "抬起方块，并检查是否稳定保持 3 秒。")
    controller.move_to([x, y, grasp_height + 0.225])
    minimum_lift = math.inf
    for _ in range(3 * 240):
        controller.step()
        height = p.getBasePositionAndOrientation(target)[0][2]
        minimum_lift = min(minimum_lift, height - initial_height)
        if minimum_lift < 0.15 or not controller.fingers_touch(target):
            raise RuntimeError("方块未被稳定抬起，或已从夹爪中滑落")
    return minimum_lift


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--color", choices=COLOR_NAMES, default="red", help="目标颜色")
    parser.add_argument("--headless", action="store_true", help="无窗口验证抓取")
    parser.add_argument(
        "--exit-after-grasp", action="store_true", help="完成抓取验证后自动退出"
    )
    args = parser.parse_args(argv)

    client = p.connect(p.DIRECT if args.headless else p.GUI)
    if client < 0:
        print("无法连接 PyBullet。", file=sys.stderr)
        return 1

    try:
        robot, home, objects = create_scene()
        p.setPhysicsEngineParameter(numSolverIterations=150)
        if not args.headless:
            p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0)
            p.resetDebugVisualizerCamera(
                cameraDistance=1.25,
                cameraYaw=45,
                cameraPitch=-30,
                cameraTargetPosition=[0.4, 0, 0.2],
            )
        print(f"目标：{COLOR_NAMES[args.color]}方块。关闭窗口或按 Ctrl+C 退出。", flush=True)
        controller = PandaController(client, robot, home, real_time=not args.headless)
        lift = grasp_cube(controller, objects[args.color])
        print(
            f"抓取成功：{COLOR_NAMES[args.color]}方块稳定保持 3 秒，"
            f"最低抬升 {lift * 100:.1f} cm。",
            flush=True,
        )
        if not args.headless and not args.exit_after_grasp:
            print("正在保持夹持。关闭窗口或按 Ctrl+C 退出。", flush=True)
            while p.isConnected(client):
                controller.step()
        return 0
    except (KeyboardInterrupt, ConnectionError):
        print("\n仿真已停止。")
        return 130
    except RuntimeError as error:
        print(f"抓取失败：{error}", file=sys.stderr)
        return 1
    except p.error:
        if p.isConnected(client):
            raise
        print("仿真窗口已关闭。")
        return 130
    finally:
        if p.isConnected(client):
            p.disconnect(client)


if __name__ == "__main__":
    raise SystemExit(main())
