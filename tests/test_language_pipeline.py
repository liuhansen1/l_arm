"""校验四层动作意图模型的结构、输入约束和已训练权重。"""

import unittest
import importlib.util


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
        from llm.model import IntentParser
        predictor = IntentParser()
        for text in ["", " " * 5, "抓" * 129]:
            with self.assertRaises(ValueError):
                predictor.predict(text)

    def test_loaded_model_understands_placeholder_instruction(self):
        from llm.model import IntentParser
        result = IntentParser().predict("帮我抓取<OBJ>")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["action"], "GRASP")


if __name__ == "__main__":
    unittest.main()
