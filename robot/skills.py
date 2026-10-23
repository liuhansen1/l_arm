"""组合任务使用的移动、地面放置和方块堆叠技能。"""

import math

import pybullet as p

from robot.grasp import GRASP_LINK


def grasp_position(controller):
    return list(p.getLinkState(controller.robot, GRASP_LINK, computeForwardKinematics=True)[4])


def move_safely(controller, position, held_body=None):
    if held_body is not None and not controller.fingers_touch(held_body):
        raise RuntimeError("移动前夹持物已滑落")
    start = grasp_position(controller)
    clearance = max(0.25, start[2], position[2])
    waypoints = ([start[0], start[1], clearance],
                 [position[0], position[1], clearance], list(position))
    for waypoint in waypoints:
        if math.dist(grasp_position(controller), waypoint) > 0.003:
            controller.move_to(waypoint)
        if held_body is not None and not controller.fingers_touch(held_body):
            raise RuntimeError("移动过程中夹持物滑落")
    return {"position": list(position), "end_effector_error_m": math.dist(grasp_position(controller), position)}


def placement_support(surface_position, poses, *, excluded=()):
    """使用不可变姿态检查保存点的支撑面，不移动或吸附用户坐标。"""
    support = 0
    if surface_position[2] > 0.003:
        matches = []
        for center, rotation, body in poses.values():
            if body in excluded:
                continue
            axis = max(range(3), key=lambda i: abs(rotation[2][i]))
            if abs(rotation[2][axis]) < math.cos(math.radians(5)):
                continue
            delta = [surface_position[i] - center[i] for i in range(3)]
            local = [sum(rotation[i][j] * delta[i] for i in range(3)) for j in range(3)]
            sign = 1 if rotation[2][axis] >= 0 else -1
            # 深度反投影及三位小数记录存在毫米误差，保留原坐标并给顶部 5 mm 容差。
            if (abs(sign * local[axis] - 0.025) <= 0.005
                    and all(abs(local[i]) <= 0.020 for i in range(3) if i != axis)):
                matches.append(body)
        if len(matches) != 1:
            raise ValueError("放置点没有唯一的水平支撑面，请重新点选方块顶部中央；侧面、边缘或已移走的方块不能支撑")
        support = matches[0]
    for center, rotation, body in poses.values():
        if body in excluded or body == support:
            continue
        half_height = 0.025 * sum(abs(rotation[2][i]) for i in range(3))
        if (center[2] + half_height > surface_position[2] + 0.003
                and center[2] - half_height < surface_position[2] + 0.050
                and math.dist(surface_position[:2], center[:2]) < 0.065):
            raise ValueError("放置位置被另一个方块占用")
    return support


def place_cube(controller, target, candidates, surface_position):
    if target is None or not controller.fingers_touch(target):
        raise RuntimeError("当前没有稳定夹持的物体")
    if len(surface_position) != 3 or not all(math.isfinite(v) for v in surface_position) or surface_position[2] < 0:
        raise RuntimeError("放置点必须是有效的支撑面坐标")
    candidates = list(candidates)

    def poses():
        result = {}
        for body in candidates:
            position, orientation = p.getBasePositionAndOrientation(body)
            matrix = p.getMatrixFromQuaternion(orientation)
            result[body] = (position, [matrix[i:i + 3] for i in (0, 3, 6)], body)
        return result

    def rests_on(body, lower):
        return any(contact[9] > 0.01 and contact[7][2] > 0.9
                   for contact in p.getContactPoints(bodyA=body, bodyB=lower))

    def grounded(body, visited=frozenset()):
        if body == 0:
            return True
        if body in visited:
            return False
        return any(rests_on(body, lower) and grounded(lower, visited | {body})
                   for lower in [0, *candidates] if lower not in (body, target))

    def stable(body):
        linear, angular = p.getBaseVelocity(body)
        return math.sqrt(sum(v * v for v in linear)) <= 0.05 and math.sqrt(sum(v * v for v in angular)) <= 0.5

    support = placement_support(surface_position, poses(), excluded=(target,))
    if not grounded(support) or (support != 0 and not stable(support)):
        raise RuntimeError("支撑方块尚未稳定落在地面或其他方块上，请重新选择放置点")
    x, y, surface_z = surface_position
    current = grasp_position(controller)
    held = p.getBasePositionAndOrientation(target)[0]
    # 使用当前持物与末端的偏移作坐标换算，不替换用户保存的目标位置。
    release_position = [x + current[0] - held[0], y + current[1] - held[1],
                        surface_z + 0.025 + current[2] - held[2]]
    above = [release_position[0], release_position[1], max(0.25, current[2], release_position[2] + 0.10)]
    move_safely(controller, above, target)
    controller.move_to(release_position)
    before = p.getBasePositionAndOrientation(target)[0]
    if not (0.015 <= before[2] - surface_z <= 0.040):
        raise RuntimeError("物体没有到达所选支撑面上方的放置高度")
    if placement_support(surface_position, poses(), excluded=(target,)) != support or not grounded(support):
        raise RuntimeError("下降过程中支撑发生变化，请重新采集放置点")
    controller.opening = 0.04
    controller.wait(1)
    controller.grasped_target = None
    controller.move_to(above)
    # 夹爪退出后继续验证，避免把短暂接触或夹爪托住误报为堆叠成功。
    for _ in range(240):
        controller.step()
        position = p.getBasePositionAndOrientation(target)[0]
        bottom = [position[0], position[1], position[2] - 0.025]
        placement_error = math.dist(bottom, surface_position)
        if (not rests_on(target, support) or not grounded(support) or not stable(target)
                or (support != 0 and not stable(support)) or placement_error > 0.035):
            raise RuntimeError("释放后物体没有稳定放在所选支撑面上")
        if support != 0 and placement_support(bottom, poses(), excluded=(target,)) != support:
            raise RuntimeError("物体滑出支撑方块，堆叠失败")
    return {"position": [round(v, 3) for v in position], "supported": True,
            "requested_surface_position": list(surface_position), "placement_error_m": placement_error,
            "support_type": "ground" if support == 0 else "cube", "support_body_id": support,
            "stable_seconds": 1}
