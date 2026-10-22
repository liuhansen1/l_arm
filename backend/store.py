"""工作台登记信息的 SQLite 存储；仿真状态不写入数据库。"""

import json
import copy
import math
from pathlib import Path
import sqlite3
import threading
import unicodedata
import uuid

from backend import RequestError
from backend.simulation import validate_position
from llm.schema import normalize_text


def name_key(value):
    return normalize_text(value).casefold()


def label(value):
    if not isinstance(value, str):
        raise RequestError("名称和别名必须是文本。", 422)
    value = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(value) <= 60:
        raise RequestError("名称和别名长度必须为 1 至 60 个字符。", 422)
    if any(token in value for token in ("<", ">")) or name_key(value) in {"停止", "停下", "stop", "急停"}:
        raise RequestError("名称不能包含占位符或与停止指令重名。", 422)
    return value


def alias_list(values):
    if not isinstance(values, list) or len(values) > 20:
        raise RequestError("别名必须是最多包含 20 个名称的数组。", 422)
    return list(dict.fromkeys(label(v) for v in values))


class StudioStore:
    """只保存用户明确登记的信息，不从仿真场景自动导入目标。"""

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_object_id TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL,
                name TEXT NOT NULL,
                aliases TEXT NOT NULL,
                position TEXT NOT NULL,
                skill_ids TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                aliases TEXT NOT NULL,
                steps TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS placement_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                aliases TEXT NOT NULL,
                position TEXT NOT NULL
            );
        """)

    def close(self):
        with self.lock:
            self.connection.close()

    @staticmethod
    def decode(row, fields):
        value = dict(row)
        for field in fields:
            value[field] = json.loads(value[field])
        return value

    def objects(self):
        with self.lock:
            values = [self.decode(row, ("aliases", "position", "skill_ids"))
                      for row in self.connection.execute("SELECT * FROM objects ORDER BY id")]
        for value in values:
            value["coordinate_frame"] = "world"
        return values

    def object(self, object_id):
        if type(object_id) is not int or object_id <= 0:
            raise RequestError("物体 ID 必须为正整数。", 422)
        value = next((obj for obj in self.objects() if obj["id"] == object_id), None)
        if value is None:
            raise RequestError("登记物体不存在。", 404)
        return value

    def task(self, task_id):
        if type(task_id) is not int or task_id <= 0:
            raise RequestError("任务 ID 必须为正整数。", 422)
        value = next((task for task in self.tasks() if task["id"] == task_id), None)
        if value is None:
            raise RequestError("任务不存在。", 404)
        return value

    def snapshot(self):
        with self.lock:
            return {"objects": self.objects(), "tasks": self.tasks(),
                    "placement_points": self.placement_points()}

    def clear_memory(self):
        with self.lock, self.connection:
            cleared = {}
            for table in ("tasks", "placement_points", "objects"):
                cleared[table] = self.connection.execute(f"DELETE FROM {table}").rowcount
            # 保留自增序列，避免旧页面把新登记误认为已删除的目标。
            return cleared

    def check_labels(self, name, aliases, collection, identifier=None):
        keys = {name_key(value) for value in [name, *aliases]}
        for kind, records in self.snapshot().items():
            for record in records:
                if kind == collection and record["id"] == identifier:
                    continue
                if keys.intersection(name_key(v) for v in [record["name"], *record["aliases"]]):
                    raise RequestError("名称或别名与已有物体、放置点或任务冲突。", 409)

    def placement_points(self):
        with self.lock:
            values = [self.decode(row, ("aliases", "position")) for row in self.connection.execute(
                "SELECT * FROM placement_points ORDER BY id")]
        for value in values:
            value["coordinate_frame"] = "world"
        return values

    def placement_point(self, point_id):
        if type(point_id) is not int or point_id <= 0:
            raise RequestError("放置点 ID 必须为正整数。", 422)
        value = next((point for point in self.placement_points() if point["id"] == point_id), None)
        if value is None:
            raise RequestError("放置点不存在。", 404)
        return value

    def save_placement_point(self, value, point_id=None):
        name = label(value.get("name"))
        aliases = [v for v in alias_list(value.get("aliases", [])) if v != name]
        position = validate_position(value.get("position"), workspace=False)
        if value.get("coordinate_frame", "world") != "world":
            raise RequestError("放置点只支持世界坐标。", 422)
        with self.lock:
            if point_id is not None:
                self.placement_point(point_id)
            self.check_labels(name, aliases, "placement_points", point_id)
            args = (name, json.dumps(aliases, ensure_ascii=False), json.dumps(position))
            with self.connection:
                if point_id is None:
                    cursor = self.connection.execute(
                        "INSERT INTO placement_points (name,aliases,position) VALUES (?,?,?)", args)
                    point_id = cursor.lastrowid
                else:
                    self.connection.execute(
                        "UPDATE placement_points SET name=?,aliases=?,position=? WHERE id=?", (*args, point_id))
            return self.placement_point(point_id)

    def delete_placement_point(self, point_id):
        with self.lock:
            self.placement_point(point_id)
            if any(step.get("placement_point_id") == point_id for task in self.tasks() for step in task["steps"]):
                raise RequestError("有任务引用此放置点，请先修改相应任务。", 409)
            with self.connection:
                self.connection.execute("DELETE FROM placement_points WHERE id=?", (point_id,))

    def save_object(self, value, object_id=None):
        name = label(value.get("name"))
        aliases = [v for v in alias_list(value.get("aliases", [])) if v != name]
        position = validate_position(value.get("position"), workspace=False)
        color = value.get("color")
        scene_id = value.get("scene_object_id")
        if color == "point" or scene_id is None:
            color = "point"
            scene_id = scene_id or f"point-{uuid.uuid4().hex}"
            if not isinstance(scene_id, str) or not scene_id.startswith("point-"):
                raise RequestError("采集点标识不合法。", 422)
        elif color not in ("red", "green", "blue") or scene_id != f"cube-{color}":
            raise RequestError("物体绑定标识不合法。", 422)
        if value.get("coordinate_frame", "world") != "world":
            raise RequestError("当前仅支持世界坐标。", 422)
        skills = value.get("skill_ids", ["cube_grasp"])
        if (not isinstance(skills, list) or not skills or len(skills) > 8
                or any(v not in ("cube_grasp", "cube_place") for v in skills)):
            raise RequestError("当前目标可关联技能为 cube_grasp、cube_place。", 422)
        with self.lock:
            if object_id is not None:
                self.object(object_id)
            self.check_labels(name, aliases, "objects", object_id)
            labels = {name, *aliases}
            for obj in self.objects():
                if obj["id"] == object_id:
                    continue
                if obj["scene_object_id"] == scene_id:
                    raise RequestError("方块已登记，请编辑已有物体。", 409)
                if labels.intersection([obj["name"], *obj["aliases"]]):
                    raise RequestError("物体名称或别名与已有登记冲突。", 409)
            args = (scene_id, color, name, json.dumps(aliases, ensure_ascii=False),
                    json.dumps(position), json.dumps(list(dict.fromkeys(skills))))
            with self.connection:
                if object_id is None:
                    cursor = self.connection.execute(
                        "INSERT INTO objects (scene_object_id,color,name,aliases,position,skill_ids) VALUES (?,?,?,?,?,?)", args)
                    object_id = cursor.lastrowid
                else:
                    self.connection.execute(
                        "UPDATE objects SET scene_object_id=?,color=?,name=?,aliases=?,position=?,skill_ids=? WHERE id=?",
                        (*args, object_id))
            return self.object(object_id)

    def delete_object(self, object_id):
        with self.lock:
            self.object(object_id)
            if any(step.get("object_id") == object_id for task in self.tasks() for step in task["steps"]):
                raise RequestError("有任务引用此物体，请先修改相应任务。", 409)
            with self.connection:
                self.connection.execute("DELETE FROM objects WHERE id=?", (object_id,))

    def tasks(self):
        with self.lock:
            return [self.decode(row, ("aliases", "steps"))
                    for row in self.connection.execute("SELECT * FROM tasks ORDER BY id")]

    def save_task(self, value, task_id=None):
        name = label(value.get("name"))
        aliases = alias_list(value.get("aliases", []))
        steps = copy.deepcopy(value.get("steps"))
        if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
            raise RequestError("任务需要 1 至 50 个步骤。", 422)
        with self.lock:
            self.check_labels(name, aliases, "tasks", task_id)
            for step in steps:
                if not isinstance(step, dict):
                    raise RequestError("任务步骤必须是对象。", 422)
                action = step.get("action")
                if action in ("GRASP", "PLACE"):
                    self.object(step.get("object_id"))
                    if action == "PLACE":
                        self.placement_point(step.get("placement_point_id"))
                elif action == "MOVE":
                    if step.get("coordinate_frame", "world") != "world":
                        raise RequestError("移动步骤只支持世界坐标。", 422)
                    step["position"] = validate_position(step.get("position"))
                elif action == "WAIT":
                    seconds = step.get("seconds")
                    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 60:
                        raise RequestError("等待时间必须在 0 至 60 秒之间。", 422)
                else:
                    raise RequestError("任务中包含不支持的步骤。", 422)
            all_tasks = self.tasks()
            if task_id is not None and not any(t["id"] == task_id for t in all_tasks):
                raise RequestError("任务不存在。", 404)
            names = {name, *aliases}
            if any(names.intersection([task["name"], *task["aliases"]]) for task in all_tasks if task["id"] != task_id):
                raise RequestError("任务名称或别名重复。", 409)
            args = (name, json.dumps(aliases, ensure_ascii=False), json.dumps(steps, allow_nan=False))
            with self.connection:
                if task_id is None:
                    cursor = self.connection.execute("INSERT INTO tasks (name,aliases,steps) VALUES (?,?,?)", args)
                    task_id = cursor.lastrowid
                else:
                    self.connection.execute("UPDATE tasks SET name=?,aliases=?,steps=? WHERE id=?", (*args, task_id))
            return next(task for task in self.tasks() if task["id"] == task_id)

    def delete_task(self, task_id):
        with self.lock, self.connection:
            cursor = self.connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            if not cursor.rowcount:
                raise RequestError("任务不存在。", 404)
