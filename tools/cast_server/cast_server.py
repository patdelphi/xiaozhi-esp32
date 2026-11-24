# -*- coding: utf-8 -*-
"""
程序说明：Waveshare ESP32-S3-RLCD-4.2 无线投屏后台与网页端控台服务。

主要职责：
1. 读取、校验并持久化 .env 配置（设备 IP/端口、推送间隔、灰度抖动模式、适配裁剪等）。
2. 提供 POST /push 接收上游服务推送的原始图像，转为 8-bit 灰度并执行软件抖动
   （floyd / atkinson / bayer / threshold），打包为行优先 MSB-first 1-bit 位图（400x300 = 15,000 B）。
3. 后台调度线程按 PUSH_INTERVAL_MS 使用 HTTP/1.1 Keep-Alive 长连接推送至设备端
   /api/cast/frame，支持帧哈希去重与周期保活心跳。
4. 托管位于 tools/cast_server/web/ 的网页端控台及配置、控制、预览、遥测代理接口。
"""

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import signal
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy_default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFont, ImageOps

def _resolve_runtime_dirs() -> Tuple[Path, Path]:
    """解析可读写运行目录与网页静态资源目录（兼容源码运行与 PyInstaller 打包）。"""
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
        bundle_dir = Path(getattr(sys, "_MEIPASS", app_dir))
        web_dir = (app_dir / "web") if (app_dir / "web").is_dir() else (bundle_dir / "web")
        return app_dir, web_dir
    src_dir = Path(__file__).resolve().parent
    return src_dir, src_dir / "web"


BASE_DIR, WEB_DIR = _resolve_runtime_dirs()
MAX_UPLOAD_BYTES = 16 * 1024 * 1024  # 限制单张上游图片不超过 16 MB

DITHER_MODES = ("floyd", "atkinson", "bayer", "threshold")
FIT_MODES = ("contain", "cover", "stretch")
ROTATE_VALUES = (0, 90, 180, 270)

DEFAULT_CONFIG: Dict[str, Any] = {
    "DEVICE_IP": "192.168.1.100",
    "DEVICE_PORT": 8080,
    "CAST_TOKEN": "",
    "HTTP_TIMEOUT_SEC": 3.0,
    "LISTEN_HOST": "0.0.0.0",
    "LISTEN_PORT": 8787,
    "PUSH_INTERVAL_MS": 1000,
    "SKIP_UNCHANGED": True,
    "HEARTBEAT_INTERVAL_SEC": 10,
    "CAST_TIMEOUT_SEC": 30,
    "AUTO_SWITCH": False,
    "SCREEN_WIDTH": 400,
    "SCREEN_HEIGHT": 300,
    "DITHER_MODE": "floyd",
    "THRESHOLD": 128,
    "FIT_MODE": "contain",
    "ROTATE": 0,
    "INVERT": False,
}

CONFIG_ORDER = list(DEFAULT_CONFIG.keys())

# 4x4 有序 Bayer 矩阵，映射到 0..255 像素比较区间
_BAYER_4X4 = (
    (8, 136, 40, 168),
    (200, 72, 232, 104),
    (56, 184, 24, 152),
    (248, 120, 216, 88),
)


