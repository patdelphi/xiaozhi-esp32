# 无线投屏后台与网页端控台使用说明

## 1. 环境准备

- 本机 Python 版本：3.10+（仅依赖内置标准库与 `Pillow`）。
- 首次启动前复制配置模板（`.env` 已被 `.gitignore` 忽略，若直接启动服务也会自动按模板创建默认 `.env`）：

```powershell
Copy-Item ".env.example" ".env"
```

## 2. 启动后台与网页控台

```powershell
python cast_server.py
```

启动后在浏览器访问 `http://127.0.0.1:8787`：
- 左侧区域查看 1-bit 转换后的 `400×300` 实时预览、生成测试图、上传图片并查看设备在线遥测；
- 右侧表单修改设备 IP、推送间隔、黑白抖动算法（`floyd` / `atkinson` / `bayer` / `threshold`）、适配模式与旋转角度，保存后自动写回 `.env` 并立刻重算预览画面。

## 3. 上游服务推送示例

向后台 `/push` 推送图片字节（PNG/JPEG/BMP/WebP），后台会按 `.env` 中的间隔自动转为 `15,000` 字节 1-bit 帧并发送到 ESP32-S3：

```powershell
# raw 二进制推送（push_now=1 表示转换后立即发送）
curl.exe -X POST "http://127.0.0.1:8787/push?push_now=1" -H "Content-Type: image/png" --data-binary "@dashboard.png"
```

## 4. 运行自动化测试

```powershell
python -m unittest discover -s "tests" -v
```
