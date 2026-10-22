"""在真实 PyBullet 中检查网页拾取、编辑坐标试抓和 HTTP 协议。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
import sqlite3
import struct
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import RequestError
from backend.server import StudioApplication, StudioHandler
from backend.simulation import SimulationService
from backend.store import StudioStore


class MemoryConnection:
    """把标准 HTTP Handler 的收发接到内存，避免测试依赖监听端口权限。"""

    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.response = bytearray()

    def makefile(self, mode, *args):
        return self.request

    def sendall(self, value):
        self.response.extend(value)

    def settimeout(self, value):
        pass


class WebStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.simulation = SimulationService(real_time=False, width=640, height=400, fps=4)
        cls.simulation.start()

    @classmethod
    def tearDownClass(cls):
        cls.simulation.close()

    def setUp(self):
        self.simulation.control("reset")
        self.store = StudioStore(":memory:")
        self.app = StudioApplication(self.simulation, self.store)

    def tearDown(self):
        self.store.close()

    def wait_job(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.simulation.get_job(job["id"])
            if job["status"] in ("succeeded", "failed", "cancelled"):
                return job
            time.sleep(0.01)
        self.fail("仿真测试未在规定时间内完成")

    def pick(self, color):
        scene = self.simulation.snapshot()
        obj = next(obj for obj in scene["objects"] if obj["color"] == color)
        return self.simulation.pick({"frame_id": scene["frame_id"],
                                     "x": obj["screen_position"][0], "y": obj["screen_position"][1],
                                     "scene_object_id": obj["scene_object_id"]})

    def register(self, color, name="测试零件", aliases=None):
        pick = self.pick(color)
        return self.store.save_object({"scene_object_id": pick["scene_object_id"],
                                       "color": color, "name": name, "aliases": aliases or [],
                                       "position": pick["grasp_position"], "skill_ids": ["cube_grasp"]})

    def request(self, method, path, body=None):
        payload = json.dumps(body).encode() if body is not None else b""
        request = f"{method} {path} HTTP/1.0\r\nHost: localhost\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload
        connection = MemoryConnection(request)
        with redirect_stderr(io.StringIO()):
            StudioHandler(connection, ("127.0.0.1", 0), SimpleNamespace(), application=self.app)
        headers, data = bytes(connection.response).split(b"\r\n\r\n", 1)
        status = int(headers.split()[1])
        return status, headers, data

    def test_native_frame_and_http_bootstrap(self):
        status, _, data = self.request("GET", "/studio-config.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["mode"], "backend")
        status, _, data = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertIn("coordinate_grasp", json.loads(data)["capabilities"])
        status, _, data = self.request("GET", "/api/v1/simulation/scene")
        self.assertEqual(status, 200)
        scene = json.loads(data)
        self.assertTrue(all("position" not in obj and "name" not in obj for obj in scene["objects"]))
        status, headers, image = self.request("GET", scene["image_url"])
        self.assertEqual(status, 200)
        self.assertIn(b"image/png", headers)
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_library_starts_empty_and_only_explicit_save_adds_an_object(self):
        status, _, data = self.request("GET", "/api/v1/objects")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), [])
        pick = self.pick("red")
        self.assertEqual(self.store.objects(), [])
        self.simulation.control("reset")
        self.assertEqual(self.store.objects(), [])
        payload = {"scene_object_id": pick["scene_object_id"], "color": "red",
                   "name": "客户自定名称", "aliases": ["客户别名"], "position": pick["grasp_position"]}
        status, _, data = self.request("POST", "/api/v1/objects", payload)
        self.assertEqual(status, 201)
        obj = json.loads(data)
        self.assertEqual(obj["name"], payload["name"])
        self.assertEqual(obj["position"], payload["position"])
        self.assertEqual(len(self.store.objects()), 1)
        self.simulation.control("reset")
        self.assertEqual(self.store.object(obj["id"])["name"], payload["name"])

    def test_all_three_depth_picks_physically_lift_the_selected_cube(self):
        self.assertEqual(self.store.objects(), [])
        for color in ("red", "green", "blue"):
            with self.subTest(color=color):
                self.simulation.control("reset")
                pick = self.pick(color)
                self.assertEqual(pick["position_source"], "depth_surface_offset")
                self.assertAlmostEqual(math.dist(pick["hit_position"], pick["grasp_position"]), 0.025)
                before = self.simulation.snapshot()
                with redirect_stdout(io.StringIO()):
                    job = self.simulation.submit_grasp(pick["scene_object_id"], pick["grasp_position"], pick["frame_id"])
                    job = self.wait_job(job)
                self.assertEqual(job["status"], "succeeded", job["message"])
                self.assertEqual(job["result"]["requested_position"], pick["grasp_position"])
                self.assertLess(job["result"]["coordinate_error_m"], 0.01)
                self.assertGreaterEqual(job["result"]["minimum_lift_m"], 0.15)
                self.assertEqual(job["result"]["held_seconds"], 3)
                self.assertTrue(job["result"]["bilateral_contact"])
                after = self.simulation.snapshot()
                self.assertNotEqual(before["frame_id"], after["frame_id"])
                self.assertTrue(after["requires_reset"])
                self.assertEqual(self.store.objects(), [])

    def test_edited_bad_coordinate_really_fails_instead_of_using_true_center(self):
        pick = self.pick("red")
        bad_position = list(pick["grasp_position"])
        bad_position[1] = round(bad_position[1] + 0.1, 3)
        red = self.register("red")
        with redirect_stdout(io.StringIO()):
            response = self.app.dispatch("POST", "/api/v1/jobs", {
                "action": "GRASP", "object_id": red["id"], "scene_object_id": pick["scene_object_id"],
                "frame_id": pick["frame_id"], "source": "coordinate_test", "grasp_position": bad_position,
            })
            job = self.wait_job(response.payload)
        self.assertEqual(response.status, 202)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["requested_position"], bad_position)
        self.assertGreater(job["result"]["coordinate_error_m"], 0.09)
        self.assertFalse(job["result"]["success"])
        self.assertIsNone(job["result"]["held_seconds"])

    def test_coordinate_request_is_not_replaced_by_saved_database_position(self):
        pick = self.pick("red")
        red = self.register("red")
        red["position"][1] += 0.1
        self.store.save_object(red, red["id"])
        with redirect_stdout(io.StringIO()):
            response = self.app.dispatch("POST", "/api/v1/jobs", {
                "action": "GRASP", "object_id": red["id"], "source": "coordinate_test",
                "grasp_position": pick["grasp_position"],
            })
            job = self.wait_job(response.payload)
        self.assertEqual(job["status"], "succeeded", job["message"])
        self.assertEqual(job["requested_position"], pick["grasp_position"])

    def test_invalid_pick_and_coordinate_do_not_start_a_job(self):
        scene = self.simulation.snapshot()
        status, _, _ = self.request("POST", "/api/v1/simulation/pick", {"frame_id": scene["frame_id"], "x": 1, "y": 0})
        self.assertEqual(status, 422)
        for position in ([0.55, 0, float("nan")], [0.55, 0], [2, 0, 0], [True, 0, 0]):
            with self.assertRaises(RequestError):
                self.simulation.submit_grasp("cube-red", position)
        status, _, _ = self.request("POST", "/api/v1/jobs", {"action": "GRASP", "source": "coordinate_test", "scene_object_id": "cube-red"})
        self.assertEqual(status, 422)
        self.assertIsNone(self.simulation.active_job)

    def test_any_pixel_can_be_sampled_without_registering_or_selecting_a_nearby_cube(self):
        scene = self.simulation.snapshot()
        for x, y in ((0, 0), (0.99, 0), (0.1, 0.8), (0.99, 0.99), (0.5, 0.5)):
            with self.subTest(x=x, y=y):
                status, _, data = self.request("POST", "/api/v1/simulation/pick", {"frame_id": scene["frame_id"], "x": x, "y": y})
                self.assertEqual(status, 200)
                pick = json.loads(data)
                self.assertTrue(all(math.isfinite(value) for value in pick["grasp_position"]))
                self.assertTrue(all(value == round(value, 3) for value in pick["grasp_position"]))
                self.assertEqual(pick["screen_position"], [x, y])
                self.assertIsNone(pick["scene_object_id"])
        self.assertEqual(self.store.objects(), [])

    def test_named_arbitrary_point_is_persisted_with_three_decimals_and_can_be_edited(self):
        status, _, data = self.request("POST", "/api/v1/objects", {
            "name": "用户点位", "color": "point", "aliases": [],
            "position": [0.5512345, -0.123678, 0.025123], "scene_object_id": None,
        })
        self.assertEqual(status, 201)
        obj = json.loads(data)
        self.assertTrue(obj["scene_object_id"].startswith("point-"))
        self.assertEqual(obj["position"], [0.551, -0.124, 0.025])
        status, _, data = self.request("PUT", f'/api/v1/objects/{obj["id"]}', {**obj, "position": [1.23456, 2.12345, 0]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["position"], [1.235, 2.123, 0])

    def test_arbitrary_coordinate_grasp_checks_actual_contact_and_does_not_snap_to_a_cube(self):
        pick = self.pick("red")
        with redirect_stdout(io.StringIO()):
            response = self.app.dispatch("POST", "/api/v1/jobs", {
                "action": "GRASP", "source": "coordinate_test", "scene_object_id": None,
                "grasp_position": pick["grasp_position"], "frame_id": pick["frame_id"],
            })
            job = self.wait_job(response.payload)
        self.assertEqual(job["status"], "succeeded", job["message"])
        self.assertEqual(job["result"]["detected_scene_object_id"], "cube-red")
        self.simulation.control("reset")
        position = [0.55, -0.08, 0.025]
        point = self.store.save_object({"name": "空处点位", "color": "point", "scene_object_id": None,
                                        "aliases": [], "position": position})
        with redirect_stdout(io.StringIO()):
            response = self.app.dispatch("POST", "/api/v1/jobs", {
                "action": "GRASP", "source": "coordinate_test", "object_id": point["id"],
                "scene_object_id": point["scene_object_id"], "grasp_position": position,
            })
            job = self.wait_job(response.payload)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["requested_position"], position)
        self.assertIsNone(job["result"]["detected_scene_object_id"])
        self.assertIsNone(job["result"]["coordinate_error_m"])

    def test_binary_camera_stream_contains_paired_metadata_and_native_png(self):
        packets = self.simulation.stream_packets()
        try:
            packet = next(packets)
            old_length = struct.unpack(">I", packet[:4])[0]
            old_metadata = json.loads(packet[4:4 + old_length])
            self.simulation.control("reset")
            next_packet = next(packets)
            new_length = struct.unpack(">I", next_packet[:4])[0]
            new_metadata = json.loads(next_packet[4:4 + new_length])
            self.assertNotEqual(old_metadata["scene_id"], new_metadata["scene_id"])
        finally:
            packets.close()
        with patch.object(self.simulation, "stream_packets", return_value=iter([next_packet])):
            status, headers, data = self.request("GET", "/api/v1/simulation/stream")
        self.assertEqual(status, 200)
        self.assertIn(b"application/x-panda-frames", headers)
        length = struct.unpack(">I", data[:4])[0]
        metadata = json.loads(data[4:4 + length])
        image = data[4 + length:]
        self.assertEqual(len(image), metadata["image_size"])
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_stop_cancels_running_job_and_reset_invalidates_old_frame(self):
        pick = self.pick("red")
        with redirect_stdout(io.StringIO()):
            job = self.simulation.submit_grasp(pick["scene_object_id"], pick["grasp_position"])
            with self.assertRaises(RequestError):
                self.simulation.submit_grasp(pick["scene_object_id"], pick["grasp_position"])
            result = self.simulation.control("stop")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.simulation.get_job(job["id"])["status"], "cancelled")
        self.simulation.control("reset")
        with self.assertRaises(RequestError):
            self.simulation.submit_grasp(pick["scene_object_id"], pick["grasp_position"], pick["frame_id"])
        self.assertFalse(self.simulation.snapshot()["requires_reset"])

    def test_names_aliases_and_edited_coordinates_persist_in_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "studio.sqlite3"
            store = StudioStore(path)
            self.assertEqual(store.objects(), [])
            red = store.save_object({"scene_object_id": "cube-red", "color": "red",
                                     "name": "客户零件", "aliases": ["零件 A"],
                                     "position": [0.55, -0.17, 0.026]})
            store.close()
            reopened = StudioStore(path)
            try:
                restored = reopened.object(red["id"])
                self.assertEqual(restored["name"], "客户零件")
                self.assertEqual(restored["aliases"], ["零件 A"])
                self.assertEqual(restored["position"], red["position"])
                green = {"scene_object_id": "cube-green", "color": "green", "name": "另一个零件",
                         "aliases": ["零件 A"], "position": [0.55, 0, 0.025]}
                with self.assertRaises(RequestError):
                    reopened.save_object(green)
            finally:
                reopened.close()

    def test_empty_database_stays_empty_when_reopened(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite3"
            store = StudioStore(path)
            obj = store.save_object({"scene_object_id": "cube-red", "color": "red", "name": "手动登记",
                                     "aliases": [], "position": [0.55, -0.18, 0.025]})
            store.delete_object(obj["id"])
            store.close()
            reopened = StudioStore(path)
            try:
                self.assertEqual(reopened.objects(), [])
            finally:
                reopened.close()

    def test_clear_memory_removes_referenced_records_and_running_job_over_http(self):
        obj = self.register("red", aliases=["清除测试别名"])
        point = self.store.save_placement_point({"name": "测试放置点", "aliases": ["终点"],
                                                 "position": [0.4, 0.2, 0]})
        task = self.store.save_task({"name": "测试任务", "aliases": ["抓放"], "steps": [
            {"action": "WAIT", "seconds": 30}, {"action": "GRASP", "object_id": obj["id"]},
            {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
        before = self.simulation.snapshot()
        job = self.app.dispatch("POST", "/api/v1/jobs", {"source": "task_builder",
            "action": "RUN_TASK", "task_id": task["id"]}).payload
        job_count = len(self.simulation.jobs)
        status, _, data = self.request("POST", "/api/v1/memory/clear", {})
        result = json.loads(data)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "cleared")
        self.assertEqual(result["cleared"], {"objects": 1, "placement_points": 1, "tasks": 1, "jobs": job_count, "parses": 0})
        self.assertEqual(self.store.snapshot(), {"objects": [], "tasks": [], "placement_points": []})
        after = self.simulation.snapshot()
        self.assertNotEqual(before["scene_id"], after["scene_id"])
        self.assertFalse(after["is_running"])
        self.assertFalse(after["requires_reset"])
        self.assertIsNone(after["held_object_id"])
        self.assertEqual(len(after["objects"]), 3)
        self.assertEqual(self.request("GET", f'/api/v1/jobs/{job["id"]}')[0], 404)
        with self.assertRaises(RequestError):
            self.simulation.frame(before["frame_id"])
        self.assertEqual(self.app.dispatch("POST", "/api/v1/memory/clear", {}).payload["cleared"]["objects"], 0)

    def test_clear_memory_persists_and_does_not_reuse_deleted_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite3"
            store = StudioStore(path)
            obj = store.save_object({"name": "零件", "color": "red", "scene_object_id": "cube-red",
                                     "position": [0.55, -0.18, 0.025], "aliases": ["别名"]})
            point = store.save_placement_point({"name": "终点", "position": [0.4, 0.2, 0]})
            task = store.save_task({"name": "抓放", "steps": [
                {"action": "GRASP", "object_id": obj["id"]},
                {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
            store.clear_memory()
            store.close()
            reopened = StudioStore(path)
            try:
                self.assertEqual(reopened.snapshot(), {"objects": [], "tasks": [], "placement_points": []})
                new_obj = reopened.save_object(obj)
                new_point = reopened.save_placement_point(point)
                new_task = reopened.save_task({**task, "steps": [{"action": "GRASP", "object_id": new_obj["id"]}]})
                for old, new in ((obj, new_obj), (point, new_point), (task, new_task)):
                    self.assertGreater(new["id"], old["id"])
            finally:
                reopened.close()

    def test_clear_memory_rolls_back_all_tables_if_deletion_fails(self):
        obj = self.register("red")
        point = self.store.save_placement_point({"name": "终点", "position": [0.4, 0.2, 0]})
        self.store.save_task({"name": "抓放", "steps": [
            {"action": "GRASP", "object_id": obj["id"]},
            {"action": "PLACE", "object_id": obj["id"], "placement_point_id": point["id"]}]})
        before = self.store.snapshot()
        self.store.connection.execute("CREATE TRIGGER fail_clear BEFORE DELETE ON objects "
                                      "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.clear_memory()
        self.assertEqual(self.store.snapshot(), before)

    def test_clear_memory_does_not_delete_data_if_simulation_reset_fails(self):
        self.register("red")
        before = self.store.snapshot()
        with patch.object(self.simulation, "control", side_effect=RequestError("重置失败", 503)):
            status, _, data = self.request("POST", "/api/v1/memory/clear", {})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(data)["message"], "重置失败")
        self.assertEqual(self.store.snapshot(), before)

    def test_language_models_and_unverified_execution_are_rejected(self):
        status, _, _ = self.request("POST", "/api/v1/instructions/parse", {"instruction": "抓取红色方块", "model": "qwen"})
        self.assertEqual(status, 422)
        status, _, _ = self.request("POST", "/api/v1/jobs", {"action": "RUN_TASK", "task_id": 1})
        self.assertEqual(status, 422)
        status, _, _ = self.request("POST", "/api/v1/jobs", {"action": "GRASP", "source": "language"})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
