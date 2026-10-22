"""按句式族固定划分数据，名称只以占位符出现。"""

import json
from pathlib import Path

from llm.schema import ACTIONS, normalize_text

DATA_DIRECTORY = Path(__file__).resolve().parent / "data"

# 每行依次为训练、验证、测试句式；相同句式的礼貌词变体不跨集合。
TEMPLATES = {
    "GRASP": (
        "抓取<OBJ>|抓起<OBJ>|抓住<OBJ>|拿起<OBJ>|夹起<OBJ>|拾起<OBJ>|把<OBJ>拿起来|把<OBJ>抓起来|把<OBJ>夹起来|把<OBJ>拾起来|将<OBJ>抓起|将<OBJ>拿起|用夹爪夹住<OBJ>|抓一下<OBJ>|拿一下<OBJ>|夹住<OBJ>|我要抓取<OBJ>|我想拿起<OBJ>|用机械臂抓取<OBJ>|抓起这个<OBJ>",
        "把<OBJ>拿一下|把<OBJ>抓一下|替我夹起<OBJ>|将这个<OBJ>拿起来|抓取一下<OBJ>|拿起这个<OBJ>",
        "把这个<OBJ>抓起来|将<OBJ>夹起|用夹爪抓起<OBJ>|替我拿起<OBJ>|抓住这个<OBJ>|机械臂抓取<OBJ>",
    ),
    "PLACE": (
        "放下<OBJ>|松开<OBJ>|释放<OBJ>|把<OBJ>放下|把<OBJ>放下来|将<OBJ>放下|放下手里的东西|放下当前物体|松开手里的物体|释放当前夹持物|把手里的东西放下|把夹住的物体放下|将夹持的物体释放|把拿着的东西放下来|放开<OBJ>|把<OBJ>松开|在这里放下<OBJ>|将<OBJ>放在当前位置|放下夹爪中的物体|把手中物体放下",
        "将<OBJ>松开|把这个<OBJ>放下来|放下这个<OBJ>|把当前物体放下|放下拿着的东西|把<OBJ>放在这里",
        "松开夹住的物体|将这个<OBJ>放下|把当前夹持物放下来|释放这个<OBJ>|放下手中的物体|把<OBJ>释放掉",
    ),
    "MOVE": (
        "移动到<OBJ>|移到<OBJ>|前往<OBJ>|到达<OBJ>|去<OBJ>|移动至<OBJ>|让机械臂移动到<OBJ>|把夹爪移到<OBJ>|将夹爪移动至<OBJ>|机械臂前往<OBJ>|到<OBJ>去|让夹爪到达<OBJ>|移动到指定位置|移动机械臂|移动到目标坐标|让机械臂换个位置|将机械臂移动到<OBJ>|把末端移动到<OBJ>|让末端到达<OBJ>|移至<OBJ>",
        "夹爪前往<OBJ>|机械臂移动至<OBJ>|把机械臂移到<OBJ>|移动夹爪到<OBJ>|让末端前往<OBJ>|把夹爪移动到指定位置",
        "将机械臂移至<OBJ>|末端移动到<OBJ>|让夹爪前往<OBJ>|移动到这个<OBJ>|夹爪到达<OBJ>|把机械臂移动至<OBJ>",
    ),
    "STOP": (
        "停止|停下|停下来|立即停止|马上停止|停止当前任务|停止任务|中断任务|取消当前任务|暂停执行|停止机械臂|让机械臂停下|别动|不要动|别再执行了|停止当前动作|急停|结束当前执行|停止运行|取消执行",
        "把当前任务停下来|让当前动作停下|马上停下来|中断当前执行|不要再动了|请立即停下",
        "机械臂停下来|把任务停止|取消正在执行的任务|立刻停止当前动作|结束当前任务|让机械臂停止运动",
    ),
    "RUN_TASK": (
        "执行<TASK>|运行<TASK>|开始<TASK>|启动<TASK>|执行一下<TASK>|运行一下<TASK>|帮忙执行<TASK>|我要执行<TASK>|我想运行<TASK>|按配置执行<TASK>|开始执行<TASK>|开始运行<TASK>|调用<TASK>|进行<TASK>|完成<TASK>|<TASK>|帮我<TASK>|执行任务<TASK>|运行任务<TASK>|按步骤完成<TASK>",
        "调用一下<TASK>|启动任务<TASK>|替我执行<TASK>|帮忙运行<TASK>|将<TASK>执行一遍|按照配置运行<TASK>",
        "执行一遍<TASK>|把<TASK>运行一下|开始任务<TASK>|替我完成<TASK>|按照步骤执行<TASK>|让机械臂执行<TASK>",
    ),
    "CLARIFY": (
        "抓一个|拿一个|抓那个|帮我抓东西|执行一个任务|运行那个任务|我想让你做点事|不知道抓哪个|不要抓取<OBJ>|别抓<OBJ>|不要拿起<OBJ>|不用抓<OBJ>|不必拿<OBJ>|不要放下<OBJ>|别松开<OBJ>|不要执行<TASK>|别运行<TASK>|取消执行<TASK>的想法|不要停止|别停|继续保持不要停|不需要移动|不要移动到<OBJ>|不必启动<TASK>|先不要动<OBJ>|先别执行<TASK>|不要抓|别放下|什么也不要做|等等先别抓",
        "不想抓<OBJ>|暂时不要执行<TASK>|先不要拿起<OBJ>|不用放下<OBJ>|不知道执行什么|不要停下来",
        "暂时别抓<OBJ>|先别运行<TASK>|不需要抓取<OBJ>|不要把<OBJ>拿起来|先不执行<TASK>|不要把<OBJ>放下",
    ),
    "UNSUPPORTED": (
        "拧紧螺丝|焊接零件|给物体涂漆|识别所有图片|唱一首歌|打开浏览器|把<OBJ>拧紧|切开<OBJ>|给<OBJ>打孔|称量<OBJ>|清洗<OBJ>|给我倒一杯水|连续抓取<OBJ>和<OBJ>|先抓<OBJ>再抓<OBJ>|把<OBJ>移到左边|向右移动|向前移动十厘米|让机械臂向上移动|解释天气|写一篇作文",
        "把<OBJ>切开|给<OBJ>喷漆|将<OBJ>焊接起来|先拿<OBJ>然后拿<OBJ>|告诉我今天的天气|把<OBJ>向右挪动",
        "给<OBJ>上色|将<OBJ>切成两半|把<OBJ>清洗干净|拿起<OBJ>和<OBJ>|机械臂往后移动|讲一个故事",
    ),
}

