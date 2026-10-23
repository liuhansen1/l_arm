"""自训模型的固定动作意图协议与输入标准化，不依赖 PyTorch。"""

import unicodedata

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
