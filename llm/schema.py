"""自训模型、Qwen 和机械臂共用的任务协议，不依赖 PyTorch。"""

import unicodedata

SCHEMA_VERSION = 1
LABEL_VALUES = {
    "intent": ["grasp", "clarify", "unsupported"],
    "target_color": [None, "red", "green", "blue"],
    "reference_color": [None, "red", "green", "blue"],
    "relation": ["none", "left_of", "right_of", "leftmost", "rightmost"],
    "exclude_color": [None, "red", "green", "blue"],
}
COLOR_NAMES = {"red": "红色", "green": "绿色", "blue": "蓝色"}

# 旧协议保留给已有命令行入口；网页使用独立的固定意图协议。
INTENT_SCHEMA_VERSION = 2
ACTIONS = ["GRASP", "PLACE", "MOVE", "STOP", "RUN_TASK", "CLARIFY", "UNSUPPORTED"]
SPECIAL_TOKENS = ["<PAD>", "<UNK>", "<CLS>", "<SEP>", "<OBJ>", "<TASK>"]


def validate_intent(value):
    if not isinstance(value, dict) or set(value) != {"action"} or value["action"] not in ACTIONS:
        raise ValueError("动作必须是固定意图协议中的一个类别")
    return value


def normalize_text(text):
    if not isinstance(text, str):
        raise ValueError("指令必须是字符串")
    return "".join(unicodedata.normalize("NFKC", text).split())


def task(intent="grasp", target_color=None, reference_color=None,
         relation="none", exclude_color=None):
    return validate_task(dict(intent=intent, target_color=target_color,
                              reference_color=reference_color, relation=relation,
                              exclude_color=exclude_color))


def validate_task(value):
    if not isinstance(value, dict) or set(value) != set(LABEL_VALUES):
        raise ValueError("任务必须包含协议规定的五个字段")
    for name, choices in LABEL_VALUES.items():
        if value[name] not in choices:
            raise ValueError(f"字段 {name} 的值不合法")
    if value["intent"] != "grasp":
        if any(value[k] is not None for k in ("target_color", "reference_color", "exclude_color")) or value["relation"] != "none":
            raise ValueError("不执行的意图不能携带目标槽位")
    elif (value["relation"] in ("left_of", "right_of")) != (value["reference_color"] is not None):
        raise ValueError("左右关系和参照物必须同时出现")
    return value
