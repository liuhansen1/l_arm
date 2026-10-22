"""点选、持久化、名称角色匹配和真实定点放置验证。"""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

if importlib.util.find_spec("torch") is None:
    raise unittest.SkipTest("定点放置集成检查需要 .venv_train")

import numpy as np
import pybullet as p
import torch

from backend import RequestError
from backend.instructions import prepare_steps
from backend.server import StudioApplication
from backend.simulation import SimulationService
from backend.store import StudioStore
from llm.model import INTENT_CHECKPOINT, IntentParser
from robot.skills import place_cube


class PlacementPointTests(unittest.TestCase):
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

    def object(self):
        return self.store.save_object({"name": "零件A", "aliases": ["小红块"], "color": "red",
            "scene_object_id": "cube-red", "position": [0.55, -0.18, 0.025]})

    def point(self, **overrides):
        return self.store.save_placement_point({"name": "放置区A", "aliases": ["归位点"],
            "position": [0.45, 0.15, 0], **overrides})

    def parse(self, text):
        return self.app.instructions.parse({"instruction": text})

    def execute(self, result):
        self.assertEqual(result["status"], "ready", result)
        response = self.app.dispatch("POST", "/api/v1/jobs", {"source": "language", "parse_id": result["parse_id"]})
        job = response.payload
        deadline = time.monotonic() + 15
        while job["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.01)
            job = self.simulation.get_job(job["id"])
        self.assertNotIn(job["status"], ("queued", "running"))
        return job

    def grasp(self):
        with redirect_stdout(io.StringIO()):
            job = self.execute(self.parse("抓取零件A"))
        self.assertEqual(job["status"], "succeeded", job)

    def picked_top(self, scene_object_id):
        scene = self.simulation.snapshot()
        cube = next(obj for obj in scene["objects"] if obj["scene_object_id"] == scene_object_id)
        return self.simulation.pick({"frame_id": scene["frame_id"],
                                    "x": cube["screen_position"][0], "y": cube["screen_position"][1]})

    def test_actual_clicked_surface_is_saved_without_registering_an_object(self):
        self.assertEqual(self.store.placement_points(), [])
        scene = self.simulation.snapshot()
        camera = scene["camera"]
        clip = (np.array(camera["projection_matrix"]).reshape(4, 4, order="F")
                @ np.array(camera["view_matrix"]).reshape(4, 4, order="F") @ [0.4, -0.25, 0, 1])
        clip = clip / clip[3]
        pick = self.simulation.pick({"frame_id": scene["frame_id"], "x": (clip[0] + 1) / 2,
                                     "y": (1 - clip[1]) / 2})
        self.assertEqual(pick["position_source"], "ground_plane_projection")
        self.assertEqual(self.store.placement_points(), [])
        response = self.app.dispatch("POST", "/api/v1/placement-points", {"name": "点选放置点",
            "aliases": [], "position": pick["hit_position"], "coordinate_frame": "world"})
        self.assertEqual(response.status, 201)
        self.assertEqual(response.payload["position"], pick["hit_position"])
        self.assertEqual(response.payload["position"][2], 0)
        self.assertEqual(self.store.objects(), [])

    def test_sqlite_point_migration_preserves_existing_objects_and_persists_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite3"
            store = StudioStore(path)
            obj = store.save_object({"name": "已有零件", "color": "red", "scene_object_id": "cube-red",
                                      "position": [0.55, -0.18, 0.025]})
            # 仅操作临时夹具，模拟新增放置点表以前的数据库。
            store.connection.execute("DROP TABLE placement_points")
            store.close()
            store = StudioStore(path)
            self.assertEqual(store.object(obj["id"]), obj)
            point = store.save_placement_point({"name": "用户放置点", "aliases": ["目的地"],
                                                 "position": [0.451234, 0.156789, 0]})
            self.assertEqual(point["position"], [0.451, 0.157, 0])
            edited = store.save_placement_point({**point, "position": [0.4, 0.2, 0]}, point["id"])
            store.close()
            store = StudioStore(path)
            try:
                self.assertEqual(store.placement_point(point["id"]), edited)
                self.assertEqual(store.object(obj["id"]), obj)
            finally:
                store.close()

    def test_point_api_update_delete_references_and_name_conflicts(self):
        obj = self.object()
        point = self.point()
        for name in (obj["name"], obj["aliases"][0]):
            with self.assertRaises(RequestError):
                self.point(name=name, aliases=[])
        updated = self.app.dispatch("PUT", f'/api/v1/placement-points/{point["id"]}',
                                    {**point, "name": "新目的地"}).payload
        self.assertEqual(updated["name"], "新目的地")
        task = self.store.save_task({"name": "抓放任务", "aliases": [], "steps": [
            {"action": "GRASP", "object_id": obj["id"]},
            {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
        with self.assertRaises(RequestError):
            self.store.delete_placement_point(point["id"])
        self.store.delete_task(task["id"])
        self.assertEqual(self.app.dispatch("DELETE", f'/api/v1/placement-points/{point["id"]}').status, 204)
        self.assertEqual(self.app.dispatch("GET", "/api/v1/placement-points").payload, [])

    def test_place_requires_an_explicit_point_in_new_and_legacy_tasks(self):
        obj = self.object()
        self.point()
        task = {"name": "缺少目的地", "aliases": [], "steps": [{"action": "PLACE", "object_id": obj["id"]}]}
        with self.assertRaises(RequestError):
            self.store.save_task(task)
        with self.store.connection:
            cursor = self.store.connection.execute("INSERT INTO tasks(name,aliases,steps) VALUES(?,?,?)",
                (task["name"], "[]", json.dumps(task["steps"])))
        before = self.store.task(cursor.lastrowid)
        with self.assertRaises(RequestError):
            prepare_steps("RUN_TASK", self.store.snapshot(), task_id=cursor.lastrowid)
        self.assertEqual(self.store.task(cursor.lastrowid), before)
        self.assertEqual(self.parse("放下零件A")["status"], "clarify")

    def test_named_object_and_point_place_physically_without_changing_model(self):
        before = hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest()
        vocab = list(self.parser.vocab)
        obj = self.object()
        point = self.point()
        self.grasp()
        result = self.parse("帮我把小红块放到归位点")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["action"], "PLACE")
        self.assertEqual(result["normalized_instruction"], "帮我把<OBJ>放到<OBJ>")
        self.assertEqual(result["object_id"], obj["id"])
        self.assertEqual(result["placement_point_id"], point["id"])
        job = self.execute(result)
        self.assertEqual(job["status"], "succeeded", job)
        detail = job["step_results"][0]["result"]
        self.assertEqual(detail["requested_surface_position"], point["position"])
        self.assertLess(detail["placement_error_m"], 0.035)
        self.assertTrue(detail["supported"])
        self.assertIsNone(self.simulation.snapshot()["held_object_id"])
        self.assertEqual(vocab, self.parser.vocab)
        self.assertEqual(before, hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest())

    def test_point_only_command_uses_current_held_object_and_empty_gripper_is_rejected(self):
        obj = self.object()
        point = self.point()
        self.assertEqual(self.parse("放到归位点")["status"], "clarify")
        self.grasp()
        for text in ("把当前物体放到归位点", "把手里的东西放在归位点", "放到归位点"):
            result = self.parse(text)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["object_id"], obj["id"])
            self.assertEqual(result["placement_point_id"], point["id"])
        self.assertEqual(self.execute(result)["status"], "succeeded")

    def test_clicked_cube_top_is_saved_and_stacked_on_without_retraining(self):
        before = hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest()
        obj = self.object()
        pick = self.picked_top("cube-blue")
        self.assertEqual(pick["scene_object_id"], "cube-blue")
        self.assertGreater(pick["surface_normal"][2], 0.99)
        point = self.point(position=pick["hit_position"])
        self.assertEqual(point["position"], pick["hit_position"])
        self.assertLessEqual(abs(point["position"][2] - 0.05), 0.005)
        self.assertNotEqual(point["position"], pick["grasp_position"])
        self.assertEqual(len(self.store.objects()), 1)  # 支撑方块不需要登记进物体库。
        self.grasp()
        result = self.parse("把零件A放到放置区A")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["action"], "PLACE")
        job = self.execute(result)
        self.assertEqual(job["status"], "succeeded", job)
        detail = job["step_results"][0]["result"]
        self.assertEqual(detail["support_type"], "cube")
        self.assertEqual(detail["support_scene_object_id"], "cube-blue")
        self.assertTrue(detail["supported"])
        self.assertEqual(detail["stable_seconds"], 1)
        self.assertEqual(detail["requested_surface_position"], point["position"])
        self.assertGreater(detail["position"][2], 0.07)
        self.assertLess(detail["placement_error_m"], 0.035)
        self.assertIsNone(self.simulation.snapshot()["held_object_id"])
        self.assertFalse(self.simulation.snapshot()["requires_reset"])
        self.assertEqual(before, hashlib.sha256(INTENT_CHECKPOINT.read_bytes()).hexdigest())

    def test_three_cube_stack_checks_support_chain_after_gripper_withdraws(self):
        self.object()
        self.point(position=self.picked_top("cube-blue")["hit_position"])
        self.grasp()
        self.assertEqual(self.execute(self.parse("放到归位点"))["status"], "succeeded")
        second = self.point(name="第二层顶部", aliases=["高位点"],
                            position=self.picked_top("cube-red")["hit_position"])
        self.assertLessEqual(abs(second["position"][2] - 0.1), 0.005)
        self.store.save_object({"name": "零件B", "aliases": [], "color": "green",
            "scene_object_id": "cube-green", "position": [0.55, 0, 0.025]})
        with redirect_stdout(io.StringIO()):
            grasp = self.execute(self.parse("抓取零件B"))
        self.assertEqual(grasp["status"], "succeeded", grasp)
        result = self.parse("把零件B放到高位点")
        self.assertEqual(result["status"], "ready", result)
        job = self.execute(result)
        self.assertEqual(job["status"], "succeeded", job)
        detail = job["step_results"][0]["result"]
        self.assertEqual(detail["support_scene_object_id"], "cube-red")
        self.assertGreater(detail["position"][2], 0.12)

    def test_cube_sides_edges_self_support_and_empty_air_are_rejected_before_motion(self):
        obj = self.object()
        for i, position in enumerate(([0.575, 0.18, 0.025], [0.574, 0.18, 0.05],
                                      [0.55, -0.18, 0.05], [0.55, 0.18, 0.1])):
            point = self.point(name=f"无效支撑点{i}", aliases=[], position=position)
            task = self.store.save_task({"name": f"无效堆叠{i}", "steps": [
                {"action": "GRASP", "object_id": obj["id"]},
                {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
            version = self.simulation.snapshot()["state_version"]
            with self.assertRaises(RequestError):
                self.app.dispatch("POST", "/api/v1/jobs", {"source": "task_builder", "action": "RUN_TASK", "task_id": task["id"]})
            self.assertIsNone(self.simulation.active_job)
            self.assertEqual(self.simulation.snapshot()["state_version"], version)

    def test_support_moved_during_task_is_rechecked_in_physics_without_releasing(self):
        obj = self.object()
        point = self.point(position=self.picked_top("cube-blue")["hit_position"])
        task = self.store.save_task({"name": "支撑移动检查", "steps": [
            {"action": "GRASP", "object_id": obj["id"]},
            {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})

        def move_support(controller, target, candidates, surface):
            # 在仿真线程中改变测试夹具，模拟预检查后支撑物移走。
            p.resetBasePositionAndOrientation(self.simulation.bodies["blue"], [0.4, 0.3, 0.025], [0, 0, 0, 1])
            controller.wait(0.5)
            return place_cube(controller, target, candidates, surface)

        with patch("backend.simulation.place_cube", move_support), redirect_stdout(io.StringIO()):
            job = self.execute(self.parse("执行支撑移动检查"))
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("支撑", job["message"])
        self.assertEqual(job["result"]["held_object_id"], obj["id"])
        self.assertTrue(self.simulation.snapshot()["requires_reset"])

    def test_task_cannot_move_support_away_then_place_at_its_old_top(self):
        red = self.object()
        blue = self.store.save_object({"name": "零件B", "aliases": [], "color": "blue",
            "scene_object_id": "cube-blue", "position": [0.55, 0.18, 0.025]})
        top = self.point(position=self.picked_top("cube-blue")["hit_position"])
        ground = self.point(name="移走位置", aliases=[], position=[0.4, 0.3, 0])
        task = self.store.save_task({"name": "无支撑任务", "steps": [
            {"action": "GRASP", "object_id": blue["id"]},
            {"action": "PLACE", "object_id": blue["id"], "placement_point_id": ground["id"]},
            {"action": "GRASP", "object_id": red["id"]},
            {"action": "PLACE", "object_id": red["id"], "placement_point_id": top["id"]}]})
        with self.assertRaises(RequestError):
            self.app.dispatch("POST", "/api/v1/jobs", {"source": "task_builder", "action": "RUN_TASK", "task_id": task["id"]})
        self.assertIsNone(self.simulation.active_job)

    def test_destination_changes_and_client_tampering_invalidate_place_requests(self):
        self.object()
        point = self.point()
        self.grasp()
        result = self.parse("把零件A放到放置区A")
        for extra in ({"placement_point_id": point["id"] + 1}, {"placement_position": [0.55, 0.18, 0]}):
            with self.assertRaises(RequestError):
                self.app.instructions.execute({"parse_id": result["parse_id"], **extra})
        self.store.save_placement_point({**point, "position": [0.4, 0.2, 0]}, point["id"])
        with self.assertRaises(RequestError):
            self.execute(result)
        self.assertIsNone(self.simulation.active_job)

    def test_unknown_ambiguous_negative_and_wrong_object_place_requests_do_not_execute(self):
        self.object()
        self.point()
        self.store.save_object({"name": "零件B", "aliases": [], "color": "blue", "scene_object_id": "cube-blue",
                                "position": [0.55, 0.18, 0.025]})
        self.point(name="放置区B", aliases=[], position=[0.4, 0.2, 0])
        self.grasp()
        for text in ("把零件A放到未知位置", "把未知物体放到放置区A", "帮我将未登记物体放在归位点",
                     "不要把零件A放到放置区A",
                     "把零件B放到放置区A", "把零件A放到放置区A和放置区B", "把零件A放到大放置区A"):
            self.assertNotEqual(self.parse(text)["status"], "ready", text)
        self.assertIsNone(self.simulation.active_job)

    def test_occupied_point_fails_in_physics_without_selecting_another_position(self):
        self.object()
        point = self.point(position=[0.55, 0.18, 0])
        self.grasp()
        result = self.parse("把零件A放到放置区A")
        self.assertEqual(result["status"], "ready", result)
        job = self.execute(result)
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("占用", job["message"])
        self.assertEqual(job["steps"][0]["placement_position"], point["position"])
        self.assertTrue(self.simulation.snapshot()["requires_reset"])

    def test_task_can_place_after_a_low_move_without_mistaking_held_cube_for_an_obstacle(self):
        obj = self.object()
        point = self.point()
        task = self.store.save_task({"name": "低位放置", "aliases": [], "steps": [
            {"action": "GRASP", "object_id": obj["id"]},
            {"action": "MOVE", "position": [0.45, 0.15, 0.030]},
            {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
        with redirect_stdout(io.StringIO()):
            job = self.execute(self.parse("执行低位放置"))
        self.assertEqual(job["status"], "succeeded", job)
        self.assertTrue(job["step_results"][-1]["result"]["supported"])

    def test_unsupported_surface_or_outside_workspace_is_rejected_before_grasp(self):
        obj = self.object()
        for position in ([0.45, 0.15, 0.05], [2, 0.15, 0]):
            point = self.point(name=f"不支持的点{position[0]}-{position[2]}", aliases=[], position=position)
            task = self.store.save_task({"name": point["name"] + "任务", "aliases": [], "steps": [
                {"action": "GRASP", "object_id": obj["id"]},
                {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
            version = self.simulation.snapshot()["state_version"]
            with self.assertRaises(RequestError):
                self.app.dispatch("POST", "/api/v1/jobs", {"source": "task_builder", "action": "RUN_TASK", "task_id": task["id"]})
            self.assertIsNone(self.simulation.active_job)
            self.assertEqual(self.simulation.snapshot()["state_version"], version)


if __name__ == "__main__":
    unittest.main()
