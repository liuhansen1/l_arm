"""数据库目标解析、真实模型调用与一次性执行凭据。模型代码均位于 llm/。"""

from collections import OrderedDict
import copy
import hashlib
import json
import re
import threading
import time
import uuid

from backend import RequestError
from backend.simulation import validate_placement_position, validate_position
from backend.store import name_key


# 仅用于定位动作词和计数；动作类别、分数仍由 Transformer 返回。
COMMAND_CUE = re.compile(
    r"(?:开始|启动)(?:执行|运行)|"
    r"别再执行了|别再动了|不要再动了|别继续动了|不要动|别动|"
    r"(?:停止|中断|中止|暂停|取消|结束)(?:正在执行的|当前|正在)?(?:任务|动作|执行|运行|运动)?|"
    r"停下来|停一下|停下|急停|"
    r"抓起来|抓取|抓起|抓住|夹起来|夹起|夹住|夹走|拿起来|拿起|"
    r"拾起来|拾起|捡起来|捡起|提起来|提起|"
    r"放下来|放下到|放置到|放置在|放到|放在|放下|放开|松开|释放掉|释放|"
    r"移动(?:机械臂|夹爪|末端)?(?:到|至)?|移到|移至|前往|到达|"
    r"执行|运行|启动|开始|调用|进行|完成|"
    r"(?:抓|拿|拾|夹|捡|提|到|去)(?=(?:一下|这个|那个)?<OBJ>)|"
    r"拧紧|焊接|涂漆|切开|切割|打孔|称量|清洗|喷漆|上色|翻转|旋转|删除|复制|修改|统计|拍照|照相")
SLOT = re.compile(r"<OBJ>|<TASK>")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def resolve_name(instruction, snapshot):
    text = name_key(instruction)
    matches = []
    for kind, records in snapshot.items():
        for record in records:
            for name in [record["name"], *record["aliases"]]:
                key = name_key(name)
                for hit in re.finditer(re.escape(key), text):
                    start, end = hit.span()
                    # A 不能匹配 AB 或 A2；中文名称按完整的连续字符串匹配。
                    if (key[-1:].isascii() and key[-1:].isalnum() and end < len(text)
                            and text[end].isascii() and text[end].isalnum()):
                        continue
                    if (key[:1].isascii() and key[:1].isalnum() and start > 0
                            and text[start - 1].isascii() and text[start - 1].isalnum()):
                        continue
                    matches.append({"start": start, "end": end, "kind": kind, "record": record})
    # 长名称完全覆盖的短别名不重复计算；相同长度的跨记录冲突仍保留。
    matches = [m for m in matches if not any(n["start"] <= m["start"] and m["end"] <= n["end"]
               and n["end"] - n["start"] > m["end"] - m["start"] for n in matches)]
    matches = list({(m["start"], m["end"], m["kind"], m["record"]["id"]): m for m in matches}.values())
    if not matches:
        return text, None, None
    match = matches[0]
    if len(matches) > 1:
        objects = [m for m in matches if m["kind"] == "objects"]
        points = [m for m in matches if m["kind"] == "placement_points"]
        if len(matches) != 2 or len(objects) != 1 or len(points) != 1:
            return text, None, "请一次指定一个物体；放置指令可另外指定一个放置点。"
        obj, point = objects[0], points[0]
        if obj["end"] > point["start"] or not re.fullmatch(r"放到|放在|放置到|放置在|放下到", text[obj["end"]:point["start"]]):
            return text, None, "请使用“把物体名称放到放置点名称”，或在任务编辑器选择放置点。"
        match = {**obj, "placement": point}
    normalized = text
    for item in sorted(matches, key=lambda m: m["start"], reverse=True):
        placeholder = "<TASK>" if item["kind"] == "tasks" else "<OBJ>"
        normalized = normalized[:item["start"]] + placeholder + normalized[item["end"]:]
    return normalized, match, None


