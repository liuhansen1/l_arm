"""启动顺序和浏览器阻塞检查；不打开真实浏览器或监听端口。"""

from contextlib import redirect_stdout
import io
import threading
import unittest
from unittest.mock import Mock, patch

from backend import RequestError
from backend.server import main, open_browser


class WebStartupTests(unittest.TestCase):
    def setUp(self):
        self.simulation, self.store, self.application, self.server = (Mock() for _ in range(4))
        for name, value in (("SimulationService", self.simulation), ("StudioStore", self.store),
                            ("StudioApplication", self.application)):
            self.enterContext(patch(f"backend.server.{name}", return_value=value))
        self.server_factory = self.enterContext(patch("backend.server.ThreadingHTTPServer", return_value=self.server))
        self.browser = self.enterContext(patch("backend.server.webbrowser.open", return_value=True))
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))

    def test_model_loads_before_continuous_rendering_and_no_browser_option_is_respected(self):
        events = []
        self.application.instructions.load.side_effect = lambda: events.append("model")
        self.simulation.start.side_effect = lambda: events.append("simulation")
        self.assertEqual(main(["--no-browser", "--database", ":memory:"]), 0)
        self.assertEqual(events, ["model", "simulation"])
        self.server.serve_forever.assert_called_once()
        self.browser.assert_not_called()
        self.simulation.close.assert_called_once()
        self.server.server_close.assert_called_once()
        self.store.close.assert_called_once()
        self.assertIn("后端准备完成", self.output.getvalue())

    def test_slow_browser_does_not_delay_http_serving(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked_browser(url):
            entered.set()
            try:
                return release.wait(5)
            finally:
                finished.set()

        def serve():
            self.assertTrue(entered.wait(2), "浏览器线程未启动")
            self.assertFalse(finished.is_set(), "HTTP 等到了浏览器启动结束才开始服务")

        self.browser.side_effect = blocked_browser
        self.server.serve_forever.side_effect = serve
        try:
            self.assertEqual(main(["--database", ":memory:"]), 0)
            self.server.serve_forever.assert_called_once()
            self.browser.assert_called_once_with("http://127.0.0.1:8080")
        finally:
            release.set()
            if entered.is_set():
                self.assertTrue(finished.wait(2))

    def test_model_failure_closes_resources_without_starting_simulation_or_browser(self):
        self.application.instructions.load.side_effect = RequestError("模型加载失败", 503)
        self.assertEqual(main(["--database", ":memory:"]), 1)
        self.simulation.start.assert_not_called()
        self.simulation.close.assert_called_once()
        self.store.close.assert_called_once()
        self.server_factory.assert_not_called()
        self.browser.assert_not_called()
        self.assertIn("启动失败", self.output.getvalue())

    def test_browser_failure_reports_manual_url(self):
        url = "http://127.0.0.1:8080"
        self.browser.return_value = False
        open_browser(url)
        self.assertIn(f"请手动访问：{url}", self.output.getvalue())
        self.output.seek(0)
        self.output.truncate()
        self.browser.side_effect = OSError("浏览器启动失败")
        open_browser(url)
        self.assertIn("浏览器启动失败", self.output.getvalue())
        self.assertIn(f"请手动访问：{url}", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