class ConfigValidationError(ValueError):
    """配置校验失败异常，附带逐字段错误明细。"""

    def __init__(self, message: str, errors: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.errors = errors or {}


class ImageConversionError(ValueError):
    """图像解码或 1-bit 位图转换失败异常。"""


@dataclass
class ConvertedFrame:
    """单帧图像转换结果。"""

    mono_bytes: bytes
    preview_png: bytes
    sha256: str
    width: int
    height: int


def _parse_bool(value: Any, field: str) -> bool:
    """将配置中的布尔表示规范化为 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ConfigValidationError(f"{field} 必须为布尔值", {field: "请使用 true 或 false"})


def _parse_int(value: Any, field: str, min_val: int, max_val: int) -> int:
    """解析并校验整数范围。"""
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not int")
        parsed = int(str(value).strip())
    except (ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"{field} 必须为整数", {field: f"请输入 {min_val} 到 {max_val} 之间的整数"}
        ) from exc
    if parsed < min_val or parsed > max_val:
        raise ConfigValidationError(
            f"{field} 超出允许范围 [{min_val}, {max_val}]",
            {field: f"允许范围为 {min_val} 到 {max_val}"},
        )
    return parsed


def _parse_float(value: Any, field: str, min_val: float, max_val: float) -> float:
    """解析并校验浮点数范围。"""
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not float")
        parsed = float(str(value).strip())
    except (ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"{field} 必须为数值", {field: f"请输入 {min_val} 到 {max_val} 之间的数字"}
        ) from exc
    if parsed < min_val or parsed > max_val:
        raise ConfigValidationError(
            f"{field} 超出允许范围 [{min_val}, {max_val}]",
            {field: f"允许范围为 {min_val} 到 {max_val}"},
        )
    return round(parsed, 3)


def _check_safe_line(value: Any, field: str) -> str:
    """校验单行字符串字段，防止 .env 换行符注入。"""
    text = "" if value is None else str(value).strip()
    if "\r" in text or "\n" in text or "\x00" in text:
        raise ConfigValidationError(f"{field} 包含非法换行或空字符", {field: "禁止包含换行或控制字符"})
    return text


def validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化完整配置字典，返回强类型配置副本。"""
    if not isinstance(raw, dict):
        raise ConfigValidationError("配置数据必须为 JSON 对象", {})

    merged = {**DEFAULT_CONFIG, **raw}
    errors: Dict[str, str] = {}
    out: Dict[str, Any] = {}

    try:
        device_ip = _check_safe_line(merged["DEVICE_IP"], "DEVICE_IP")
        if not device_ip or not re.fullmatch(r"[A-Za-z0-9.\-:]+", device_ip):
            raise ConfigValidationError(
                "DEVICE_IP 格式无效", {"DEVICE_IP": "请输入合法 IPv4 或主机名，勿包含协议前缀"}
            )
        out["DEVICE_IP"] = device_ip
    except ConfigValidationError as exc:
        errors.update(exc.errors or {"DEVICE_IP": str(exc)})

    try:
        listen_host = _check_safe_line(merged["LISTEN_HOST"], "LISTEN_HOST")
        if not listen_host or not re.fullmatch(r"[A-Za-z0-9.\-:]+", listen_host):
            raise ConfigValidationError(
                "LISTEN_HOST 格式无效", {"LISTEN_HOST": "请输入合法绑定地址（例如 0.0.0.0 或 127.0.0.1）"}
            )
        out["LISTEN_HOST"] = listen_host
    except ConfigValidationError as exc:
        errors.update(exc.errors or {"LISTEN_HOST": str(exc)})

    try:
        out["CAST_TOKEN"] = _check_safe_line(merged.get("CAST_TOKEN", ""), "CAST_TOKEN")
    except ConfigValidationError as exc:
        errors.update(exc.errors or {"CAST_TOKEN": str(exc)})

    int_specs = (
        ("DEVICE_PORT", 1, 65535),
        ("LISTEN_PORT", 1, 65535),
        ("PUSH_INTERVAL_MS", 50, 3600000),
        ("HEARTBEAT_INTERVAL_SEC", 0, 3600),
        ("CAST_TIMEOUT_SEC", 0, 86400),
        ("SCREEN_WIDTH", 8, 2000),
        ("SCREEN_HEIGHT", 8, 2000),
        ("THRESHOLD", 0, 255),
    )
    for key, low, high in int_specs:
        try:
            out[key] = _parse_int(merged[key], key, low, high)
        except ConfigValidationError as exc:
            errors.update(exc.errors or {key: str(exc)})

    if "SCREEN_WIDTH" in out and out["SCREEN_WIDTH"] % 8 != 0:
        errors["SCREEN_WIDTH"] = "屏幕宽度必须为 8 的倍数（本板默认 400）"

    try:
        out["HTTP_TIMEOUT_SEC"] = _parse_float(merged["HTTP_TIMEOUT_SEC"], "HTTP_TIMEOUT_SEC", 0.2, 60.0)
    except ConfigValidationError as exc:
        errors.update(exc.errors or {"HTTP_TIMEOUT_SEC": str(exc)})

    for key in ("SKIP_UNCHANGED", "AUTO_SWITCH", "INVERT"):
        try:
            out[key] = _parse_bool(merged[key], key)
        except ConfigValidationError as exc:
            errors.update(exc.errors or {key: str(exc)})

    dither = str(merged["DITHER_MODE"]).strip().lower()
    if dither not in DITHER_MODES:
        errors["DITHER_MODE"] = f"仅支持 {', '.join(DITHER_MODES)}"
    else:
        out["DITHER_MODE"] = dither

    fit_mode = str(merged["FIT_MODE"]).strip().lower()
    if fit_mode not in FIT_MODES:
        errors["FIT_MODE"] = f"仅支持 {', '.join(FIT_MODES)}"
    else:
        out["FIT_MODE"] = fit_mode

    try:
        rotate = _parse_int(merged["ROTATE"], "ROTATE", 0, 270)
        if rotate not in ROTATE_VALUES:
            errors["ROTATE"] = "旋转角度仅支持 0、90、180、270"
        else:
            out["ROTATE"] = rotate
    except ConfigValidationError as exc:
        errors.update(exc.errors or {"ROTATE": str(exc)})

    if errors:
        first_msg = next(iter(errors.values()))
        raise ConfigValidationError(f"配置校验失败：{first_msg}", errors)
    return out


def load_env_config(env_path: Path) -> Dict[str, Any]:
    """从 .env 文件读取并校验配置；若文件不存在则返回默认配置。"""
    parsed: Dict[str, Any] = {}
    if env_path.exists():
        try:
            text = env_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConfigValidationError(f"读取配置文件失败: {exc}") from exc
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] in ('"', "'"):
                quote = val[0]
                end_idx = val.find(quote, 1)
                if end_idx != -1:
                    trailing = val[end_idx + 1 :].strip()
                    if not trailing or trailing.startswith("#"):
                        val = val[1:end_idx]
            else:
                # 仅剥离非引号包裹值右侧的行内注释，避免配置值混入注释文本
                val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
            if key in DEFAULT_CONFIG:
                parsed[key] = val
    return validate_config(parsed)


