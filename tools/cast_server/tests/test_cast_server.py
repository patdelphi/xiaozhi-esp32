# -*- coding: utf-8 -*-
"""
程序说明：Waveshare ESP32-S3-RLCD-4.2 无线投屏后台服务与网页端控台测试套件。

覆盖范围：
1. .env 配置文件读取、默认值补全、字段合法性校验与安全写回（含注入拦截）。
2. 图像管线单元测试：8-bit 灰度化、四种抖动算法（floyd/atkinson/bayer/threshold）、
   尺寸适配（contain/cover/stretch）、旋转、反色、MSB-first 1-bit 位图打包及异常图片拦截。
3. HTTP 服务端与端到端集成测试：网页静态资源、/push（raw 与 multipart）、/api/config、
   /api/control、/preview.png、/health、/api/device-status，以及配合本地 Mock 设备验证
   HTTP Keep-Alive 推帧、重复帧跳过与保活心跳、设备离线/异常容错。
"""

import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import cast_server  # noqa: E402


def make_png_bytes(img: Image.Image) -> bytes:
    """将 PIL 图像编码为内存中的 PNG 字节流。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_multipart_payload(field_name: str, filename: str, content_type: str, data: bytes):
    """构造标准 multipart/form-data 请求体与对应的 Content-Type 头。"""
    boundary = "----CastServerTestBoundary7d9f2a"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", body


def http_request(url: str, method: str = "GET", body: bytes = None, headers: dict = None):
    """发送测试 HTTP 请求，统一返回 (status_code, resp_headers, resp_bytes)。"""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class MockDeviceState:
    """记录本地模拟 ESP32-S3 设备收到的所有请求及连接复用情况。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = []
        self.frame_headers = []
        self.heartbeats = 0
        self.disconnects = 0
        self.connection_keys = []
        self.fail_frame_status = 0
        self.streaming = False


class MockDeviceHTTPServer(ThreadingHTTPServer):
    """允许在单测中保存共享 MockDeviceState 的 HTTP 服务容器。"""

    daemon_threads = True

    def __init__(self, server_address, state: MockDeviceState):
        self.device_state = state
        super().__init__(server_address, MockDeviceHandler)


