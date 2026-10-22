"""不依赖模型服务的 Ollama 响应检查与空间抓取入口回归测试。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import pybullet as p

import qwen.grasp as language_grasp
from llm.schema import task


class OllamaResponseTests(unittest.TestCase):
    def parse_response(self, response):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(response).encode())
        with patch("qwen.grasp.build_opener", return_value=opener):
            result = language_grasp.parse_instruction("抓取红色方块左边的方块")
        return result, opener.open.call_args

    def test_spatial_task_and_request_contract(self):
        expected = task(reference_color="red", relation="left_of")
        result, call = self.parse_response(
            dict(done=True, message=dict(content=json.dumps(expected)))
        )
        self.assertEqual(result, expected)
        payload = json.loads(call.args[0].data)
        self.assertEqual(payload["model"], "qwen3:4b")
        self.assertEqual(payload["messages"][-1]["content"], "抓取红色方块左边的方块")
        self.assertEqual(set(payload["format"]["required"]), set(expected))
        self.assertFalse(payload["format"]["additionalProperties"])
        self.assertFalse(payload["think"])

    def test_invalid_model_tasks_are_rejected(self):
        invalid = [
            [],
            dict(status="execute", color="red", message="抓红色"),
            {**task(), "target_color": "yellow"},
            {**task(), "relation": "left_of"},
            {**task(), "reference_color": "red"},
            {**task("clarify"), "target_color": "red"},
            {**task(), "relation": "front_of"},
            {**task(), "unexpected": "red"},
            {key: value for key, value in task().items() if key != "exclude_color"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse_response(dict(done=True, message=dict(content=json.dumps(value))))

    def test_incomplete_and_malformed_responses_are_rejected(self):
        responses = [
            [],
            dict(done=False, message=dict(content=json.dumps(task()))),
            dict(done=True),
            dict(done=True, message="invalid"),
            dict(done=True, message=dict(content="")),
            dict(done=True, message=dict(content="not JSON")),
        ]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.parse_response(response)

    def test_service_errors_stop_the_request(self):
        errors = [
            URLError("connection refused"),
            TimeoutError(),
            HTTPError(language_grasp.CHAT_URL, 500, "failed", {}, io.BytesIO(b"failed")),
        ]
        for error in errors:
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(error=error), patch(
                "qwen.grasp.build_opener", return_value=opener
            ), self.assertRaises(RuntimeError):
                language_grasp.parse_instruction("抓红色方块")


class OllamaEntryTests(unittest.TestCase):
    def run_entry(self, parsed, options):
        output = io.StringIO()
        with patch("qwen.grasp.parse_instruction", return_value=parsed), redirect_stdout(output):
            code = language_grasp.main(["测试指令", *options])
        return code, output.getvalue()

    def test_non_grasp_and_parse_only_do_not_create_a_scene(self):
        cases = [
            (task("clarify"), [], 2),
            (task("unsupported"), [], 2),
            (task(reference_color="red", relation="left_of"), ["--parse-only"], 0),
        ]
        for parsed, options, expected_code in cases:
            with self.subTest(parsed=parsed, options=options), patch("pybullet.connect") as connect:
                code, _ = self.run_entry(parsed, options)
                self.assertEqual(code, expected_code)
                connect.assert_not_called()

    def test_actual_layout_controls_selection_and_ambiguity(self):
        cases = [
            (task(reference_color="red", relation="left_of"), "blue,red,green", "选择蓝色", 0),
            (task(reference_color="red", relation="left_of"), "green,red,blue", "选择绿色", 0),
            (task(reference_color="red", relation="right_of"), "blue,red,green", "选择绿色", 0),
            (task(relation="leftmost"), "blue,red,green", "选择蓝色", 0),
            (task(reference_color="green", relation="left_of"), "blue,red,green", "有多个方块", 2),
            (task(reference_color="blue", relation="left_of"), "blue,red,green", "没有满足全部条件", 2),
            (task(exclude_color="red"), "blue,red,green", "有多个方块", 2),
        ]
        for parsed, layout, message, expected_code in cases:
            with self.subTest(parsed=parsed, layout=layout), patch(
                "robot.grasp.grasp_cube", return_value=0.22
            ) as grasp:
                code, output = self.run_entry(
                    parsed, ["--headless", "--layout", layout, "--seed", "42"]
                )
                self.assertEqual(code, expected_code)
                self.assertIn(message, output)
                self.assertEqual(grasp.call_count, int(expected_code == 0))
                self.assertFalse(p.isConnected())

    def test_default_layout_keeps_original_color_order(self):
        with patch("robot.grasp.grasp_cube", return_value=0.22):
            code, output = self.run_entry(task(relation="leftmost"), ["--headless"])
        self.assertEqual(code, 0)
        self.assertIn("选择蓝色", output)
        self.assertFalse(p.isConnected())

    def test_failed_grasp_disconnects_the_scene(self):
        with patch("robot.grasp.grasp_cube", side_effect=RuntimeError("夹持失败")), redirect_stderr(io.StringIO()):
            code, _ = self.run_entry(task(target_color="red"), ["--headless"])
        self.assertEqual(code, 1)
        self.assertFalse(p.isConnected())

    def test_invalid_layout_is_rejected_before_model_request(self):
        with patch("qwen.grasp.parse_instruction") as parse, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                language_grasp.main(["抓红色方块", "--layout", "red,red,blue"])
        self.assertEqual(error.exception.code, 2)
        parse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
