"""使用真实新权重与真实物理，验证动态名称、解析凭据和连续任务。"""

from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import hashlib
import io
from pathlib import Path
import time
import threading
import unittest
from unittest.mock import patch

if importlib.util.find_spec("torch") is None:
    raise unittest.SkipTest("新模型集成检查需要 .venv_train 中的 PyTorch")

import torch

from backend import RequestError
from backend.instructions import extract_command, resolve_name, special_instruction_reason
from backend.server import StudioApplication
from backend.simulation import SimulationService
from backend.store import StudioStore
from llm.evaluate import read_records
from llm.model import INTENT_CHECKPOINT, IntentParser, encode_text
from llm.schema import ACTIONS, normalize_text


class IntentStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(2)
        cls.parser = IntentParser()
        cls.simulation = SimulationService(real_time=False, width=320, height=200, fps=2)
        cls.simulation.start()

    @classmethod
    def tearDownClass(cls):
        cls.simulation.close()

    def setUp(self):
        self.simulation.control("reset")
        self.store = StudioStore(":memory:")
        self.app = StudioApplication(self.simulation, self.store, self.parser)

    def tearDown(self):
        self.store.close()

    def add_object(self, *, name="零件A", color="red", aliases=None, position=None, skills=None):
        return self.store.save_object({"name": name, "aliases": aliases or ["小红块"], "color": color,
            "scene_object_id": f"cube-{color}" if color != "point" else None,
            "position": position or [0.55, -0.18, 0.025], "skill_ids": skills or ["cube_grasp"]})

    def parse(self, instruction):
        return self.app.dispatch("POST", "/api/v1/instructions/parse", {"instruction": instruction}).payload

    def execute(self, result, **extra):
        return self.app.dispatch("POST", "/api/v1/jobs", {"source": "language", "parse_id": result["parse_id"], **extra}).payload

    def wait(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.simulation.get_job(job["id"])
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(0.01)
        self.fail("物理执行超时")

    def task(self, obj):
        point = self.store.save_placement_point({"name": "放置区A", "aliases": ["归位点"], "position": [0.45, 0.15, 0]})
        return self.store.save_task({"name": "零件归位", "aliases": ["收拾零件"], "steps": [
            {"action": "GRASP", "object_id": obj["id"]},
            {"action": "MOVE", "position": [0.45, 0.15, 0.25]},
            {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]},
            {"action": "WAIT", "seconds": 0.5}]})

    def test_trained_model_has_four_layers_one_head_and_atomic_placeholders(self):
        model = self.parser.model
        self.assertEqual(len(model.encoder.layers), 4)
        self.assertEqual(list(model.heads), ["action"])
        self.assertEqual(model.heads["action"].out_features, len(ACTIONS))
        ids = encode_text("<OBJ><TASK>", self.parser.vocab, 8)
        self.assertEqual(ids[:4], [2, self.parser.vocab.index("<OBJ>"), self.parser.vocab.index("<TASK>"), 3])

    def test_dataset_families_and_inputs_do_not_cross_splits(self):
        prior_texts, prior_families, prior_templates = set(), set(), set()
        for split in ("train", "validation", "test"):
            rows = read_records(Path("llm/data") / f"{split}.jsonl")
            texts = {normalize_text(row["text"]) for row in rows}
            families = {row["family"] for row in rows}
            templates = {row["template"] for row in rows}
            self.assertFalse(prior_texts & texts)
            self.assertFalse(prior_families & families)
            self.assertFalse(prior_templates & templates)
            self.assertEqual({row["action"] for row in rows}, set(ACTIONS))
            prior_texts |= texts
            prior_families |= families
            prior_templates |= templates
        original = "想一想，我要<TASK>"
        self.assertNotIn(original, {r["text"] for r in read_records("llm/data/train.jsonl")})
        self.assertIn(original, {r["text"] for r in read_records("llm/data/test.jsonl")})

    def test_conversational_task_requests_use_actual_model_and_registered_aliases(self):
        obj = self.add_object()
        task = self.task(obj)
        before = hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest()
        for text in ("想一想，我要收拾零件", "我想了想，还是执行收拾零件吧",
                     "嗯，想一下，我要收拾零件", "再想想，帮我执行收拾零件",
                     "麻烦你了，我想要收拾零件"):
            result = self.parse(text)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["source"], "model")
            self.assertEqual(result["action"], "RUN_TASK")
            self.assertEqual(result["task_id"], task["id"])
            self.assertEqual(result["normalized_instruction"], normalize_text(text).replace("收拾零件", "<TASK>"))
        self.assertEqual(before, hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest())

    def test_conversational_model_inputs_include_fillers_without_changing_action_head(self):
        for text, action in (("嗯，麻烦帮我抓取<OBJ>", "GRASP"), ("想一想，我要<TASK>", "RUN_TASK"),
                             ("我想了想，还是执行<TASK>吧", "RUN_TASK"), ("嗯，放到<OBJ>吧", "PLACE"),
                             ("考虑一下，移动到<OBJ>吧", "MOVE"), ("想一想，停止当前任务", "STOP")):
            result = self.parser.predict(text)
            self.assertEqual(result["action"], action, (text, result))
            self.assertTrue(result["accepted"], (text, result))
        self.assertEqual(self.parser.model.heads["action"].out_features, len(ACTIONS))

    def test_background_sentences_are_excluded_without_opening_phrase_rules(self):
        obj = self.add_object()
        task = self.task(obj)
        for text, model_input, action, target in (
                ("我在上课，我想收拾零件", "<TASK>", "RUN_TASK", {"task_id": task["id"]}),
                ("我在开会，我想收拾零件", "<TASK>", "RUN_TASK", {"task_id": task["id"]}),
                ("我在上课，我想执行收拾零件", "执行<TASK>", "RUN_TASK", {"task_id": task["id"]}),
                ("我在上课，帮我抓取零件A", "抓取<OBJ>", "GRASP", {"object_id": obj["id"]})):
            placeholder = "<TASK>" if action == "RUN_TASK" else "<OBJ>"
            name = "收拾零件" if action == "RUN_TASK" else "零件A"
            expected = normalize_text(text).replace(name, placeholder)
            with patch.object(self.parser, "predict", wraps=self.parser.predict) as predict:
                result = self.parse(text)
                predict.assert_called_once_with(model_input)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["action"], action)
            self.assertEqual(result["normalized_instruction"], expected)
            for key, value in target.items():
                self.assertEqual(result[key], value)
            self.assertEqual(result["model_input"], model_input)
        self.assertIsNone(self.simulation.active_job)

    def test_start_wrappers_and_move_complements_are_single_commands(self):
        obj = self.add_object(name="1")
        task = self.task(obj)
        for text, expected, action in (("我在玩游戏，开始抓取1", "抓取<OBJ>", "GRASP"),
                                       ("开始执行抓取1", "抓取<OBJ>", "GRASP"),
                                       ("开始执行收拾零件", "开始执行<TASK>", "RUN_TASK"),
                                       ("我在玩游戏我想收拾零件", "<TASK>", "RUN_TASK")):
            result = self.parse(text)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["model_input"], expected)
            self.assertEqual(result["action"], action)
        point = self.add_object(name="观察点", color="point", aliases=["待机点"], position=[0.45, 0.15, 0.25])
        result = self.parse("移动夹爪到观察点")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["action"], "MOVE")
        self.assertEqual(result["object_id"], point["id"])

    def test_background_conjunctions_are_not_treated_as_multiple_targets(self):
        obj = self.add_object()
        self.task(obj)
        for text in ("我和同学在上课，我想收拾零件", "我与同学在上课，我想收拾零件",
                     "请及时执行收拾零件", "再想想，帮我执行收拾零件"):
            normalized, match, reason = resolve_name(text, self.store.snapshot())
            self.assertIsNone(reason)
            self.assertIsNotNone(match)
            self.assertIsNone(special_instruction_reason(normalized))
            model_input, reason = extract_command(normalized)
            self.assertIsNone(reason)
            # 模型可因意图或置信度拒绝，但数据库规则不能提前拒绝普通背景。
            with patch.object(self.parser, "predict", wraps=self.parser.predict) as predict:
                result = self.parse(text)
                predict.assert_called_once_with(model_input)
            self.assertEqual(result["normalized_instruction"], normalized)
            self.assertIn("action", result)

    def test_unseen_background_characters_do_not_override_actual_model_prediction(self):
        obj = self.add_object(name="1")
        text = "我在大游戏，现在我口可乐，我想抓取1"
        normalized = "我在大游戏,现在我口可乐,我想抓取<OBJ>"
        prediction = self.parser.predict(normalized)
        self.assertGreater(prediction["unknown_ratio"], 0.2)
        self.assertEqual(prediction["action"], "GRASP")
        self.assertTrue(prediction["accepted"], prediction)
        self.assertIsNone(prediction["reason"])
        core_prediction = self.parser.predict("抓取<OBJ>")
        with patch.object(self.parser, "predict", wraps=self.parser.predict) as predict:
            result = self.parse(text)
            predict.assert_called_once_with("抓取<OBJ>")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["object_id"], obj["id"])
        self.assertEqual(result["action"], core_prediction["action"])
        self.assertAlmostEqual(result["confidence"], core_prediction["confidence"], places=6)
        self.assertEqual(result["normalized_instruction"], normalized)
        self.assertEqual(result["model_input"], "抓取<OBJ>")
        # 取消词表比例门槛不改变实际模型分数或置信度门槛。
        with patch.object(self.parser, "threshold", 1.0):
            rejected = self.parser.predict(normalized)
        self.assertEqual(rejected["action"], prediction["action"])
        self.assertAlmostEqual(rejected["confidence"], prediction["confidence"], places=6)
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "模型置信度不足，请换一种明确的表达。")
        for unsafe in ("我在大游戏，现在我口可乐，我不想抓取1",
                       "我在大游戏，现在我口可乐，我想抓取未登记物体"):
            rejected = self.parse(unsafe)
            self.assertNotEqual(rejected["status"], "ready", rejected)
            self.assertNotIn("parse_id", rejected)
        self.assertIsNone(self.simulation.active_job)

    def test_irrelevant_words_before_and_after_one_command_do_not_change_prediction(self):
        obj = self.add_object(name="1")
        expected = self.parser.predict("抓取<OBJ>")
        for text in ("我在大游戏，现在我口可乐，我想抓取1",
                     "我在打游戏现在想喝可乐我想抓取1",
                     "抓取1，我在打游戏，今天天气很好",
                     "我在打游戏，我想抓取1今天天气很好",
                     "今天的天气很好，" * 12 + "我想抓取1"):
            with patch.object(self.parser, "predict", wraps=self.parser.predict) as predict:
                result = self.parse(text)
                predict.assert_called_once_with("抓取<OBJ>")
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["object_id"], obj["id"])
            self.assertEqual(result["action"], expected["action"])
            self.assertAlmostEqual(result["confidence"], expected["confidence"], places=6)
        self.assertIsNone(self.simulation.active_job)

    def test_multiple_commands_without_connectors_are_not_reduced_to_one(self):
        self.add_object(name="1")
        for text in ("抓取1，放下", "抓取1放下", "我想抓取1，停止当前任务",
                     "抓取1，抓取未登记物体", "旋转未登记物体，抓取1"):
            with patch.object(self.parser, "predict", wraps=self.parser.predict) as predict:
                result = self.parse(text)
                predict.assert_not_called()
            self.assertEqual(result["status"], "clarify", result)
            self.assertNotIn("parse_id", result)
        for text in ("我在大游戏，现在我口可乐", "今天天气很好"):
            result = self.parse(text)
            self.assertNotEqual(result["status"], "ready", result)
            self.assertNotIn("parse_id", result)

    def test_special_requests_with_one_known_target_still_require_clarification(self):
        obj = self.add_object()
        self.task(obj)
        for text in ("我在上课，我不想收拾零件", "如果可以，执行收拾零件",
                     "假如有空，我想收拾零件", "等到下课，执行收拾零件",
                     "执行收拾零件和未知任务", "抓取未知物体与零件A",
                     "抓取零件A或者未知物体", "抓取零件A再放下", "抓取零件A左边的物体",
                     "解释一下抓取零件A的过程", "明天执行收拾零件", "等我下课再抓取零件A",
                     "我不确定执行收拾零件", "先不要，抓取零件A", "抓取零件A，不要"):
            result = self.parse(text)
            self.assertEqual(result["status"], "clarify", result)
            self.assertNotIn("parse_id", result)
        self.assertIsNone(self.simulation.active_job)

    def test_conversational_fillers_do_not_hide_unknown_targets_negation_or_questions(self):
        obj = self.add_object()
        self.task(obj)
        for text in ("想一想，我不要收拾零件", "嗯，先别执行收拾零件", "想一想，别停止",
                     "我想了想，不需要抓取零件A",
                     "想一想，我要收拾未知零件", "想一想，执行收拾零件和未知任务",
                     "想一想，先抓零件A然后执行收拾零件", "嗯，如何执行收拾零件",
                     "只是问问，能不能抓取零件A", "如果可以，执行收拾零件"):
            result = self.parse(text)
            self.assertNotEqual(result["status"], "ready", result)
            self.assertNotIn("parse_id", result)
        self.assertIsNone(self.simulation.active_job)

    def test_conversational_grasp_and_explicit_negative_stop_are_supported(self):
        obj = self.add_object()
        grasp = self.parse("嗯，麻烦帮我抓取零件A")
        self.assertEqual(grasp["status"], "ready", grasp)
        self.assertEqual(grasp["object_id"], obj["id"])
        for text in ("别动", "请不要动", "别再执行了", "嗯，别再动了", "我想了想，不要再动了"):
            with patch.object(self.parser, "predict", wraps=self.parser.predict) as predict:
                result = self.parse(text)
                predict.assert_called_once_with(result["model_input"])
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["action"], "STOP")
        # 统一提取动作分句，不需要单独枚举“想一想”这样的开场。
        result = self.parse("想一想，别动")
        self.assertEqual(result["model_input"], "别动")
        self.assertEqual(result["action"], self.parser.predict("别动")["action"])
        self.assertEqual(result["status"], "ready", result)

    def test_new_names_and_aliases_work_without_changing_weights_or_vocab(self):
        before = hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest()
        vocabulary = list(self.parser.vocab)
        obj = self.add_object(name="客户定制零件甲", aliases=["任意新叫法"])
        for name in (obj["name"], obj["aliases"][0]):
            result = self.parse("帮我抓取" + name)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["action"], "GRASP")
            self.assertEqual(result["object_id"], obj["id"])
            self.assertEqual(result["normalized_instruction"], "帮我抓取<OBJ>")
        self.assertEqual(vocabulary, self.parser.vocab)
        self.assertEqual(before, hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest())

    def test_unknown_targets_and_ascii_partial_names_never_execute(self):
        self.add_object()
        for text in ("抓取黄色方块", "抓取零件AB", "抓取小红块和未登记物体"):
            result = self.parse(text)
            self.assertNotEqual(result["status"], "ready", result)
            self.assertNotIn("parse_id", result)
        self.assertEqual(self.parse("抓取黄色方块")["status"], "unmatched")
        self.assertIsNone(self.simulation.active_job)

    def test_unique_registered_name_is_replaced_without_chinese_modifier_whitelist(self):
        obj = self.add_object()
        text = "想一想，我要抓取大零件A"
        result = self.parse(text)
        self.assertEqual(result["normalized_instruction"], "想一想,我要抓取大<OBJ>")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["object_id"], obj["id"])
        self.assertEqual(result["action"], "GRASP")

    def test_negative_instructions_and_multiple_targets_are_rejected(self):
        self.add_object()
        self.add_object(name="零件B", color="blue", aliases=["小蓝块"], position=[0.55, 0.18, 0.025])
        for text in ("不要抓取零件A", "别拿起小红块", "不要停止", "抓取零件A和零件B",
                     "抓取零件A，抓取小红块", "先抓零件A再放下", "抓取零件A左边的方块"):
            self.assertNotEqual(self.parse(text)["status"], "ready", text)
        self.assertEqual(self.parse("停止当前任务")["action"], "STOP")

    def test_cross_namespace_alias_collisions_and_reserved_names_are_rejected(self):
        obj = self.add_object(aliases=["客户 零件"])
        for name in ("客户零件", "停止", "<OBJ>"):
            with self.assertRaises(RequestError):
                self.store.save_task({"name": name, "aliases": [], "steps": [{"action": "WAIT", "seconds": 1}]})
        for identifier in (True, "1", 0):
            with self.assertRaises(RequestError):
                self.store.object(identifier)
        task = self.task(obj)
        with self.assertRaises(RequestError):
            self.add_object(name="另一个目标", color="blue", aliases=[task["name"]])

    def test_longest_registered_name_wins_over_an_embedded_alias(self):
        self.add_object(name="零件", aliases=["短名字"])
        obj = self.add_object(name="零件甲", color="blue", aliases=["蓝方块"])
        normalized, match, reason = resolve_name("抓取零件甲", self.store.snapshot())
        self.assertIsNone(reason)
        self.assertEqual(match["record"]["id"], obj["id"])
        self.assertEqual(normalized, "抓取<OBJ>")

    def test_modified_parse_parameters_and_missing_tokens_are_rejected(self):
        obj = self.add_object()
        result = self.parse("抓取零件A")
        for extra in ({"object_id": obj["id"] + 1}, {"action": "PLACE"}, {"position": [0, 0, 0]}, {"instruction": "放下零件A"}):
            with self.assertRaises(RequestError):
                self.execute(result, **extra)
        with self.assertRaises(RequestError):
            self.app.instructions.execute({"parse_id": "invented"})
        self.assertIsNone(self.simulation.active_job)

    def test_clear_memory_after_grasp_invalidates_tokens_and_preserves_model(self):
        before = hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest()
        self.add_object()
        pending = self.parse("抓取零件A")
        with redirect_stdout(io.StringIO()):
            job = self.wait(self.execute(self.parse("抓取零件A")))
        self.assertEqual(job["status"], "succeeded", job)
        self.assertIsNotNone(self.simulation.snapshot()["held_object_id"])
        response = self.app.dispatch("POST", "/api/v1/memory/clear", {}).payload
        self.assertEqual(response["cleared"]["parses"], 2)
        self.assertEqual(self.app.instructions.parses, {})
        self.assertIsNone(self.simulation.snapshot()["held_object_id"])
        with self.assertRaises(RequestError):
            self.execute(pending)
        forgotten = self.parse("抓取零件A")
        self.assertNotEqual(forgotten["status"], "ready")
        self.assertNotIn("parse_id", forgotten)
        self.add_object(name="新零件")
        self.assertEqual(self.parse("抓取新零件")["status"], "ready")
        self.assertEqual(before, hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest())

    def test_inflight_prediction_cannot_restore_parse_memory_after_clear(self):
        self.add_object()
        entered, resume = threading.Event(), threading.Event()
        predict = self.parser.predict

        def blocked_predict(text):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("测试没有及时释放预测线程")
            return predict(text)

        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(self.parser, "predict", blocked_predict):
            future = executor.submit(self.parse, "抓取零件A")
            try:
                self.assertTrue(entered.wait(5))
                self.app.dispatch("POST", "/api/v1/memory/clear", {})
            finally:
                resume.set()
            result = future.result(timeout=5)
        self.assertEqual(result["status"], "clarify", result)
        self.assertNotIn("parse_id", result)
        self.assertEqual(self.app.instructions.parses, {})

    def test_database_edits_scene_resets_and_expiry_invalidate_parses(self):
        obj = self.add_object()
        result = self.parse("抓取零件A")
        self.store.save_object({**obj, "position": [0.56, -0.18, 0.025]}, obj["id"])
        with self.assertRaises(RequestError):
            self.execute(result)
        result = self.parse("抓取零件A")
        self.simulation.control("reset")
        with self.assertRaises(RequestError):
            self.execute(result)
        result = self.parse("抓取零件A")
        self.app.instructions.parses[result["parse_id"]]["created"] -= 121
        with self.assertRaises(RequestError):
            self.execute(result)

    def test_language_grasp_holds_then_place_runs_without_reset_and_token_cannot_replay(self):
        obj = self.add_object()
        result = self.parse("帮我把小红块拿起来")
        self.assertEqual(result["status"], "ready", result)
        with redirect_stdout(io.StringIO()):
            job = self.execute(result)
            with self.assertRaises(RequestError):
                self.execute(result)
            job = self.wait(job)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(self.simulation.snapshot()["held_object_id"], obj["id"])
        self.assertFalse(self.simulation.snapshot()["requires_reset"])
        self.store.save_placement_point({"name": "原位点", "aliases": [], "position": [0.55, -0.18, 0]})
        result = self.parse("放到原位点")
        self.assertEqual(result["status"], "ready", result)
        job = self.wait(self.execute(result))
        self.assertEqual(job["status"], "succeeded", job)
        self.assertTrue(job["result"]["steps"][0]["result"]["supported"])
        self.assertIsNone(self.simulation.snapshot()["held_object_id"])

    def test_new_task_alias_physically_executes_full_program_without_retraining(self):
        obj = self.add_object()
        task = self.task(obj)
        result = self.parse("我在上课，我想收拾零件")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["action"], "RUN_TASK")
        self.assertEqual(result["task_id"], task["id"])
        with redirect_stdout(io.StringIO()):
            job = self.wait(self.execute(result))
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual([r["action"] for r in job["step_results"]], ["GRASP", "MOVE", "PLACE", "WAIT"])
        self.assertFalse(self.simulation.snapshot()["requires_reset"])
        self.assertEqual(self.store.object(obj["id"])["position"], obj["position"])

    def test_task_builder_runs_saved_program_with_same_checks(self):
        task = self.store.save_task({"name": "等待演示", "aliases": [], "steps": [{"action": "WAIT", "seconds": 0.1}]})
        response = self.app.dispatch("POST", "/api/v1/jobs", {"action": "RUN_TASK", "task_id": task["id"], "source": "task_builder"})
        self.assertEqual(self.wait(response.payload)["status"], "succeeded")

    def test_failed_grasp_aborts_following_steps_and_requires_reset(self):
        obj = self.add_object(position=[0.55, -0.08, 0.025])
        task = self.task(obj)
        with redirect_stdout(io.StringIO()):
            response = self.app.dispatch("POST", "/api/v1/jobs", {"action": "RUN_TASK", "task_id": task["id"], "source": "task_builder"})
            job = self.wait(response.payload)
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(job["current_step"], 1)
        self.assertEqual(len(job["step_results"]), 1)
        self.assertTrue(self.simulation.snapshot()["requires_reset"])

    def test_place_before_grasp_and_missing_skills_are_rejected_before_motion(self):
        obj = self.add_object()
        self.assertEqual(self.parse("放下零件A")["status"], "clarify")
        point = self.store.save_placement_point({"name": "放置点", "position": [0.45, 0.15, 0]})
        task = self.store.save_task({"name": "错误顺序", "aliases": [], "steps": [{"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
        with self.assertRaises(RequestError):
            self.app.dispatch("POST", "/api/v1/jobs", {"action": "RUN_TASK", "task_id": task["id"], "source": "task_builder"})
        self.store.save_object({**obj, "skill_ids": ["cube_place"]}, obj["id"])
        self.assertEqual(self.parse("抓取零件A")["status"], "clarify")
        self.assertIsNone(self.simulation.active_job)

    def test_named_point_supplies_absolute_move_position(self):
        obj = self.add_object(name="待机点", color="point", aliases=["观察点"], position=[0.45, 0.15, 0.25])
        result = self.parse("移动到观察点")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["position"], obj["position"])
        job = self.wait(self.execute(result))
        self.assertEqual(job["status"], "succeeded", job)
        self.assertLess(job["result"]["steps"][0]["result"]["end_effector_error_m"], 0.01)

    def test_stop_cancels_program_and_invalidates_other_parse_tokens(self):
        self.add_object()
        result = self.parse("抓取零件A")
        job = self.simulation.submit_program([{"action": "WAIT", "seconds": 60}])
        self.simulation.control("stop")
        self.assertEqual(self.simulation.get_job(job["id"])["status"], "cancelled")
        with self.assertRaises(RequestError):
            self.execute(result)

    def test_coordinate_trial_cannot_open_gripper_while_a_program_holds_an_object(self):
        self.add_object()
        result = self.parse("抓取零件A")
        with redirect_stdout(io.StringIO()):
            job = self.wait(self.execute(result))
        self.assertEqual(job["status"], "succeeded")
        with self.assertRaises(RequestError):
            self.simulation.submit_grasp("cube-blue", [0.55, 0.18, 0.025])

    def test_non_string_parse_id_is_rejected_without_internal_error(self):
        for value in (None, [], {}, 1, True):
            with self.assertRaises(RequestError) as error:
                self.app.instructions.execute({"parse_id": value})
            self.assertEqual(error.exception.status, 409)

    def test_model_unavailable_reports_error_without_rule_fallback(self):
        self.app.instructions.parser = None
        from unittest.mock import patch
        with patch("llm.model.IntentParser", side_effect=OSError("missing")):
            with self.assertRaises(RequestError) as error:
                self.parse("停止当前任务")
        self.assertEqual(error.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
