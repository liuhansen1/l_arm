"""校验保留的模型推理与场景关系行为，不依赖训练数据。"""

import unittest
import importlib.util

from llm.schema import task
from robot.scene_reasoning import resolve_task


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.objects = [dict(id=1, color="blue", position=[0.55, 0.18, 0.025]),
                        dict(id=2, color="red", position=[0.55, 0, 0.025]),
                        dict(id=3, color="green", position=[0.55, -0.18, 0.025])]

    def test_reference_is_not_the_target(self):
        result = resolve_task(task(reference_color="red", relation="left_of"), self.objects)
        self.assertEqual(result["target_id"], 1)

    def test_swapping_layout_changes_selected_object(self):
        self.objects[0]["position"], self.objects[2]["position"] = self.objects[2]["position"], self.objects[0]["position"]
        result = resolve_task(task(reference_color="red", relation="left_of"), self.objects)
        self.assertEqual(result["target_id"], 3)

    def test_multiple_targets_require_clarification(self):
        result = resolve_task(task(reference_color="green", relation="left_of"), self.objects)
        self.assertEqual(result["status"], "clarify")
        self.assertIsNone(result["target_id"])

    def test_missing_reference_and_impossible_target(self):
        result = resolve_task(task(reference_color="red", relation="left_of"), [self.objects[0]])
        self.assertEqual(result["status"], "not_found")
        result = resolve_task(task(target_color="green", reference_color="red", relation="left_of"), self.objects)
        self.assertEqual(result["status"], "not_found")

    def test_exclusion_does_not_arbitrarily_choose(self):
        result = resolve_task(task(exclude_color="red"), self.objects)
        self.assertEqual(result["status"], "clarify")

    def test_near_equal_extreme_is_ambiguous(self):
        self.objects[1]["position"][1] = 0.175
        self.assertEqual(resolve_task(task(relation="leftmost"), self.objects)["status"], "clarify")

    def test_unsupported_never_executes(self):
        self.assertEqual(resolve_task(task("unsupported"), self.objects)["status"], "unsupported")


@unittest.skipUnless(importlib.util.find_spec("torch") is not None, "需要训练虚拟环境中的 PyTorch")
class ModelTests(unittest.TestCase):
    def test_encoder_has_only_one_fixed_action_head(self):
        import torch
        from llm.model import GraspLanguageModel, ModelConfig
        torch.set_num_threads(2)
        model = GraspLanguageModel(ModelConfig(vocab_size=8, d_model=32, num_heads=4, num_layers=4, dim_feedforward=64)).eval()
        with torch.inference_mode():
            logits = model(torch.tensor([[2, 4, 7, 3]]))
        self.assertEqual(set(logits), {"action"})
        self.assertEqual(tuple(logits["action"].shape), (1, 7))
        self.assertEqual(len(model.encoder.layers), 4)

    def test_predictor_rejects_empty_and_overlong_input(self):
        from llm.model import TaskParser
        predictor = TaskParser()
        for text in ["", " " * 5, "抓" * 129]:
            with self.assertRaises(ValueError):
                predictor.predict(text)

    def test_loaded_model_understands_placeholder_instruction(self):
        from llm.model import TaskParser
        result = TaskParser().predict("帮我抓取<OBJ>")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["action"], "GRASP")


if __name__ == "__main__":
    unittest.main()
