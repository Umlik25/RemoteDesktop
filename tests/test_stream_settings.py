import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from core.host_server import HostServer, Session, encode_jpeg


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
