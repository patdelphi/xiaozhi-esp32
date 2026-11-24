# 第四屏无线投屏功能实施计划（todo.md）

## 1. 需求与硬件基线

- **目标仓库**：`xiaozhi-esp32`（`waveshare-s3-rlcd-4.2`，400×300 横屏 ST7305 RLCD）
- **设计文档**：`docs/Waveshare-ESP32-S3-RLCD-4.2-无线投屏设计文档.md`
- **屏幕灰度能力**：当前硬件驱动（`rlcd_driver.cc` 中 `0x3A=0x11`、`0xB9=0x20`）工作在 **1-bit 单色（2 级黑白）**模式，单帧显存固定为 `15,000` 字节。后台将输入图像转为 8-bit 灰度图，经抖动算法（`floyd` / `atkinson` / `bayer` / `threshold`）映射为 1-bit 帧，在黑白屏上呈现灰阶过渡与清晰文字。
- **刷新率上限估算**：
  - SPI3 总线（40 MHz）：单帧 `15,000` 字节纯传输约 `3.0 ms`，加窗口指令与驱动开销约 `3.5–4 ms`。
  - ST7305 面板与液晶光学响应：当前配置开启 High Power Mode（`0x38`、`0xB2=0x02`），反射式液晶在高于 `15–20 FPS` 时易产生拖影，清晰可用的视觉上限约 **10–15 FPS**。
  - 渲染路径对比：经 LVGL `lvgl_port`（50 ms 定时器 + 240 KB RGB565 缓冲 + 12 万像素查表）实际约 `5–10 FPS`；采用 1-bit 帧直写路径并复用 HTTP Keep-Alive 长连接，局域网整机可达 **10–15 FPS**。
  - 推荐运行参数：静态看板类场景设 `0.2–2 FPS`（`500–5000 ms`），动态刷新场景设 `5–10 FPS`（`100–200 ms`）。
- **存储与内存余量**：当前 `ota_0` 分区剩余约 `426 KB`，静态 D/IRAM 剩余约 `175 KB`，板载 `8 MB` PSRAM；新增 `esp_http_server`、第四屏 UI 与投屏管理预计占用 `40–70 KB` Flash，空间充足。

## 2. 架构与改造范围

### 2.1 设计文档（`xiaozhi-esp32/docs/`）

- 新增 `docs/Waveshare-ESP32-S3-RLCD-4.2-无线投屏设计文档.md`，覆盖总体架构、灰度与刷新率定量估算、HTTP 通信协议、设备端状态机与语音防卡顿、网页端控台设计、`.env` 全量配置说明与测试方案。

### 2.2 后台服务与网页端控台（`xiaozhi-esp32/tools/cast_server/`）

- 核心脚本：`cast_server.py`（单个轻量 Python 3 工具，仅依赖标准库与 `Pillow`），配套 `.env.example`、`README.md` 与 `tests/test_cast_server.py`。
- 网页端控台：`web/index.html`、`web/app.css`、`web/app.js`：
  - 400×300 像素级 1-bit 实时模拟屏框与本地图片拖拽/上传测试；
  - 立即推流、暂停/恢复、测试图生成、主动断开控制；
  - 可视化 `.env` 配置表单（支持失焦/提交验证并即时重算预览）；
  - 后台遥测统计、代理查询设备状态与上游接入示例代码复制；
  - 遵循原生 `<meta name="color-scheme" content="light dark">` 浅/深色自适应与表单无障碍规范。
- 服务端点：`GET /`、`POST /push`、`GET /api/config`、`POST /api/config`、`POST /api/control`、`GET /api/device-status`、`GET /health`、`GET /preview.png`。
- 推送调度：按 `PUSH_INTERVAL_MS` 周期检查最新帧，开启 `SKIP_UNCHANGED` 时对未变化画面跳过推流并按 `HEARTBEAT_INTERVAL_SEC` 发送 `POST /api/cast/heartbeat` 保活；通过 HTTP Keep-Alive 推送到 `http://{DEVICE_IP}:{DEVICE_PORT}/api/cast/frame`。

