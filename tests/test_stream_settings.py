import unittest
import time
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from core.host_server import HostServer, InputInjector, Session, encode_jpeg
from core.display import DisplayManager, best_mode


class StreamSettingsTests(unittest.TestCase):
    def make_server(self, user, *, work_only=False):
        server = HostServer.__new__(HostServer)
        server.config = {"work_only_mode": work_only}
        server.capture = SimpleNamespace(max_fps=0)
        server._frame_evt = None
        session = Session(SimpleNamespace(), "viewer", user, "127.0.0.1", "LAN Direct")
        server.sessions = {session.sid: session}
        return server, session

    def test_config_is_clamped_acknowledged_and_wakes_capture(self):
        user = {"role": "user", "profile": "dev", "max_fps": 60,
                "priority": "normal"}
        server, session = self.make_server(user)

        response = server._apply_stream_config(session, {
            "request_id": 7, "fps": 144, "quality": 999, "scale": 0.1,
            "profile": "custom", "adaptive": False,
        }, user)

        self.assertEqual("config_applied", response["type"])
        self.assertEqual(7, response["request_id"])
        self.assertEqual({"fps": 60, "quality": 95, "scale": 0.25,
                          "profile": "custom", "adaptive": False},
                         response["selected"])
        self.assertEqual({"fps": 60, "quality": 95, "scale": 0.25},
                         response["applied"])
        self.assertEqual(60, response["limits"]["max_fps"])
        self.assertTrue(any("60 FPS" in reason for reason in response["reasons"]))
        self.assertTrue(session.force_full)
        self.assertEqual(60, server.capture.max_fps)

    def test_fixed_mode_disables_network_adaptation_only(self):
        user = {"role": "owner", "profile": "custom", "max_fps": 60,
                "priority": "critical"}
        _, session = self.make_server(user)
        session.fps, session.quality, session.scale = 120, 80, 1.0
        session.net_degrade = 3
        session.adaptive = False

        self.assertEqual((120, 80, 1.0), session.effective())
        session.degrade = 1
        self.assertEqual((120, 60, 1.0), session.effective())

    def test_work_only_limit_is_visible_in_applied_state(self):
        user = {"role": "user", "profile": "design", "max_fps": 120,
                "priority": "normal"}
        server, session = self.make_server(user, work_only=True)

        response = server._apply_stream_config(
            session, {"fps": 90, "quality": 85, "scale": 1}, user)

        self.assertEqual(90, response["selected"]["fps"])
        self.assertEqual(30, response["applied"]["fps"])
        self.assertEqual(60, response["applied"]["quality"])
        self.assertTrue(any("только работа" in reason for reason in response["reasons"]))

    def test_encoder_uses_requested_resolution_scale(self):
        width, height = 192, 144
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                pixels.extend((x * 7 % 256, y * 9 % 256, (x * y) % 256, 255))
        frame = (bytes(pixels), width, height, 0.0, 1)

        low_quality, x, y, stream_w, stream_h = encode_jpeg(frame, 40, 0.625)
        high_quality, *_ = encode_jpeg(frame, 95, 0.625)

        self.assertEqual((0, 0, 120, 90), (x, y, stream_w, stream_h))
        self.assertGreater(len(high_quality), len(low_quality))
        with Image.open(__import__("io").BytesIO(high_quality)) as image:
            self.assertEqual((120, 90), image.size)

    def test_display_mode_prefers_client_aspect_ratio(self):
        modes = [
            {"width": 2560, "height": 1440, "refresh": 144},
            {"width": 1920, "height": 1200, "refresh": 60},
            {"width": 1920, "height": 1080, "refresh": 144},
        ]

        chosen = best_mode(modes, 3456, 2234)

        self.assertEqual((1920, 1200), (chosen["width"], chosen["height"]))

    def test_display_manager_is_safely_disabled_off_windows(self):
        with mock.patch("core.display.IS_WIN", False):
            manager = DisplayManager()
        self.assertFalse(manager.available)

    def test_set_best_never_exceeds_original_monitor_mode(self):
        # Монитор по D-SUB не синхронизирует режимы крупнее исходного, даже если
        # GPU их отдаёт — set_best обязан оставаться в пределах исходного режима.
        mgr = DisplayManager.__new__(DisplayManager)
        mgr._lock = __import__("threading").RLock()
        mgr._mode_cache = [
            {"width": 1920, "height": 1080, "refresh": 60, "bpp": 32},
            {"width": 1280, "height": 720, "refresh": 60, "bpp": 32},
            {"width": 1366, "height": 768, "refresh": 60, "bpp": 32},
        ]
        mgr._original = {"width": 1366, "height": 768, "refresh": 60, "bpp": 32}
        requested = {}

        def fake_set_mode(width, height, refresh=0):
            requested["wh"] = (width, height)
            return True, None

        mgr.set_mode = fake_set_mode
        ok, chosen, error = mgr.set_best(3456, 2234)   # огромный Retina-ноутбук

        self.assertTrue(ok, error)
        self.assertLessEqual(chosen["width"], 1366)
        self.assertLessEqual(chosen["height"], 768)
        self.assertLessEqual(requested["wh"][0], 1366)
        self.assertLessEqual(requested["wh"][1], 768)

    def test_set_mode_rejects_modes_larger_than_original(self):
        mgr = DisplayManager.__new__(DisplayManager)
        mgr._lock = __import__("threading").RLock()
        mgr._original = {"width": 1366, "height": 768, "refresh": 60, "bpp": 32}
        with mock.patch("core.display.IS_WIN", True):
            ok, error = mgr.set_mode(1920, 1080, 60)
        self.assertFalse(ok)
        self.assertIn("превышает", error)

    def test_frame_ack_tracks_browser_backlog_and_latency(self):
        user = {"role": "owner", "profile": "dev", "max_fps": 60}
        server, session = self.make_server(user)
        session.sent_times.extend([(8, time.monotonic() - 0.04),
                                   (9, time.monotonic() - 0.02)])

        server._apply_frame_ack(session, {
            "id": 9, "queue": 2, "decode_ms": 3.5,
        })

        self.assertEqual(9, session.last_ack_id)
        self.assertEqual(2, session.client_queue)
        self.assertEqual(3.5, session.client_decode_ms)
        self.assertGreater(session.ack_latency_ms, 0)
        self.assertEqual([], list(session.sent_times))

    def test_client_display_size_distinguishes_screen_and_window(self):
        server, _ = self.make_server({"role": "owner", "max_fps": 60})
        data = {
            "client_width": 100,
            "client_height": 100,
            "client_display": {
                "screen": {"width": 3456, "height": 2234},
                "viewport": {"width": 3024, "height": 1712},
            },
        }

        self.assertEqual((3456, 2234), server._client_display_size(data, "client"))
        self.assertEqual((3024, 1712), server._client_display_size(data, "viewport"))

    def test_game_mouse_delta_moves_exact_distance_in_fallback(self):
        injector = InputInjector.__new__(InputInjector)
        injector.mouse = SimpleNamespace(position=(100, 200))
        injector.screen_wh = (1920, 1080)

        with mock.patch("core.host_server._SENDINPUT_OK", False):
            injector._mouse_move_relative(37, -19)

        self.assertEqual((137, 181), injector.mouse.position)


class DisplayConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_session_changes_and_restores_shared_display(self):
        server = HostServer.__new__(HostServer)
        user = {"role": "user", "profile": "dev", "max_fps": 60,
                "allow_display": True}
        session = Session(SimpleNamespace(), "viewer", user, "127.0.0.1", "LAN Direct")
        manager = SimpleNamespace(
            available=True,
            current=lambda: {"width": 2560, "height": 1440, "refresh": 144},
            modes=lambda: [{"width": 1920, "height": 1200, "refresh": 60}],
            set_best=mock.Mock(return_value=(
                True, {"width": 1920, "height": 1200, "refresh": 60}, None)),
            restore=mock.Mock(return_value=True),
        )
        server.display = manager
        server.capture = SimpleNamespace(restart=mock.Mock())
        server.sessions = {session.sid: session}
        server._display_owner = None

        state, reasons = await server._apply_display_config(session, {
            "desktop_mode": "client",
            "client_display": {"screen": {"width": 3456, "height": 2234}},
        }, user)

        self.assertEqual("client", state["mode"])
        self.assertEqual(session.sid, server._display_owner)
        self.assertTrue(any("1920×1200" in reason for reason in reasons))
        manager.set_best.assert_called_once_with(3456, 2234)
        server.capture.restart.assert_called_once()

        await server._apply_display_config(session, {
            "desktop_mode": "client", "client_width": 3456, "client_height": 2234,
        }, user)
        manager.set_best.assert_called_once_with(3456, 2234)
        server.capture.restart.assert_called_once()

        await server._release_display(session)
        manager.restore.assert_called_once()
        self.assertIsNone(server._display_owner)


class StreamCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_precise_scales_do_not_share_encoded_cache(self):
        server = HostServer.__new__(HostServer)
        server._enc_jobs = {}
        calls = []

        def fake_encode(frame, quality, scale, region):
            calls.append(scale)
            return b"jpeg", 0, 0, 100, 100

        frame = (b"", 100, 100, 0.0, 4)
        with mock.patch("core.host_server.encode_jpeg", side_effect=fake_encode):
            await server._encoded(frame, 70, 0.625, None)
            await server._encoded(frame, 70, 0.63, None)

        self.assertEqual([0.625, 0.63], calls)


if __name__ == "__main__":
    unittest.main()