def special_instruction_reason(text):
    """只检查未实现的指令结构，不限制普通用词或识别指定开场白。"""
    if re.search(r"如果|假如|假设|要是|只要|除非|等到|等[^,，。！？!?;；]*再", text):
        return "当前不支持条件指令，请明确是否现在执行。"
    if re.search(r"如何|怎么|怎样|只是问问|是否|要不要|还没决定|还在考虑|不知道|不确定|想知道|想了解|解释|介绍", text):
        return "指令包含询问或未确认的要求，请明确是否执行。"
    if re.search(r"明天|稍后|之后|以后|过一会|等会|待会|晚点", text):
        return "当前不支持预约执行，请在需要执行时下达指令。"
    if re.search(r"然后|接着|同时|并且|<(?:OBJ|TASK)>.*再(?=抓|夹|拿|拾|捡|提|放|移|执行|运行|启动|停止)", text):
        return "多个动作请保存为任务后执行，请一次指定一个动作或任务。"
    if re.search(r"左边|右边|前面|后面|上面|下面|不是|除了", text):
        return "当前不支持相对位置或排除条件，请使用已登记的目标或放置点。"
    for clause in re.split(r"[,，。！？!?;；:：]", text):
        if ("<OBJ>" in clause or "<TASK>" in clause) and re.search(
                r"[和与或]|以及|<(?:OBJ|TASK)>及|及<(?:OBJ|TASK)>", clause):
            return "请一次指定一个目标；放置指令可另外指定一个放置点。"
    return None


def extract_command(text):
    """按目标和动作词定位唯一片段，不枚举闲话、开场白或允许字符。"""
    candidates = []
    for clause in re.split(r"[,，。！？!?;；:：]", text):
        cues, slots = list(COMMAND_CUE.finditer(clause)), list(SLOT.finditer(clause))
        if not cues and not slots:
            continue
        # “开始抓取”是一条命令；相邻的调用动词不另算一次动作。
        wrappers = {"开始", "启动", "执行", "运行", "调用", "进行", "完成", "开始执行", "开始运行", "启动执行", "启动运行"}
        cues = [cue for index, cue in enumerate(cues) if not (
            cue[0] in wrappers and index + 1 < len(cues) and cue.end() == cues[index + 1].start())]
        if len(cues) > 1:
            return None, "检测到多个动作，请一次下达一个命令，或保存为组合任务。"
        model_input = clause
        if cues and slots:
            start = min(cues[0].start(), slots[0].start())
            end = max(cues[0].end(), slots[-1].end())
            # 保留“把<OBJ>拿起来”及“把<OBJ>放到<OBJ>”的语序。
            prefix = re.search(r"(?:把|将)(?:这个|那个)?$", clause[:start])
            if prefix:
                start = prefix.start()
            model_input = clause[start:end]
        elif slots and slots[0][0] == "<TASK>":
            model_input = "<TASK>"
        candidates.append(model_input)
    if len(candidates) > 1:
        return None, "检测到多个命令，请一次下达一个命令，或保存为组合任务。"
    if not candidates:
        return None, "没有找到目标名称或动作命令，请指定已登记目标或明确说停止。"
    return candidates[0], None


def prepare_steps(action, snapshot, *, object_id=None, task_id=None, position=None, placement_point_id=None):
    objects = {obj["id"]: obj for obj in snapshot["objects"]}
    if action == "RUN_TASK":
        tasks = {task["id"]: task for task in snapshot["tasks"]}
        if type(task_id) is not int or task_id not in tasks:
            raise RequestError("任务不存在。", 404)
        steps = copy.deepcopy(tasks[task_id]["steps"])
    else:
        steps = [{"action": action}]
        if object_id is not None:
            steps[0]["object_id"] = object_id
        if position is not None:
            steps[0]["position"] = position
        if placement_point_id is not None:
            steps[0]["placement_point_id"] = placement_point_id
    for step in steps:
        action = step.get("action")
        if action in ("GRASP", "PLACE"):
            identifier = step.get("object_id")
            if identifier is not None:
                if type(identifier) is not int or identifier not in objects:
                    raise RequestError("目标不存在或已被删除。", 404)
                obj = objects[identifier]
                required = "cube_grasp" if action == "GRASP" else "cube_place"
                if required not in obj["skill_ids"] and not (action == "PLACE" and "cube_grasp" in obj["skill_ids"]):
                    raise RequestError("目标没有关联此动作所需的技能。", 422)
                step["object"] = copy.deepcopy(obj)
            elif action == "GRASP":
                raise RequestError("抓取需要指定已登记目标。", 422)
            if action == "GRASP":
                step["position"] = validate_position(step["object"]["position"])
            else:
                point_id = step.get("placement_point_id")
                if type(point_id) is not int or point_id <= 0:
                    raise RequestError("放下步骤必须选择已登记的放置点，请编辑任务补齐。", 422)
                point = next((p for p in snapshot.get("placement_points", []) if p["id"] == point_id), None)
                if point is None:
                    raise RequestError("放置点不存在或已被删除。", 404)
                step["placement_point"] = copy.deepcopy(point)
                step["placement_position"] = validate_placement_position(point["position"])
        elif action == "MOVE":
            if step.get("coordinate_frame", "world") != "world":
                raise RequestError("移动只支持世界坐标。", 422)
            step["position"] = validate_position(step.get("position"))
        elif action == "WAIT":
            seconds = step.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 60:
                raise RequestError("等待时间必须在 0 至 60 秒之间。", 422)
        else:
            raise RequestError("任务包含未实现动作。", 422)
    return steps


