import json
import os
import tempfile
import time
import unittest
import asyncio
import collections
from types import SimpleNamespace
from unittest import mock

from aiohttp import FormData, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from core import auth, state
from core.host_server import HostServer


class AuthSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = self.temp_dir.name
        self.patches = [
            mock.patch.object(state, "DATA_DIR", root),
            mock.patch.object(state, "CONFIG_PATH", os.path.join(root, "config.json")),
            mock.patch.object(state, "AUDIT_PATH", os.path.join(root, "audit.log")),
            mock.patch.object(state, "SESSIONS_LOG_PATH", os.path.join(root, "sessions.log")),
            mock.patch.object(auth, "USERS_PATH", os.path.join(root, "users.json")),
            mock.patch.object(auth, "INVITES_PATH", os.path.join(root, "invites.json")),
            mock.patch.object(auth, "RESET_PATH", os.path.join(root, "reset_codes.json")),
        ]
        for patcher in self.patches:
            patcher.start()
        auth._tokens.clear()

    def tearDown(self):
        auth._tokens.clear()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_rejects_unsafe_username_and_weak_password(self):
        with self.assertRaisesRegex(ValueError, "Имя"):
            auth.create_user("<script>", "long-enough-password")
        with self.assertRaisesRegex(ValueError, "8"):
            auth.create_user("valid-user", "short")

    def test_expired_user_token_is_revoked(self):
        now = time.time()
        auth.create_user("guest", "long-enough-password", expires=now + 10)
        token = auth.issue_token("guest")

        with mock.patch("core.auth.time.time", return_value=now + 20):
            self.assertEqual((None, None), auth.check_token(token))
        self.assertNotIn(token, auth._tokens)

    def test_failed_invite_redemption_does_not_consume_code(self):
        auth.create_user("taken", "long-enough-password")
        code = auth.create_invite()

        with self.assertRaisesRegex(ValueError, "существует"):
            auth.redeem_invite(code, "taken", "another-long-password")

        invite = next(item for item in auth.list_invites() if item["code"] == code)
        self.assertFalse(invite["used"])

    def test_display_permission_survives_invite_redemption(self):
        code = auth.create_invite(allow_display=True)

        auth.redeem_invite(code, "display-user", "long-enough-password")

        self.assertTrue(auth.get_user("display-user")["allow_display"])
        listed = next(item for item in auth.list_users()
                      if item["username"] == "display-user")
        self.assertTrue(listed["allow_display"])

    def test_failed_password_reset_does_not_consume_code(self):
        auth.create_user("user", "long-enough-password")
        code = auth.create_reset_code("user")

        with self.assertRaisesRegex(ValueError, "8"):
            auth.redeem_reset_code(code, "short")

        with open(auth.RESET_PATH, "r", encoding="utf-8") as file:
            self.assertFalse(json.load(file)[code]["used"])

    def test_local_admin_panel_still_requires_owner_token(self):
        server = HostServer.__new__(HostServer)
        request = SimpleNamespace(headers={}, query={}, remote="127.0.0.1")
        self.assertIsNone(server._admin(request))

        auth.create_user("owner", "long-enough-password", role="owner")
        token = auth.issue_token("owner")
        request.query = {"token": token}
        self.assertEqual("owner", server._admin(request))

    def test_only_same_origin_and_local_client_origin_are_allowed(self):
        server = HostServer.__new__(HostServer)
        server.config = {"client_port": 8600}
        request = SimpleNamespace(scheme="http", host="192.168.1.20:8532")

        self.assertTrue(server._origin_allowed(request, "http://192.168.1.20:8532"))
        self.assertTrue(server._origin_allowed(request, "http://127.0.0.1:8600"))
        self.assertFalse(server._origin_allowed(request, "https://example.com"))

    async def test_virtual_display_install_requires_local_admin_confirmation(self):
        server = HostServer.__new__(HostServer)
        auth.create_user("owner", "long-enough-password", role="owner")
        token = auth.issue_token("owner")
        remote_request = SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"}, query={}, remote="192.168.1.50")

        with self.assertRaises(web.HTTPForbidden):
            await server.api_display_install(remote_request)

        local_request = SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"}, query={}, remote="127.0.0.1")
        with mock.patch("core.host_server.virtual_display.launch_installer",
                        return_value=(True, None)) as launch:
            response = await server.api_display_install(local_request)

        self.assertEqual(200, response.status)
        launch.assert_called_once_with()

    async def test_admin_http_api_requires_token_and_restricts_origin(self):
        server = HostServer.__new__(HostServer)
        server.config = {"client_port": 8600}
        server.sessions = {}
        server._login_attempts = collections.defaultdict(collections.deque)
        app = server._build_app()
        app.on_startup.clear()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/users")
            self.assertEqual(403, response.status)

            allowed_origin = "http://127.0.0.1:8600"
            response = await client.options("/api/login", headers={"Origin": allowed_origin})
            self.assertEqual(200, response.status)
            self.assertEqual(allowed_origin, response.headers["Access-Control-Allow-Origin"])

            response = await client.options(
                "/api/login", headers={"Origin": "https://example.com"})
            self.assertEqual(403, response.status)

            auth.create_user("owner", "long-enough-password", role="owner")
            response = await client.post(
                "/api/login", json={"username": "owner", "password": "long-enough-password"},
                headers={"Origin": allowed_origin})
            self.assertEqual(200, response.status)
            token = (await response.json())["token"]

            response = await client.get(
                "/api/users", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(200, response.status)
        finally:
            await client.close()

    async def test_file_transfer_is_permissioned_and_isolated_per_user(self):
        server = HostServer.__new__(HostServer)
        server.config = {"client_port": 8600}
        server.sessions = {}
        server._login_attempts = collections.defaultdict(collections.deque)
        app = server._build_app()
        app.on_startup.clear()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            auth.create_user("alice", "long-enough-password", allow_files=True,
                             disk_quota_mb=16)
            auth.create_user("bob", "another-long-password", allow_files=True,
                             disk_quota_mb=16)
            auth.create_user("viewer", "viewer-long-password", allow_files=False)
            alice_token = auth.issue_token("alice")
            bob_token = auth.issue_token("bob")
            viewer_token = auth.issue_token("viewer")

            response = await client.get(
                "/api/files", headers={"Authorization": f"Bearer {viewer_token}"})
            self.assertEqual(403, response.status)

            form = FormData()
            form.add_field("file", b"private contents", filename="notes.txt",
                           content_type="text/plain")
            response = await client.post(
                "/api/files/upload", data=form,
                headers={"Authorization": f"Bearer {alice_token}"})
            self.assertEqual(200, response.status)

            response = await client.get(
                "/api/files", headers={"Authorization": f"Bearer {alice_token}"})
            self.assertEqual(["notes.txt"], [item["name"] for item in (await response.json())["files"]])

            response = await client.get(
                "/api/files", headers={"Authorization": f"Bearer {bob_token}"})
            self.assertEqual([], (await response.json())["files"])

            response = await client.get(
                "/api/files/download/notes.txt",
                headers={"Authorization": f"Bearer {alice_token}"})
            self.assertEqual(b"private contents", await response.read())

            with self.assertRaises(web.HTTPBadRequest):
                server._safe_filename("../outside.txt")
        finally:
            await client.close()

    async def test_stream_config_is_confirmed_over_websocket(self):
        server = HostServer.__new__(HostServer)
        server.config = {
            "client_port": 8600, "host_port": 8532, "accepting": True,
            "max_sessions": 4, "work_only_mode": False,
        }
        width, height = 64, 48
        server.capture = SimpleNamespace(
            frame=(bytes([20, 60, 120, 255]) * width * height,
                   width, height, time.time(), 1),
            max_fps=30, backend="test", mon_offset=(0, 0),
            bbox_since=lambda _start, _end: None,
        )
        server.injector = SimpleNamespace(apply=lambda _event: None)
        server.sessions = {}
        server.static_info = {"hostname": "Test host"}
        server.bench = {}
        server._enc_jobs = {}
        server._load_cache = (0.0, None)
        server._load_task = None
        server._loop = asyncio.get_running_loop()
        server._frame_evt = asyncio.Event()
        server._login_attempts = collections.defaultdict(collections.deque)
        server._get_load = mock.AsyncMock(return_value={"cpu_percent": 5, "ram_percent": 10})
        server._cursor_info = lambda: None
        app = server._build_app()
        app.on_startup.clear()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            auth.create_user("streamer", "long-enough-password", max_fps=60)
            token = auth.issue_token("streamer")
            socket = await client.ws_connect(f"/ws/stream?token={token}")
            hello = await socket.receive_json(timeout=2)
            self.assertEqual("hello", hello["type"])
            self.assertEqual(60, hello["limits"]["max_fps"])

            await socket.send_json({
                "type": "config", "request_id": 42, "fps": 60,
                "quality": 80, "scale": 0.5, "profile": "custom",
                "adaptive": False,
            })
            applied = None
            for _ in range(10):
                message = await socket.receive(timeout=2)
                if message.type == WSMsgType.TEXT:
                    payload = json.loads(message.data)
                    if payload.get("type") == "config_applied":
                        applied = payload
                        break
            self.assertIsNotNone(applied)
            self.assertEqual(42, applied["request_id"])
            self.assertEqual({"fps": 60, "quality": 80, "scale": 0.5},
                             applied["applied"])
            self.assertFalse(applied["selected"]["adaptive"])
            await socket.close()
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