### 2.3 设备端（`xiaozhi-esp32/main/`）

- `main/CMakeLists.txt`：`PRIV_REQUIRES` 增加 `esp_http_server`。
- `main/boards/waveshare-s3-rlcd-4.2/rlcd_driver.h` 与 `rlcd_driver.cc`：新增 `RLCD_DisplayMono1Bit(const uint8_t* mono_buf, size_t len)`，接收 400×300 行优先 1-bit 位图（MSB 在前，1 为白，0 为黑，共 `15,000` 字节）直写刷新屏幕。
- `main/boards/waveshare-s3-rlcd-4.2/cast_ui.cc`（新增）：创建第四屏等待页 `cast_page_`，采用高对比纯黑白样式与完整中文字库，展示顶部状态栏、本机 IPv4:8080、`等待连接` 状态与接收统计提示。
- `main/boards/waveshare-s3-rlcd-4.2/managers/cast_manager.h` 与 `cast_manager.cc`（新增）：板载轻量 HTTP 服务（默认端口 `8080`），实现 `POST /api/cast/frame`、`POST /api/cast/heartbeat`、`POST /api/cast/disconnect`、`GET /api/cast/status`，支持可选 `X-Cast-Token`、`X-Auto-Switch`、`X-Cast-Timeout`、超时回退与小智语音会话防卡顿保护。
- `main/boards/waveshare-s3-rlcd-4.2/custom_lcd_display.h` 与 `custom_lcd_display.cc`：扩展 `MODE_CAST = 3`、`CycleDisplayMode()` 四页循环、`ApplyDisplayMode()`、`SwitchToCastPage()`、`IsCastMode()`、投屏帧缓存与 `Lvgl_flush_cb` 旁路同步。
- `main/boards/waveshare-s3-rlcd-4.2/data_update_task.cc`：在 Wi-Fi 就绪时启动 `CastManager`，定时更新第四屏等待页 IPv4 与状态栏，执行超时检测与语音结束补刷。
- `main/boards/waveshare-s3-rlcd-4.2/waveshare-s3-rlcd-4.2.cc`：更新按键日志、扩展 MCP `self.disp.switch`（支持 `cast`）并新增 `self.cast.status`。

## 3. 实施阶段

1. **阶段 1：设计文档与 `todo.md` 更新**
2. **阶段 2：后台服务与网页端控台（测试先行）**
3. **阶段 3：ESP32-S3 第四屏与设备接收端**
4. **阶段 4：全量单元/集成测试与 ESP-IDF `v5.5.4` 固件编译**
5. **阶段 5：第四屏离屏 UI 渲染与 1-bit 抖动效果图视觉核查**
6. **阶段 6：收尾核对与汇报**

## 4. 完成标准

- [x] 设计文档 `docs/Waveshare-ESP32-S3-RLCD-4.2-无线投屏设计文档.md` 已写入并通过格式校验（UTF-8 BOM + CRLF）。
- [x] `tools/cast_server/tests/test_cast_server.py` 全部单元测试与 Mock 设备端到端集成测试通过（含畸形输入、设备离线异常、网页控台接口）。
- [x] `.env.example` 字段齐全且 `.env` 不被 Git 跟踪；网页端控台交互与校验符合现代 Web 表单与深浅主题规范。
- [x] 本机 ESP-IDF `v5.5.4` 成功编译生成 `xiaozhi.bin`，无新增警告或错误，分区余量充足。
- [x] 第四屏等待页（联网/离线双态）离屏渲染清晰无缺字、边框完整；后台测试图 `floyd` / `atkinson` 抖动预览图核查通过。
- [x] 所有新增源码包含文件头程序说明与中文关键注释，所有接口调用具备异常处理。