def save_env_config(env_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    """校验并按规范格式将配置写入 .env 文件（UTF-8 无 BOM）。"""
    clean = validate_config(config)
    lines = [
        "# Waveshare ESP32-S3-RLCD-4.2 无线投屏运行配置（由后台控台自动维护）",
        "",
    ]
    for key in CONFIG_ORDER:
        val = clean[key]
        if isinstance(val, bool):
            serialized = "true" if val else "false"
        else:
            serialized = str(val)
        lines.append(f"{key}={serialized}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return clean


def _flatten_alpha_to_white(img: Image.Image) -> Image.Image:
    """将含透明通道或调色板的输入图合成为纯白底 RGB 图像。"""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _dither_atkinson(gray: Image.Image, threshold: int) -> Image.Image:
    """使用 Atkinson 误差扩散将 8-bit 灰度图转为 1-bit 图像（保留高对比细节）。"""
    w, h = gray.size
    buf = list(gray.tobytes())
    out = bytearray(w * h)
    for y in range(h):
        row_off = y * w
        has_next1 = (y + 1) < h
        has_next2 = (y + 2) < h
        for x in range(w):
            idx = row_off + x
            old = buf[idx]
            new_val = 255 if old >= threshold else 0
            out[idx] = new_val
            step = (old - new_val) >> 3  # 每次向周围 6 个像素传播 1/8 误差
            if step == 0:
                continue
            if x + 1 < w:
                buf[idx + 1] += step
            if x + 2 < w:
                buf[idx + 2] += step
            if has_next1:
                n1 = idx + w
                if x > 0:
                    buf[n1 - 1] += step
                buf[n1] += step
                if x + 1 < w:
                    buf[n1 + 1] += step
            if has_next2:
                buf[idx + (w << 1)] += step
    return Image.frombytes("L", (w, h), bytes(out)).convert("1", dither=Image.Dither.NONE)


def _dither_bayer(gray: Image.Image) -> Image.Image:
    """使用 4x4 有序 Bayer 矩阵进行抖动，消除高频刷新时的帧间随机噪点。"""
    w, h = gray.size
    src = gray.tobytes()
    out = bytearray(w * h)
    for y in range(h):
        bayer_row = _BAYER_4X4[y & 3]
        row_off = y * w
        for x in range(w):
            val = src[row_off + x]
            out[row_off + x] = 255 if val >= bayer_row[x & 3] else 0
    return Image.frombytes("L", (w, h), bytes(out)).convert("1", dither=Image.Dither.NONE)


def pack_mono_1bit(img: Image.Image) -> bytes:
    """将 1-bit 单色图打包为行优先 MSB-first 字节流（bit 1=白, 0=黑）。"""
    if img.mode != "1":
        img = img.convert("1", dither=Image.Dither.NONE)
    w, _ = img.size
    if w % 8 != 0:
        raise ImageConversionError(f"图像宽度 {w} 必须为 8 的倍数")
    return img.tobytes()


def convert_image_to_mono(raw_bytes: bytes, config: Dict[str, Any]) -> ConvertedFrame:
    """将上游图片字节转换并打包为指定分辨率的 1-bit 黑白帧与 PNG 预览图。"""
    if not raw_bytes:
        raise ImageConversionError("图片数据为空")

    width = int(config["SCREEN_WIDTH"])
    height = int(config["SCREEN_HEIGHT"])
    fit_mode = config["FIT_MODE"]
    rotate = int(config["ROTATE"])
    dither_mode = config["DITHER_MODE"]
    threshold = int(config["THRESHOLD"])
    invert = bool(config["INVERT"])

    try:
        with Image.open(io.BytesIO(raw_bytes)) as opened:
            opened.load()
            rgb = _flatten_alpha_to_white(opened)
    except Exception as exc:
        raise ImageConversionError(f"无法解码图片: {exc}") from exc

    if rotate in (90, 180, 270):
        # Pillow rotate 为逆时针方向，使用负角度实现顺时针旋转
        rgb = rgb.rotate(-rotate, expand=True)

    resampling = Image.Resampling.LANCZOS
    if fit_mode == "contain":
        contained = ImageOps.contain(rgb, (width, height), method=resampling)
        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        offset_x = (width - contained.width) // 2
        offset_y = (height - contained.height) // 2
        canvas.paste(contained, (offset_x, offset_y))
        fitted = canvas
    elif fit_mode == "cover":
        fitted = ImageOps.fit(rgb, (width, height), method=resampling)
    else:
        fitted = rgb.resize((width, height), resample=resampling)

    gray = fitted.convert("L")
    if invert:
        gray = ImageOps.invert(gray)

    if dither_mode == "threshold":
        mono_img = gray.point(lambda p: 255 if p >= threshold else 0, mode="1")
    elif dither_mode == "floyd":
        mono_img = gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    elif dither_mode == "atkinson":
        mono_img = _dither_atkinson(gray, threshold)
    elif dither_mode == "bayer":
        mono_img = _dither_bayer(gray)
    else:
        raise ImageConversionError(f"不支持的抖动算法: {dither_mode}")

    mono_bytes = pack_mono_1bit(mono_img)
    expected_len = (width * height) // 8
    if len(mono_bytes) != expected_len:
        raise ImageConversionError(f"帧字节长度异常: 期望 {expected_len}, 实际 {len(mono_bytes)}")

    preview_buf = io.BytesIO()
    mono_img.save(preview_buf, format="PNG")
    preview_png = preview_buf.getvalue()
    sha256 = hashlib.sha256(mono_bytes).hexdigest()

    return ConvertedFrame(
        mono_bytes=mono_bytes,
        preview_png=preview_png,
        sha256=sha256,
        width=width,
        height=height,
    )


def generate_test_pattern_bytes(width: int = 400, height: int = 300, label: str = "") -> bytes:
    """生成一张包含灰阶条、棋盘格、同心圆与文本标签的 PNG 测试图，用于对比不同抖动模式。"""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 顶部黑底标题栏
    draw.rectangle((0, 0, width - 1, 36), fill=(0, 0, 0))
    font = ImageFont.load_default()
    stamp = label or time.strftime("%H:%M:%S")
    draw.text((12, 12), f"WAVESHARE RLCD TEST  {width}x{height}  {stamp}", fill=(255, 255, 255), font=font)

    # 中上部连续灰阶渐变条
    grad_top, grad_bottom = 48, 126
    for x in range(16, width - 16):
        tone = int(((x - 16) * 255) / max(1, (width - 33)))
        draw.line((x, grad_top, x, grad_bottom), fill=(tone, tone, tone))
    draw.rectangle((16, grad_top, width - 17, grad_bottom), outline=(0, 0, 0), width=2)

    # 左下角径向渐变圆球，用于验证误差扩散表现
    cx, cy, radius = 92, 212, 56
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            dist2 = dx * dx + dy * dy
            if dist2 <= radius * radius:
                dist = (dist2 ** 0.5) / radius
                shade = int(min(255, max(0, 40 + 215 * dist)))
                img.putpixel((cx + dx, cy + dy), (shade, shade, shade))
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(0, 0, 0), width=2)

    # 右下角黑白棋盘与线条区，用于验证边缘锐度
    cell = 16
    box_x0, box_y0 = 176, 152
    for r in range(7):
        for c in range(13):
            color = (0, 0, 0) if (r + c) % 2 == 0 else (255, 255, 255)
            x0 = box_x0 + c * cell
            y0 = box_y0 + r * cell
            if x0 + cell <= width - 16 and y0 + cell <= height - 16:
                draw.rectangle((x0, y0, x0 + cell - 1, y0 + cell - 1), fill=color)
    draw.rectangle((box_x0, box_y0, width - 17, height - 17), outline=(0, 0, 0), width=2)

    # 外边框
    draw.rectangle((0, 0, width - 1, height - 1), outline=(0, 0, 0), width=2)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class CastService:
    """投屏后台核心引擎：维护最新图像缓存、配置持久化与 HTTP Keep-Alive 推送循环。"""

    def __init__(self, env_path: Path):
        self.env_path = env_path
        self._lock = threading.Lock()
        self._push_io_lock = threading.Lock()
        self._config_write_lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)

        self._config: Dict[str, Any] = load_env_config(env_path)
        self._latest_raw: Optional[bytes] = None
        self._latest_frame: Optional[ConvertedFrame] = None
        self._frame_seq = 0
        self._last_received_at: Optional[str] = None
        self._paused = False
        self._running = False
        self._force_push = False
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self._conn: Optional[http.client.HTTPConnection] = None
        self._conn_target: Optional[Tuple[str, int, float]] = None

        self._last_pushed_hash: Optional[str] = None
        self._last_pushed_seq: int = -1
        self._last_device_touch_mono: float = 0.0

        self._stats: Dict[str, Any] = {
            "pushed_frames": 0,
            "skipped_frames": 0,
            "failed_pushes": 0,
            "heartbeats_sent": 0,
            "last_push_ms": None,
            "last_push_at": None,
            "last_error": "",
            "last_device_response": "",
        }

    def start(self) -> None:
        """启动后台定时推流工作线程。"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._worker_thread = threading.Thread(target=self._push_loop, name="CastPushWorker", daemon=True)
            self._worker_thread.start()

    def stop(self) -> None:
        """停止后台推流线程并关闭复用 HTTP 连接。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
            self._wakeup.notify_all()
            worker = self._worker_thread
            self._worker_thread = None
        if worker:
            worker.join(timeout=3.0)
        with self._push_io_lock:
            self._close_conn()

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._conn_target = None

    def _request_device(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, bytes]:
        """向 ESP32-S3 发送 Keep-Alive HTTP 请求；断链时自动重建一次连接。"""
        with self._lock:
            cfg = dict(self._config)
        target = (cfg["DEVICE_IP"], int(cfg["DEVICE_PORT"]), float(cfg["HTTP_TIMEOUT_SEC"]))

        req_headers = {"Connection": "keep-alive"}
        if cfg["CAST_TOKEN"]:
            req_headers["X-Cast-Token"] = cfg["CAST_TOKEN"]
        if headers:
            req_headers.update(headers)

        with self._push_io_lock:
            last_exc: Optional[Exception] = None
            for _attempt in range(2):
                try:
                    if self._conn is None or self._conn_target != target:
                        self._close_conn()
                        self._conn = http.client.HTTPConnection(target[0], target[1], timeout=target[2])
                        self._conn_target = target
                    self._conn.request(method, path, body=body, headers=req_headers)
                    resp = self._conn.getresponse()
                    resp_body = resp.read()
                    if resp.will_close:
                        self._close_conn()
                    return resp.status, resp_body
                except Exception as exc:
                    last_exc = exc
                    self._close_conn()
            raise RuntimeError(f"连接设备 {target[0]}:{target[1]} 失败: {last_exc}") from last_exc

    def get_config(self) -> Dict[str, Any]:
        """返回当前配置副本。"""
        with self._lock:
            return dict(self._config)

    def update_config(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        """校验更新部分或全部配置、写入 .env，并基于缓存原图即时重算预览与帧数据。"""
        if not isinstance(partial, dict):
            raise ConfigValidationError("请求体必须为 JSON 对象", {})

        with self._config_write_lock:
            with self._lock:
                merged = {**self._config, **partial}
                new_cfg = validate_config(merged)
                raw_copy = self._latest_raw

            new_frame: Optional[ConvertedFrame] = None
            if raw_copy is not None:
                new_frame = convert_image_to_mono(raw_copy, new_cfg)

            save_env_config(self.env_path, new_cfg)

            while True:
                with self._lock:
                    # 若配置写盘/转码期间收到新图，退出锁按最新原图重算后再提交，避免旧图覆盖新帧
                    if self._latest_raw is not raw_copy:
                        raw_copy = self._latest_raw
                    else:
                        target_changed = (
                            new_cfg["DEVICE_IP"] != self._config["DEVICE_IP"]
                            or new_cfg["DEVICE_PORT"] != self._config["DEVICE_PORT"]
                            or new_cfg["HTTP_TIMEOUT_SEC"] != self._config["HTTP_TIMEOUT_SEC"]
                        )
                        policy_changed = (
                            new_cfg["AUTO_SWITCH"] != self._config["AUTO_SWITCH"]
                            or new_cfg["CAST_TIMEOUT_SEC"] != self._config["CAST_TIMEOUT_SEC"]
                            or new_cfg["CAST_TOKEN"] != self._config["CAST_TOKEN"]
                        )
                        self._config = new_cfg
                        if new_frame is not None:
                            self._latest_frame = new_frame
                            self._frame_seq += 1
                        if target_changed or policy_changed:
                            self._last_pushed_hash = None
                            self._last_device_touch_mono = 0.0
                        self._wakeup.notify_all()
                        break

                new_frame = convert_image_to_mono(raw_copy, new_cfg) if raw_copy is not None else None

            if target_changed:
                with self._push_io_lock:
                    self._close_conn()
            return dict(new_cfg)

    def push_upstream_image(self, raw_bytes: bytes, push_now: bool = False) -> ConvertedFrame:
        """接收上游图片，转换后刷新最新帧缓存，必要时触发立即推送。"""
        cfg = self.get_config()
        frame = convert_image_to_mono(raw_bytes, cfg)
        with self._lock:
            self._latest_raw = raw_bytes
            self._latest_frame = frame
            self._frame_seq += 1
            self._last_received_at = time.strftime("%Y-%m-%d %H:%M:%S")
            if push_now:
                self._force_push = True
            self._wakeup.notify_all()
        return frame

    def get_preview_png(self) -> Optional[bytes]:
        """获取最新一帧 1-bit 抖动预览图；尚无图片时返回占位测试图预览。"""
        with self._lock:
            if self._latest_frame is not None:
                return self._latest_frame.preview_png
            cfg = dict(self._config)
        # 无任何推送图时按需生成静态等待引导画面预览（不进入推流队列）
        placeholder = Image.new("RGB", (cfg["SCREEN_WIDTH"], cfg["SCREEN_HEIGHT"]), (255, 255, 255))
        draw = ImageDraw.Draw(placeholder)
        draw.rectangle((4, 4, cfg["SCREEN_WIDTH"] - 5, cfg["SCREEN_HEIGHT"] - 5), outline=(0, 0, 0), width=2)
        draw.text((24, 130), "NO FRAME YET - UPLOAD OR GENERATE TEST IMAGE", fill=(0, 0, 0))
        buf = io.BytesIO()
        placeholder.save(buf, format="PNG")
        return buf.getvalue()

    def control(self, action: str) -> Dict[str, Any]:
        """执行网页控台控制动作：push_now / pause / resume / disconnect / test_pattern。"""
        act = str(action or "").strip().lower()
        if act == "pause":
            with self._lock:
                self._paused = True
            return {"ok": True, "action": act, "paused": True}

        if act == "resume":
            with self._lock:
                self._paused = False
                self._force_push = True
                self._wakeup.notify_all()
            return {"ok": True, "action": act, "paused": False}

        if act == "push_now":
            with self._lock:
                has_frame = self._latest_frame is not None
                if has_frame:
                    self._force_push = True
                    self._wakeup.notify_all()
            if not has_frame:
                raise ConfigValidationError("当前尚无已接收图像，请先推送或生成测试图")
            return {"ok": True, "action": act}

        if act == "test_pattern":
            cfg = self.get_config()
            pattern_bytes = generate_test_pattern_bytes(cfg["SCREEN_WIDTH"], cfg["SCREEN_HEIGHT"])
            frame = self.push_upstream_image(pattern_bytes, push_now=True)
            return {"ok": True, "action": act, "sha256": frame.sha256, "frame_bytes": len(frame.mono_bytes)}

        if act == "disconnect":
            with self._lock:
                self._paused = True
                self._last_pushed_hash = None
            try:
                status, body = self._request_device("POST", "/api/cast/disconnect", body=b"")
                resp_text = body.decode("utf-8", errors="replace")
                if status >= 400:
                    raise RuntimeError(f"设备返回状态 {status}: {resp_text}")
                with self._lock:
                    self._stats["last_device_response"] = resp_text
                    self._stats["last_error"] = ""
                return {"ok": True, "action": act, "paused": True, "device_response": resp_text}
            except Exception as exc:
                with self._lock:
                    self._stats["last_error"] = str(exc)
                return {"ok": False, "action": act, "paused": True, "error": str(exc)}

        raise ConfigValidationError(f"未知控制指令: {act}")

    def query_device_status(self) -> Dict[str, Any]:
        """代理查询设备端 /api/cast/status，避免前端浏览器的跨源限制。"""
        with self._lock:
            cfg = dict(self._config)
        conn: Optional[http.client.HTTPConnection] = None
        try:
            conn = http.client.HTTPConnection(
                cfg["DEVICE_IP"], int(cfg["DEVICE_PORT"]), timeout=float(cfg["HTTP_TIMEOUT_SEC"])
            )
            headers = {}
            if cfg["CAST_TOKEN"]:
                headers["X-Cast-Token"] = cfg["CAST_TOKEN"]
            conn.request("GET", "/api/cast/status", headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                return {"ok": False, "error": f"设备 HTTP {resp.status}: {raw}"}
            return {"ok": True, "device": json.loads(raw)}
        except Exception as exc:
            return {"ok": False, "error": f"无法访问设备: {exc}"}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_health(self) -> Dict[str, Any]:
        """返回后台服务健康状态、最新帧摘要与推送统计。"""
        with self._lock:
            return {
                "ok": True,
                "running": self._running,
                "paused": self._paused,
                "has_frame": self._latest_frame is not None,
                "frame_seq": self._frame_seq,
                "frame_sha256": self._latest_frame.sha256 if self._latest_frame else None,
                "frame_bytes": len(self._latest_frame.mono_bytes) if self._latest_frame else 0,
                "last_received_at": self._last_received_at,
                "config": dict(self._config),
                "stats": dict(self._stats),
            }

    def _push_once_locked_snapshot(self) -> Tuple[bool, Optional[ConvertedFrame], int, Dict[str, Any], bool]:
        with self._lock:
            return (
                self._paused,
                self._latest_frame,
                self._frame_seq,
                dict(self._config),
                self._force_push,
            )

    def _push_loop(self) -> None:
        """后台定时推送主循环：按 PUSH_INTERVAL_MS 下发变更帧并发送保活心跳。"""
        while not self._stop_event.is_set():
            paused, frame, seq, cfg, force = self._push_once_locked_snapshot()
            interval_sec = max(0.05, float(cfg["PUSH_INTERVAL_MS"]) / 1000.0)
            now_mono = time.monotonic()

            if frame is not None and (not paused or force):
                should_push = force or (not cfg["SKIP_UNCHANGED"]) or (frame.sha256 != self._last_pushed_hash)
                if should_push:
                    with self._lock:
                        self._force_push = False
                    self._send_frame(frame, seq, cfg, now_mono)
                else:
                    # 帧未变化：跳过图像传输，必要时发保活心跳
                    with self._lock:
                        self._last_pushed_seq = seq
                        self._stats["skipped_frames"] += 1
                    hb_sec = int(cfg["HEARTBEAT_INTERVAL_SEC"])
                    if hb_sec > 0 and self._last_device_touch_mono > 0 and (now_mono - self._last_device_touch_mono) >= hb_sec:
                        self._send_heartbeat(cfg, now_mono)
            elif force:
                with self._lock:
                    self._force_push = False

            with self._lock:
                if self._stop_event.is_set():
                    break
                if self._force_push:
                    continue
                self._wakeup.wait(timeout=interval_sec)

    def _send_frame(self, frame: ConvertedFrame, seq: int, cfg: Dict[str, Any], now_mono: float) -> None:
        t0 = time.monotonic()
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Auto-Switch": "1" if cfg["AUTO_SWITCH"] else "0",
            "X-Cast-Timeout": str(int(cfg["CAST_TIMEOUT_SEC"])),
        }
        try:
            status, body = self._request_device("POST", "/api/cast/frame", body=frame.mono_bytes, headers=headers)
            resp_text = body.decode("utf-8", errors="replace")
            if status >= 400:
                raise RuntimeError(f"设备返回 HTTP {status}: {resp_text}")
            elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
            with self._lock:
                self._last_pushed_hash = frame.sha256
                self._last_pushed_seq = seq
                self._last_device_touch_mono = now_mono
                self._stats["pushed_frames"] += 1
                self._stats["last_push_ms"] = elapsed_ms
                self._stats["last_push_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._stats["last_error"] = ""
                self._stats["last_device_response"] = resp_text
        except Exception as exc:
            with self._lock:
                self._stats["failed_pushes"] += 1
                self._stats["last_error"] = str(exc)

    def _send_heartbeat(self, cfg: Dict[str, Any], now_mono: float) -> None:
        headers = {"X-Cast-Timeout": str(int(cfg["CAST_TIMEOUT_SEC"]))}
        try:
            status, body = self._request_device("POST", "/api/cast/heartbeat", body=b"", headers=headers)
            resp_text = body.decode("utf-8", errors="replace")
            if status >= 400:
                raise RuntimeError(f"心跳返回 HTTP {status}: {resp_text}")
            with self._lock:
                self._last_device_touch_mono = now_mono
                self._stats["heartbeats_sent"] += 1
                self._stats["last_error"] = ""
                self._stats["last_device_response"] = resp_text
        except Exception as exc:
            with self._lock:
                self._stats["failed_pushes"] += 1
                self._stats["last_error"] = str(exc)


def _extract_uploaded_image(content_type: str, body: bytes) -> bytes:
    """从 raw 二进制或 multipart/form-data 请求体中提取图片原始字节。"""
    if not body:
        raise ImageConversionError("请求体为空，请上传有效图片")

    ctype_lower = (content_type or "").lower()
    if "multipart/form-data" in ctype_lower:
        try:
            raw_msg = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
            msg = BytesParser(policy=email_policy_default).parsebytes(raw_msg)
            if not msg.is_multipart():
                raise ImageConversionError("multipart 请求格式无效")
            fallback_payload: Optional[bytes] = None
            for part in msg.iter_parts():
                disp_name = part.get_param("name", header="content-disposition")
                payload = part.get_payload(decode=True)
                if payload and disp_name in ("image", "file"):
                    return payload
                if payload and fallback_payload is None and part.get_filename():
                    fallback_payload = payload
            if fallback_payload is not None:
                return fallback_payload
            raise ImageConversionError("multipart 表单中未找到 image 或 file 文件字段")
        except ImageConversionError:
            raise
        except Exception as exc:
            raise ImageConversionError(f"解析 multipart 表单失败: {exc}") from exc

    return body


class CastHTTPServer(ThreadingHTTPServer):
    """持有 CastService 实例的多线程 HTTP 服务容器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], service: CastService):
        self.service = service
        super().__init__(server_address, CastRequestHandler)


class CastRequestHandler(BaseHTTPRequestHandler):
    """处理网页端控台静态页面、图片推送、配置与控制 API。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """统一精简终端请求日志。"""
        sys.stderr.write(f"[CastServer] {self.address_string()} - {format % args}\n")

    def _send_bytes(self, status: int, content_type: str, data: bytes, extra_headers: Optional[Dict[str, str]] = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            has_conn_hdr = bool(extra_headers and any(k.lower() == "connection" for k in extra_headers))
            if self.close_connection and not has_conn_hdr:
                self.send_header("Connection", "close")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端提前断开连接时终止 Keep-Alive 复用
            self.close_connection = True

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", raw)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            self.close_connection = True
            raise ValueError("Content-Length 请求头无效") from exc
        if length < 0 or length > MAX_UPLOAD_BYTES:
            self.close_connection = True
            raise ValueError(f"请求体大小超出限制（上限 {MAX_UPLOAD_BYTES} 字节）")
        return self.rfile.read(length) if length > 0 else b""

    def _serve_static(self, filename: str, content_type: str) -> None:
        target = (WEB_DIR / filename).resolve()
        if not target.is_file() or target.parent != WEB_DIR.resolve():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "静态资源不存在"})
            return
        self._send_bytes(HTTPStatus.OK, content_type, target.read_bytes())

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                self._serve_static("index.html", "text/html; charset=utf-8")
                return
            if path == "/app.css":
                self._serve_static("app.css", "text/css; charset=utf-8")
                return
            if path == "/app.js":
                self._serve_static("app.js", "application/javascript; charset=utf-8")
                return
            if path == "/api/config":
                self._send_json(HTTPStatus.OK, {"ok": True, "config": self.server.service.get_config()})
                return
            if path == "/health":
                self._send_json(HTTPStatus.OK, self.server.service.get_health())
                return
            if path == "/api/device-status":
                self._send_json(HTTPStatus.OK, self.server.service.query_device_status())
                return
            if path == "/preview.png":
                png = self.server.service.get_preview_png()
                if png is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "暂无预览图"})
                    return
                self._send_bytes(HTTPStatus.OK, "image/png", png)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            raw_body = self._read_body()

            if path == "/push":
                img_bytes = _extract_uploaded_image(self.headers.get("Content-Type", ""), raw_body)
                push_now = query.get("push_now", ["0"])[0].lower() in ("1", "true", "yes")
                frame = self.server.service.push_upstream_image(img_bytes, push_now=push_now)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "width": frame.width,
                        "height": frame.height,
                        "frame_bytes": len(frame.mono_bytes),
                        "sha256": frame.sha256,
                    },
                )
                return

            if path == "/api/config":
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"JSON 解析失败: {exc}"})
                    return
                updated = self.server.service.update_config(payload)
                self._send_json(HTTPStatus.OK, {"ok": True, "config": updated})
                return

            if path == "/api/control":
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"JSON 解析失败: {exc}"})
                    return
                result = self.server.service.control(payload.get("action", ""))
                code = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
                self._send_json(code, result)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except ConfigValidationError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc), "fields": exc.errors})
        except ImageConversionError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})