# 同一基础句式的全部开场变体留在同一集合；测试集含用户原句的占位符形式。
CONVERSATIONAL_TEMPLATES = {
    "GRASP": (
        "我想把<OBJ>拿起来|我需要抓取<OBJ>|能不能帮我拿起<OBJ>|可不可以抓起<OBJ>|抓起<OBJ>吧|把<OBJ>捡起来|提起<OBJ>|帮我夹走<OBJ>|你帮我抓取<OBJ>好吗|我希望你拿起<OBJ>",
        "我想要抓起<OBJ>|能帮我把<OBJ>拿起来吗|我打算拿起<OBJ>|把<OBJ>抓起来就行",
        "我需要把<OBJ>夹起来|可不可以帮我抓取<OBJ>|请你把<OBJ>提起来|抓取<OBJ>好吗",
    ),
    "PLACE": (
        "放到<OBJ>|放在<OBJ>|把<OBJ>放到<OBJ>|将<OBJ>放在<OBJ>|我想把<OBJ>放到<OBJ>|放到<OBJ>吧|帮我把当前物体放到<OBJ>|把手里的东西放在<OBJ>|可以帮我放下<OBJ>吗|我需要放下<OBJ>",
        "将<OBJ>放置到<OBJ>|我想放到<OBJ>|把当前持物放到<OBJ>|能不能放下<OBJ>",
        "请把<OBJ>放置在<OBJ>|我希望把<OBJ>放到<OBJ>|放在<OBJ>就行|能把手里的东西放到<OBJ>吗",
    ),
    "MOVE": (
        "我想移动到<OBJ>|我需要前往<OBJ>|能不能移到<OBJ>|移动到<OBJ>吧|让末端移动至<OBJ>|帮我把机械臂移动到<OBJ>|我希望夹爪移动到<OBJ>",
        "我打算让夹爪移到<OBJ>|能帮我移动到<OBJ>吗|我需要把末端移到<OBJ>|机械臂到<OBJ>去吧",
        "我想让夹爪前往<OBJ>|可以把末端移动到<OBJ>吗|我需要让机械臂到达<OBJ>|前往<OBJ>就行",
    ),
    "STOP": (
        "先停下来|赶紧停止|停一下|可以停止了|现在停下吧|把机械臂停下来|停止执行吧|别再动了|先停止当前任务",
        "机械臂先停一下|赶快停下|请停止正在执行的动作|让夹爪停止吧",
        "现在让机械臂停下来|先把任务停了|立即中止当前任务|别继续动了",
    ),
    "RUN_TASK": (
        "我想<TASK>|我打算<TASK>|我需要<TASK>|我决定<TASK>|我要开始<TASK>|能不能帮我<TASK>|可不可以执行<TASK>|帮我完成<TASK>|执行<TASK>吧|可以帮我<TASK>吗|我希望你执行<TASK>|现在就执行<TASK>",
        "我想要<TASK>|我现在要<TASK>|帮我开始<TASK>|能帮我执行<TASK>吗",
        "我要<TASK>|我需要你完成<TASK>|可不可以帮我<TASK>|我想把<TASK>执行一下",
    ),
    "CLARIFY": (
        "<OBJ>|我在考虑要不要<TASK>|如果执行<TASK>会怎么样|假如抓取<OBJ>会怎样|只是问问如何运行<TASK>|我不想<TASK>|暂时不做<TASK>|请不要<TASK>|我还没决定执行<TASK>|先别<TASK>|怎么抓取<OBJ>|介绍一下<TASK>|解释一下抓取<OBJ>的过程|明天执行<TASK>|稍后执行<TASK>|我不知道要不要抓取<OBJ>",
        "我还在想是否执行<TASK>|如果把<OBJ>拿起来会怎样|我只是想了解<TASK>|先不用<TASK>",
        "我想知道怎样执行<TASK>|要不要运行<TASK>|不要帮我<TASK>|只是问问能否抓取<OBJ>",
    ),
    "UNSUPPORTED": (
        "删除<TASK>|复制<TASK>|修改<TASK>|统计<OBJ>的数量|给<OBJ>拍照|把<OBJ>放到<OBJ>然后抓取<OBJ>|执行<TASK>再抓取<OBJ>|拿起<OBJ>并且移动到<OBJ>|翻转<OBJ>|把<OBJ>向右移动",
        "先执行<TASK>然后运行<TASK>|将<OBJ>旋转九十度|为<OBJ>拍一张照片|切割<OBJ>",
        "抓起<OBJ>接着运行<TASK>|运行<TASK>和<TASK>|把<OBJ>翻过来|给<OBJ>照相",
    ),
}
TEMPLATES = {action: tuple("|".join((original, extra)) for original, extra
                          in zip(TEMPLATES[action], CONVERSATIONAL_TEMPLATES[action])) for action in ACTIONS}
PREFIXES = (
    ("", "请", "请你", "帮我", "麻烦", "麻烦你", "现在", "请帮我", "能否", "可以",
     "嗯，", "那个，", "想了想，", "我想好了，", "考虑一下，", "麻烦一下，", "这样吧，", "还是"),
    ("", "请帮我", "嗯，考虑一下，", "我想了想，"),
    ("", "请帮我", "想一想，", "嗯，想一下，", "麻烦你了，"),
)


def generate(directory=DATA_DIRECTORY):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    counts = {}
    seen = set()
    for split_index, split in enumerate(("train", "validation", "test")):
        prefixes = PREFIXES[split_index]
        records = []
        for action in ACTIONS:
            for family, template in enumerate(TEMPLATES[action][split_index].split("|")):
                for index, prefix in enumerate(prefixes):
                    text = prefix + template + ("。" if index % 3 == 1 else "")
                    normalized = normalize_text(text)
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    records.append({"text": text, "action": action,
                                    "family": f"{action}/{split}/{family}", "template": template})
        (directory / f"{split}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        counts[split] = len(records)
    return counts


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False))