class InstructionService:
    def __init__(self, store, simulation, parser=None, *, ttl=120):
        self.store, self.simulation = store, simulation
        self.parser = parser
        self.ttl = ttl
        self.lock = threading.RLock()
        self.prediction_lock = threading.Lock()
        self.parses = OrderedDict()
        self.memory_version = 0
        self.load_error = None

    def clear_memory(self):
        with self.lock, self.store.lock:
            self.simulation.control("reset")
            with self.simulation.lock:
                if self.simulation.active_job is not None or self.simulation.control_pending:
                    raise RequestError("重置后有新的任务或控制请求，请稍后重试清除。", 409)
                cleared = self.store.clear_memory()
                cleared.update(parses=len(self.parses), jobs=len(self.simulation.jobs))
                self.parses.clear()
                self.memory_version += 1
                self.simulation.jobs.clear()
        return {"status": "cleared", "cleared": cleared,
                "message": "所有物体、放置点、任务和运行记录已清除，场景已重置。"}

    def load(self):
        with self.prediction_lock:
            if self.parser is None:
                try:
                    from llm.model import IntentParser
                    import torch
                    torch.set_num_threads(4)
                    self.parser = IntentParser()
                    self.load_error = None
                except (ImportError, OSError, RuntimeError, ValueError) as error:
                    self.load_error = str(error)
                    raise RequestError("动作模型加载失败，请用 .venv_train 启动并检查 llm/checkpoints/intent_v2.pt。", 503) from error
        return self.parser

    def parse(self, body):
        if body.get("model", "transformer") != "transformer":
            raise RequestError("当前只支持自训 Transformer。", 422)
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 500:
            raise RequestError("请输入 1 至 500 字的指令。", 422)
        if "<" in instruction or ">" in instruction:
            raise RequestError("请输入实际目标名称，不要输入模型占位符。", 422)
        with self.lock, self.store.lock:
            snapshot = self.store.snapshot()
            scene = self.simulation.snapshot()
            memory_version = self.memory_version
        normalized, match, reason = resolve_name(instruction, snapshot)
        reason = reason or special_instruction_reason(normalized)
        base = {"instruction": instruction, "normalized_instruction": normalized,
                "source": "model", "model": "transformer"}
        if reason:
            return {**base, "status": "clarify", "message": reason}
        model_input, reason = extract_command(normalized)
        if reason:
            return {**base, "status": "clarify", "message": reason}
        base["model_input"] = model_input
        parser = self.load()
        started = time.perf_counter()
        with self.prediction_lock:
            try:
                prediction = parser.predict(model_input)
            except ValueError as error:
                raise RequestError(str(error), 422) from error
        base.update(action=prediction["action"], confidence=prediction["confidence"],
                    inference_ms=(time.perf_counter() - started) * 1000)
        # “别动”可以表示停止；只有模型确实预测 STOP 时才豁免该短语。
        negative_context = normalized
        if prediction["action"] == "STOP" and match is None:
            negative_context = re.sub(
                r"(?:不要动|别动|别再(?:执行|动)了|不要再动了|别继续动了)(?=$|[,，。！？!?;；:：])",
                "", negative_context)
        if re.search(r"不要|别|不必|不用|无需|不需要|不想|不准|禁止|暂不|先不", negative_context):
            return {**base, "status": "clarify", "message": "指令包含否定要求，本次不执行动作。"}
        if not match and prediction["action"] in ("GRASP", "RUN_TASK"):
            return {**base, "status": "unmatched", "message": "没有匹配到已登记目标或任务，请先添加名称或别名。"}
        if not prediction["accepted"]:
            return {**base, "status": "low_confidence", "message": prediction["reason"]}
        action = prediction["action"]
        if action in ("CLARIFY", "UNSUPPORTED"):
            return {**base, "status": action.lower(), "message": "请补充明确目标，或确认是否执行。" if action == "CLARIFY" else "暂不支持此指令，请使用单个动作或已保存任务。"}
        if match and match.get("placement") and action != "PLACE":
            return {**base, "status": "clarify", "message": "物体与放置点只用于放置动作，请明确说“把物体放到放置点”。"}
        parameters = {}
        if action == "RUN_TASK":
            if not match or match["kind"] != "tasks":
                return {**base, "status": "unmatched", "message": "没有匹配到已保存任务，请先创建任务及其名称。"}
            parameters = {"task_id": match["record"]["id"], "task_name": match["record"]["name"]}
        elif action == "PLACE":
            point_match = match if match and match["kind"] == "placement_points" else (match or {}).get("placement")
            if point_match is None:
                return {**base, "status": "clarify", "message": "请指定放置点，例如“放到放置区A”或“把零件A放到放置区A”。"}
            if match["kind"] == "placement_points":
                explicit_object = re.search(
                    r"(?:把|将)([^,，。！？!?;；:：<>]+?)(?:放到|放在|放置到|放置在|放下到)<OBJ>", normalized)
                # 省略物体或明确指代当前持物时可沿用持物；显式的新名称必须登记。
                if explicit_object and explicit_object[1] not in {
                        "手里的东西", "手中的物体", "当前物体", "当前夹持物", "当前持物"}:
                    return {**base, "status": "unmatched", "message": "没有匹配到放置指令中的物体，请先登记其名称或别名。"}
            point = point_match["record"]
            parameters = {"placement_point_id": point["id"], "placement_point_name": point["name"],
                          "placement_position": point["position"]}
            obj = match["record"] if match["kind"] == "objects" else next(
                (o for o in snapshot["objects"] if o["id"] == scene["held_object_id"]), None)
            if obj:
                parameters.update(object_id=obj["id"], object_name=obj["name"])
        elif action in ("GRASP", "MOVE"):
            if not match or match["kind"] != "objects":
                return {**base, "status": "unmatched", "message": "没有匹配到已登记目标，请先添加物体或点位名称。"}
            obj = match["record"]
            parameters = {"object_id": obj["id"], "object_name": obj["name"]}
            if action == "MOVE":
                if obj["color"] != "point":
                    return {**base, "status": "clarify", "message": "移动需要已登记的目标点位，请指定点位名称或在任务中填写绝对坐标。"}
                parameters["position"] = obj["position"]
        elif action == "STOP" and match:
            return {**base, "status": "clarify", "message": "停止指令请直接说“停止当前任务”。"}
        if action != "STOP":
            if scene["is_running"] or scene["requires_reset"]:
                return {**base, "status": "clarify", "message": "请等待当前任务完成；若场景要求重置，请先重置再解析。"}
            try:
                steps = prepare_steps(action, snapshot, **{k: v for k, v in parameters.items() if k in ("object_id", "task_id", "position", "placement_point_id")})
                self.simulation.validate_program(steps)
            except RequestError as error:
                return {**base, **parameters, "status": "clarify", "message": str(error)}
        result = {**base, **parameters, "status": "ready", "parse_id": uuid.uuid4().hex}
        with self.lock:
            if memory_version != self.memory_version:
                return {**base, "status": "clarify", "message": "记忆已清除，请重新登记目标并解析指令。"}
            self.parses[result["parse_id"]] = {"result": result, "database": fingerprint(snapshot),
                                               "scene_id": scene["scene_id"], "state_version": scene["state_version"],
                                               "created": time.monotonic(), "consumed": False}
            while len(self.parses) > 200:
                self.parses.popitem(last=False)
        return result

    def execute(self, body):
        with self.lock, self.store.lock:
            if not isinstance(body.get("parse_id"), str):
                raise RequestError("必须提供有效的 parse_id。", 409)
            record = self.parses.get(body.get("parse_id"))
            if record is None or record["consumed"] or time.monotonic() - record["created"] > self.ttl:
                raise RequestError("解析结果不存在、已执行或已过期，请重新解析。", 409)
            result = record["result"]
            for key in ("action", "object_id", "task_id", "position", "instruction", "model", "placement_point_id", "placement_position"):
                if key in body and body[key] != result.get(key):
                    raise RequestError("执行参数与已确认解析不一致，请重新解析。", 409)
            scene = self.simulation.snapshot()
            snapshot = self.store.snapshot()
            if (fingerprint(snapshot) != record["database"] or scene["scene_id"] != record["scene_id"]
                    or scene["state_version"] != record["state_version"]):
                raise RequestError("目标、任务或场景状态已改变，请重新解析。", 409)
            if result["action"] == "STOP":
                response = self.simulation.control("stop")
            else:
                steps = prepare_steps(result["action"], snapshot, **{k: result[k] for k in ("object_id", "task_id", "position", "placement_point_id") if k in result})
                response = self.simulation.submit_program(steps, action=result["action"], expected_state=record["state_version"])
            record["consumed"] = True
            return response