def create_http_server(service: CastService, host: str, port: int) -> CastHTTPServer:
    """构建绑定指定地址的 HTTP 控台服务器实例。"""
    return CastHTTPServer((host, port), service)


def _setup_headless_stdio(log_path: Path) -> Optional[Any]:
    """无控制台模式（console=False）下将空标准输出重定向到日志文件，避免输出报错。"""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log_fp
    if sys.stderr is None:
        sys.stderr = log_fp
    return log_fp


def _build_tray_icon_image() -> Image.Image:
    """生成系统托盘 64×64 图标（深色外框 + 浅白 RLCD 内屏 + 无线投屏指示）。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 8, 60, 56), radius=10, fill=(24, 34, 48, 255), outline=(66, 133, 244, 255), width=2)
    draw.rounded_rectangle((10, 14, 54, 50), radius=4, fill=(242, 246, 250, 255))
    # 右上信号弧线 + 左下投屏基点
    draw.ellipse((15, 37, 25, 47), fill=(18, 24, 32, 255))
    draw.arc((12, 26, 36, 50), start=270, end=360, fill=(18, 24, 32, 255), width=3)
    draw.arc((10, 18, 46, 54), start=270, end=360, fill=(37, 99, 235, 255), width=3)
    return img


def _should_use_tray(args_tray: bool, args_no_tray: bool, is_frozen: bool) -> bool:
    """判断是否启用 Windows 系统托盘常驻模式。"""
    if args_no_tray:
        return False
    if args_tray:
        return True
    return bool(is_frozen)


def _run_with_tray(httpd: CastHTTPServer, service: CastService, log_path: Path) -> None:
    """后台线程运行 HTTP 服务，主线程挂载系统托盘图标并支持双击打开控台。"""
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    console_url = f"http://127.0.0.1:{httpd.server_port}/"
    try:
        import pystray

        def _open_console(*_args: Any) -> None:
            try:
                webbrowser.open(console_url)
            except Exception as exc:
                print(f"[警告] 打开控台浏览器失败: {exc}")

        def _open_log(*_args: Any) -> None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if not log_path.exists():
                    log_path.write_text("", encoding="utf-8")
                if sys.stdout is not None:
                    sys.stdout.flush()
                if os.name == "nt" and hasattr(os, "startfile"):
                    os.startfile(str(log_path))
                else:
                    webbrowser.open(log_path.as_uri())
            except Exception as exc:
                print(f"[警告] 打开运行日志失败: {exc}")

        def _quit_app(icon_obj: Any, *_args: Any) -> None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                icon_obj.stop()
            except Exception:
                pass

        menu = pystray.Menu(
            pystray.MenuItem("打开网页控台", _open_console, default=True),
            pystray.MenuItem("查看运行日志", _open_log),
            pystray.MenuItem("退出", _quit_app),
        )
        icon = pystray.Icon(
            "rlcd_cast_server",
            _build_tray_icon_image(),
            f"RLCD 无线投屏服务 ({httpd.server_port})",
            menu,
        )
        icon.run()
    except Exception as exc:
        print(f"[警告] 系统托盘不可用，回退无托盘常驻模式: {exc}")
        while server_thread.is_alive():
            server_thread.join(timeout=0.5)


def main() -> int:
    """命令行入口：加载 .env（不存在时自动从 .env.example 初始化）并启动网页端控台服务。"""
    parser = argparse.ArgumentParser(description="Waveshare ESP32-S3-RLCD-4.2 无线投屏后台与网页端控台")
    parser.add_argument("--env", default=str(BASE_DIR / ".env"), help=".env 配置文件路径")
    parser.add_argument("--tray", action="store_true", help="强制启用系统托盘图标运行")
    parser.add_argument("--no-tray", action="store_true", help="禁用系统托盘图标（前景阻塞运行）")
    args = parser.parse_args()

    env_path = Path(args.env).resolve()
    log_path = env_path.parent / "cast_server.log"
    log_fp = _setup_headless_stdio(log_path)

    example_candidates = [
        BASE_DIR / ".env.example",
        Path(getattr(sys, "_MEIPASS", BASE_DIR)) / ".env.example",
    ]
    example_path = next((p for p in example_candidates if p.exists()), example_candidates[0])
    if not env_path.exists() and example_path.exists():
        env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")

    service = CastService(env_path)
    cfg = service.get_config()
    httpd = create_http_server(service, cfg["LISTEN_HOST"], int(cfg["LISTEN_PORT"]))

    def _shutdown_handler(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    service.start()
    print(f"无线投屏后台与网页控台已启动: http://127.0.0.1:{httpd.server_port}")
    print(f"目标设备地址: http://{cfg['DEVICE_IP']}:{cfg['DEVICE_PORT']}/api/cast/frame")

    try:
        if _should_use_tray(args.tray, args.no_tray, bool(getattr(sys, "frozen", False))):
            _run_with_tray(httpd, service, log_path)
        else:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止投屏后台服务...")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        httpd.server_close()
        service.stop()
        if log_fp is not None:
            try:
                log_fp.flush()
                log_fp.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