class MockDeviceHandler(BaseHTTPRequestHandler):
    """模拟 ESP32-S3 CastManager 的轻量 HTTP 处理器，支持 HTTP/1.1 Keep-Alive。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        """测试期间屏蔽标准输出访问日志。"""
        return

    def _record_conn(self):
        with self.server.device_state.lock:
            self.server.device_state.connection_keys.append(self.client_address)

    def _send_json(self, code: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._record_conn()
        if self.path == "/api/cast/status":
            with self.server.device_state.lock:
                payload = {
                    "running": True,
                    "streaming": self.server.device_state.streaming,
                    "audio_paused": False,
                    "width": 400,
                    "height": 300,
                    "bpp": 1,
                    "mode": "cast",
                    "ip": "127.0.0.1",
                    "port": self.server.server_port,
                    "frames": len(self.server.device_state.frames),
                    "fps": 1.0,
                    "battery": 88,
                    "rssi": -49,
                }
            self._send_json(200, payload)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        self._record_conn()
        content_len = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        state = self.server.device_state

        if self.path == "/api/cast/frame":
            if state.fail_frame_status:
                self._send_json(state.fail_frame_status, {"ok": False, "error": "injected failure"})
                return
            with state.lock:
                state.frames.append(body)
                state.frame_headers.append({k.lower(): v for k, v in self.headers.items()})
                state.streaming = True
                count = len(state.frames)
            self._send_json(200, {"ok": True, "frames": count, "blitted": True})
            return

        if self.path == "/api/cast/heartbeat":
            with state.lock:
                state.heartbeats += 1
            self._send_json(200, {"ok": True, "streaming": True})
            return

        if self.path == "/api/cast/disconnect":
            with state.lock:
                state.disconnects += 1
                state.streaming = False
            self._send_json(200, {"ok": True, "streaming": False})
            return

        self._send_json(404, {"ok": False, "error": "not found"})


class TestEnvConfig(unittest.TestCase):
    """验证 .env 配置解析、校验与持久化的正确性与安全性。"""

    def test_load_missing_env_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            cfg = cast_server.load_env_config(env_path)
            self.assertEqual(cfg["SCREEN_WIDTH"], 400)
            self.assertEqual(cfg["SCREEN_HEIGHT"], 300)
            self.assertEqual(cfg["DITHER_MODE"], "floyd")
            self.assertEqual(cfg["LISTEN_PORT"], 8787)
            self.assertFalse(cfg["INVERT"])
            self.assertTrue(cfg["SKIP_UNCHANGED"])

    def test_save_and_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            updated = cast_server.validate_config(
                {
                    **cast_server.DEFAULT_CONFIG,
                    "DEVICE_IP": "192.168.31.42",
                    "DEVICE_PORT": 8081,
                    "PUSH_INTERVAL_MS": 250,
                    "DITHER_MODE": "atkinson",
                    "FIT_MODE": "cover",
                    "ROTATE": 90,
                    "INVERT": True,
                    "AUTO_SWITCH": True,
                    "CAST_TOKEN": "demo-token-123",
                }
            )
            cast_server.save_env_config(env_path, updated)
            reloaded = cast_server.load_env_config(env_path)
            self.assertEqual(reloaded["DEVICE_IP"], "192.168.31.42")
            self.assertEqual(reloaded["DEVICE_PORT"], 8081)
            self.assertEqual(reloaded["PUSH_INTERVAL_MS"], 250)
            self.assertEqual(reloaded["DITHER_MODE"], "atkinson")
            self.assertEqual(reloaded["FIT_MODE"], "cover")
            self.assertEqual(reloaded["ROTATE"], 90)
            self.assertTrue(reloaded["INVERT"])
            self.assertTrue(reloaded["AUTO_SWITCH"])
            self.assertEqual(reloaded["CAST_TOKEN"], "demo-token-123")

    def test_validation_rejects_invalid_values_and_crlf_injection(self):
        bad_cases = [
            {"DEVICE_IP": "192.168.1.10\nINJECT=1"},
            {"DEVICE_PORT": 70000},
            {"PUSH_INTERVAL_MS": 10},
            {"DITHER_MODE": "unknown_algo"},
            {"FIT_MODE": "invalid_fit"},
            {"ROTATE": 45},
            {"THRESHOLD": 300},
            {"SCREEN_WIDTH": 401},  # 宽度必须为 8 的倍数
            {"CAST_TOKEN": "abc\r\ndef"},
        ]
        for patch in bad_cases:
            with self.assertRaises(cast_server.ConfigValidationError):
                cast_server.validate_config({**cast_server.DEFAULT_CONFIG, **patch})

    def test_inline_comment_stripping_and_quoted_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                'DEVICE_IP=192.168.1.55  # 客厅设备\n'
                'CAST_TOKEN="tok#with#hash" # 行内注释\n'
                "DITHER_MODE='atkinson' # 抖动算法\n",
                encoding="utf-8",
            )
            cfg = cast_server.load_env_config(env_path)
            self.assertEqual(cfg["DEVICE_IP"], "192.168.1.55")
            self.assertEqual(cfg["CAST_TOKEN"], "tok#with#hash")
            self.assertEqual(cfg["DITHER_MODE"], "atkinson")


class TestImagePipeline(unittest.TestCase):
    """验证图像灰度化、四类抖动、适配裁剪与 1-bit MSB-first 打包。"""

    def test_pack_mono_1bit_msb_order(self):
        # 构造 16x2 测试图：第 0 行首尾各一白点，第 1 行全白
        img = Image.new("1", (16, 2), 0)
        img.putpixel((0, 0), 1)
        img.putpixel((15, 0), 1)
        for x in range(16):
            img.putpixel((x, 1), 1)

        packed = cast_server.pack_mono_1bit(img)
        self.assertEqual(packed, bytes([0x80, 0x01, 0xFF, 0xFF]))

    def test_all_dither_modes_produce_exact_15000_bytes(self):
        # 生成 800x600 彩色渐变图，检验缩放、抖动和输出长度
        src = Image.new("RGB", (800, 600))
        for y in range(600):
            for x in range(800):
                src.putpixel((x, y), ((x * 255) // 799, (y * 255) // 599, 128))
        raw_png = make_png_bytes(src)

        for mode in ("floyd", "atkinson", "bayer", "threshold"):
            cfg = {**cast_server.DEFAULT_CONFIG, "DITHER_MODE": mode}
            result = cast_server.convert_image_to_mono(raw_png, cfg)
            self.assertEqual(len(result.mono_bytes), 15000, msg=f"mode={mode}")
            self.assertEqual((result.width, result.height), (400, 300))
            preview_img = Image.open(io.BytesIO(result.preview_png))
            self.assertEqual(preview_img.size, (400, 300))
            self.assertEqual(len(result.sha256), 64)

    def test_fit_modes_rotate_and_invert(self):
        # 生成左黑右白的宽屏图：包含缩放与反色检查
        src = Image.new("RGB", (200, 100), (255, 255, 255))
        for y in range(100):
            for x in range(100):
                src.putpixel((x, y), (0, 0, 0))
        raw_png = make_png_bytes(src)

        normal = cast_server.convert_image_to_mono(
            raw_png, {**cast_server.DEFAULT_CONFIG, "DITHER_MODE": "threshold", "FIT_MODE": "stretch"}
        )
        inverted = cast_server.convert_image_to_mono(
            raw_png,
            {**cast_server.DEFAULT_CONFIG, "DITHER_MODE": "threshold", "FIT_MODE": "stretch", "INVERT": True},
        )
        # 正常图左半黑(0x00)右半白(0xFF)，反色图应逐字节按位取反
        self.assertEqual(normal.mono_bytes[0], 0x00)
        self.assertEqual(normal.mono_bytes[49], 0xFF)
        self.assertEqual(inverted.mono_bytes[0], 0xFF)
        self.assertEqual(inverted.mono_bytes[49], 0x00)

        for fit in ("contain", "cover", "stretch"):
            for rot in (0, 90, 180, 270):
                res = cast_server.convert_image_to_mono(
                    raw_png, {**cast_server.DEFAULT_CONFIG, "FIT_MODE": fit, "ROTATE": rot}
                )
                self.assertEqual(len(res.mono_bytes), 15000)

    def test_corrupt_image_raises_conversion_error(self):
        with self.assertRaises(cast_server.ImageConversionError):
            cast_server.convert_image_to_mono(b"not-a-valid-image-payload", cast_server.DEFAULT_CONFIG)


class TestCastWebConsoleAndPushIntegration(unittest.TestCase):
    """端到端集成测试：网页控台静态资源、配置保存、推图、Keep-Alive 复用与设备容错。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.tmpdir.name) / ".env"

        self.mock_state = MockDeviceState()
        self.mock_device = MockDeviceHTTPServer(("127.0.0.1", 0), self.mock_state)
        self.device_thread = threading.Thread(target=self.mock_device.serve_forever, daemon=True)
        self.device_thread.start()

        init_cfg = cast_server.validate_config(
            {
                **cast_server.DEFAULT_CONFIG,
                "DEVICE_IP": "127.0.0.1",
                "DEVICE_PORT": self.mock_device.server_port,
                "LISTEN_HOST": "127.0.0.1",
                "LISTEN_PORT": 8787,
                "PUSH_INTERVAL_MS": 100,
                "HEARTBEAT_INTERVAL_SEC": 1,
                "SKIP_UNCHANGED": True,
                "AUTO_SWITCH": True,
                "CAST_TIMEOUT_SEC": 20,
                "CAST_TOKEN": "secret-cast",
                "HTTP_TIMEOUT_SEC": 2.0,
            }
        )
        cast_server.save_env_config(self.env_path, init_cfg)

        self.engine = cast_server.CastService(self.env_path)
        self.httpd = cast_server.create_http_server(self.engine, "127.0.0.1", 0)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.engine.start()
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.http_thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.stop()
        self.mock_device.shutdown()
        self.mock_device.server_close()
        self.tmpdir.cleanup()

    def test_web_assets_served(self):
        for route, expected_mime in (
            ("/", "text/html"),
            ("/app.css", "text/css"),
            ("/app.js", "application/javascript"),
        ):
            code, headers, body = http_request(f"{self.base_url}{route}")
            self.assertEqual(code, 200)
            self.assertIn(expected_mime, headers.get("Content-Type", ""))
            self.assertGreater(len(body), 100)
            if route == "/app.js":
                js_text = body.decode("utf-8")
                self.assertIn("frame_seq", js_text)
                self.assertIn("refreshPreviewImage", js_text)
                self.assertIn("healthInFlight", js_text)
                self.assertIn("deviceInFlight", js_text)

    def test_push_raw_and_multipart_with_keepalive_skip_and_heartbeat(self):
        img = Image.new("RGB", (400, 300), (240, 240, 240))
        raw_png = make_png_bytes(img)

        code, _, body = http_request(
            f"{self.base_url}/push",
            method="POST",
            body=raw_png,
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(code, 200)
        resp_json = json.loads(body.decode("utf-8"))
        self.assertTrue(resp_json["ok"])
        self.assertEqual(resp_json["frame_bytes"], 15000)

        # 等待定时推流线程推送首帧并在至少 1.2 秒内触发去重跳过与保活心跳
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self.mock_state.lock:
                if len(self.mock_state.frames) >= 1 and self.mock_state.heartbeats >= 1:
                    break
            time.sleep(0.05)

        with self.mock_state.lock:
            self.assertEqual(len(self.mock_state.frames), 1)  # SKIP_UNCHANGED=true 仅推 1 帧
            self.assertEqual(len(self.mock_state.frames[0]), 15000)
            hdrs = self.mock_state.frame_headers[0]
            self.assertEqual(hdrs.get("x-cast-token"), "secret-cast")
            self.assertEqual(hdrs.get("x-auto-switch"), "1")
            self.assertEqual(hdrs.get("x-cast-timeout"), "20")
            self.assertGreaterEqual(self.mock_state.heartbeats, 1)
            # Keep-Alive 复用同一个客户端端口
            self.assertEqual(len(set(self.mock_state.connection_keys)), 1)

        # 再通过 multipart/form-data 推送不同图像，应立刻产生第 2 帧
        img2 = Image.new("RGB", (400, 300), (10, 10, 10))
        ctype, mbody = make_multipart_payload("image", "dark.png", "image/png", make_png_bytes(img2))
        code, _, _ = http_request(
            f"{self.base_url}/push",
            method="POST",
            body=mbody,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(code, 200)

        deadline = time.time() + 2.0
        while time.time() < deadline:
            with self.mock_state.lock:
                if len(self.mock_state.frames) >= 2:
                    break
            time.sleep(0.05)
        with self.mock_state.lock:
            self.assertEqual(len(self.mock_state.frames), 2)

        # 校验 preview.png 与 health
        code, headers, preview = http_request(f"{self.base_url}/preview.png")
        self.assertEqual(code, 200)
        self.assertIn("image/png", headers.get("Content-Type", ""))
        self.assertEqual(Image.open(io.BytesIO(preview)).size, (400, 300))

        code, _, health_raw = http_request(f"{self.base_url}/health")
        self.assertEqual(code, 200)
        health = json.loads(health_raw.decode("utf-8"))
        self.assertGreaterEqual(health["frame_seq"], 2)
        self.assertTrue(health["last_received_at"])
        self.assertGreaterEqual(health["stats"]["pushed_frames"], 2)
        self.assertGreaterEqual(health["stats"]["skipped_frames"], 1)

    def test_push_corrupt_image_returns_400(self):
        code, _, body = http_request(
            f"{self.base_url}/push",
            method="POST",
            body=b"garbage-bytes",
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(code, 400)
        self.assertFalse(json.loads(body.decode("utf-8"))["ok"])

    def test_config_update_persists_and_recomputes_preview(self):
        # 推送灰阶渐变，再改用 threshold 并验证配置持久化和预览刷新
        grad = Image.new("L", (400, 300))
        for y in range(300):
            for x in range(400):
                grad.putpixel((x, y), (x * 255) // 399)
        http_request(
            f"{self.base_url}/push",
            method="POST",
            body=make_png_bytes(grad),
            headers={"Content-Type": "image/png"},
        )
        _, _, h1_raw = http_request(f"{self.base_url}/health")
        sha_floyd = json.loads(h1_raw.decode("utf-8"))["frame_sha256"]

        # 非法参数应返回 400 且不覆盖 .env
        code, _, err_body = http_request(
            f"{self.base_url}/api/config",
            method="POST",
            body=json.dumps({"DITHER_MODE": "bad_mode"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(code, 400)
        self.assertFalse(json.loads(err_body.decode("utf-8"))["ok"])

        # 合法切换为 threshold=200 + INVERT=true
        code, _, ok_body = http_request(
            f"{self.base_url}/api/config",
            method="POST",
            body=json.dumps({"DITHER_MODE": "threshold", "THRESHOLD": 200, "INVERT": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(code, 200)
        cfg_resp = json.loads(ok_body.decode("utf-8"))
        self.assertEqual(cfg_resp["config"]["DITHER_MODE"], "threshold")
        self.assertEqual(cfg_resp["config"]["THRESHOLD"], 200)
        self.assertTrue(cfg_resp["config"]["INVERT"])

        reloaded = cast_server.load_env_config(self.env_path)
        self.assertEqual(reloaded["DITHER_MODE"], "threshold")
        self.assertEqual(reloaded["THRESHOLD"], 200)
        self.assertTrue(reloaded["INVERT"])

        _, _, h2_raw = http_request(f"{self.base_url}/health")
        sha_threshold = json.loads(h2_raw.decode("utf-8"))["frame_sha256"]
        self.assertNotEqual(sha_floyd, sha_threshold)

    def test_control_actions_and_device_status_proxy(self):
        # 生成测试图并立即推送
        code, _, ctrl_raw = http_request(
            f"{self.base_url}/api/control",
            method="POST",
            body=json.dumps({"action": "test_pattern"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(ctrl_raw.decode("utf-8"))["ok"])

        deadline = time.time() + 2.0
        while time.time() < deadline:
            with self.mock_state.lock:
                if len(self.mock_state.frames) >= 1:
                    break
            time.sleep(0.05)
        self.assertGreaterEqual(len(self.mock_state.frames), 1)

        # 代理查询设备状态
        code, _, dev_raw = http_request(f"{self.base_url}/api/device-status")
        self.assertEqual(code, 200)
        dev = json.loads(dev_raw.decode("utf-8"))
        self.assertTrue(dev["ok"])
        self.assertEqual(dev["device"]["mode"], "cast")
        self.assertEqual(dev["device"]["battery"], 88)

        # 暂停、恢复与断开操作
        for act in ("pause", "resume", "disconnect"):
            code, _, body = http_request(
                f"{self.base_url}/api/control",
                method="POST",
                body=json.dumps({"action": act}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body.decode("utf-8"))["ok"])
        self.assertEqual(self.mock_state.disconnects, 1)

    def test_device_error_is_handled_gracefully_without_crashing(self):
        self.mock_state.fail_frame_status = 500
        img = Image.new("RGB", (400, 300), (180, 180, 180))
        http_request(
            f"{self.base_url}/push",
            method="POST",
            body=make_png_bytes(img),
            headers={"Content-Type": "image/png"},
        )

        # 立即推送并等待失败计数更新
        http_request(
            f"{self.base_url}/api/control",
            method="POST",
            body=json.dumps({"action": "push_now"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        deadline = time.time() + 2.0
        health = {}
        while time.time() < deadline:
            _, _, hraw = http_request(f"{self.base_url}/health")
            health = json.loads(hraw.decode("utf-8"))
            if health["stats"]["failed_pushes"] >= 1:
                break
            time.sleep(0.05)

        self.assertGreaterEqual(health["stats"]["failed_pushes"], 1)
        self.assertIn("500", health["stats"]["last_error"])

    def test_device_target_switch_clears_dedup_hash_and_pushes_new_target(self):
        img = Image.new("RGB", (400, 300), (220, 220, 220))
        code, _, _ = http_request(
            f"{self.base_url}/push?push_now=1",
            method="POST",
            body=make_png_bytes(img),
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(code, 200)

        deadline = time.time() + 2.0
        while time.time() < deadline:
            with self.mock_state.lock:
                if len(self.mock_state.frames) >= 1:
                    break
            time.sleep(0.05)
        self.assertEqual(len(self.mock_state.frames), 1)

        # 启动第二个 Mock 设备并通过 /api/config 切换 DEVICE_PORT，确保不因去重跳过首帧
        state2 = MockDeviceState()
        device2 = MockDeviceHTTPServer(("127.0.0.1", 0), state2)
        t2 = threading.Thread(target=device2.serve_forever, daemon=True)
        t2.start()
        try:
            code, _, _ = http_request(
                f"{self.base_url}/api/config",
                method="POST",
                body=json.dumps({"DEVICE_IP": "127.0.0.1", "DEVICE_PORT": device2.server_port}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(code, 200)

            deadline = time.time() + 2.5
            while time.time() < deadline:
                with state2.lock:
                    if len(state2.frames) >= 1:
                        break
                time.sleep(0.05)
            with state2.lock:
                self.assertEqual(len(state2.frames), 1)
                self.assertEqual(len(state2.frames[0]), 15000)
        finally:
            device2.shutdown()
            device2.server_close()

    def test_concurrent_push_during_config_update_keeps_latest_frame(self):
        white_png = make_png_bytes(Image.new("RGB", (400, 300), (255, 255, 255)))
        black_png = make_png_bytes(Image.new("RGB", (400, 300), (0, 0, 0)))

        self.engine.push_upstream_image(white_png)
        orig_convert = cast_server.convert_image_to_mono
        injected = False

        def wrapped_convert(raw_bytes, cfg):
            nonlocal injected
            res = orig_convert(raw_bytes, cfg)
            # 在首次 update_config 重算白图期间模拟并发推入黑图
            if not injected and raw_bytes == white_png:
                injected = True
                self.engine.push_upstream_image(black_png)
            return res

        with patch.object(cast_server, "convert_image_to_mono", side_effect=wrapped_convert):
            self.engine.update_config({"INVERT": True})

        expected_sha = orig_convert(black_png, {**self.engine.get_config(), "INVERT": True}).sha256
        self.assertEqual(self.engine.get_health()["frame_sha256"], expected_sha)

    def test_oversized_content_length_returns_400_with_connection_close(self):
        with socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=3.0) as sock:
            sock.settimeout(3.0)
            sock.sendall(
                b"POST /push HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 99999999\r\n"
                b"\r\n"
            )
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
            resp_raw = b"".join(chunks).decode("utf-8", errors="replace")
            self.assertIn("400 Bad Request", resp_raw)
            self.assertIn("Connection: close", resp_raw)

    def test_resolve_runtime_dirs_source_and_frozen(self):
        # 源码运行模式：指向脚本目录及同级 web/
        src_base, src_web = cast_server._resolve_runtime_dirs()
        self.assertEqual(src_base, SERVER_DIR)
        self.assertEqual(src_web, SERVER_DIR / "web")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe_dir = root / "app"
            meipass_dir = root / "bundle"
            exe_dir.mkdir(parents=True)
            (meipass_dir / "web").mkdir(parents=True)
            fake_exe = exe_dir / "CastServer.exe"
            fake_exe.write_bytes(b"")

            # 打包模式默认读取 _MEIPASS/web，配置落在 exe 同级目录
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(fake_exe)), \
                 patch.object(sys, "_MEIPASS", str(meipass_dir), create=True):
                app_base, app_web = cast_server._resolve_runtime_dirs()
                self.assertEqual(app_base, exe_dir.resolve())
                self.assertEqual(app_web, meipass_dir / "web")

                # 若 exe 同级放置自定义 web/ 目录，应优先覆盖内置资源
                (exe_dir / "web").mkdir()
                app_base2, app_web2 = cast_server._resolve_runtime_dirs()
                self.assertEqual(app_base2, exe_dir.resolve())
                self.assertEqual(app_web2, exe_dir.resolve() / "web")

    def test_tray_helpers_icon_and_mode_selection(self):
        icon_img = cast_server._build_tray_icon_image()
        self.assertEqual(icon_img.size, (64, 64))
        self.assertEqual(icon_img.mode, "RGBA")

        self.assertFalse(cast_server._should_use_tray(False, False, is_frozen=False))
        self.assertTrue(cast_server._should_use_tray(False, False, is_frozen=True))
        self.assertTrue(cast_server._should_use_tray(True, False, is_frozen=False))
        self.assertFalse(cast_server._should_use_tray(True, True, is_frozen=True))

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "cast_server.log"
            with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
                fp = cast_server._setup_headless_stdio(log_path)
                try:
                    self.assertIsNotNone(fp)
                    print("headless stdio ok")
                finally:
                    if fp is not None:
                        fp.close()
            self.assertIn("headless stdio ok", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
