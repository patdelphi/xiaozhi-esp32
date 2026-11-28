# 升级至官方 xiaozhi-esp32 v2.6.0 并精简应用计划（todo.md）

## 1. 目标与变更范围

- **基线升级**：从当前 `v2.2.2`（`backup/v2.2.2-before-upgrade` @ `6d1d3d9`）升级到官方 `78/xiaozhi-esp32` **`v2.6.0`**（`4de00ae`，`ESP-IDF v6.0.1`）。
- **删除模块**：移除音乐播放（`music_ui.cc`、`SetMusic*`、核心音乐播放补丁）与番茄时钟/白噪音（`pomodoro_ui.cc`、`pomodoro_manager.*`、`sdcard_manager.*`、`self.pomodoro.*` 工具）。
- **语音唤醒**：按 `v2.6.0` 官方 ESP32-S3 默认配置启用 **“你好小智”**（`CONFIG_USE_AFE_WAKE_WORD=y` + `CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y`）。
- **保留能力**：
  - 首页（`MODE_WEATHER`）：四宫格时钟/日历/天气/SHTC3 温湿度/AI 对话与表情/NVS 备忘录首页。
  - 无线投屏（`MODE_CAST`）：板载 `CastManager`（HTTP `:8080`）、`cast_ui.cc` 等待页、`RLCD_DisplayMono1Bit` 1-bit 直写刷新、语音通话保护，以及 `tools/cast_server/` 与 `flash_package/`、`docs/`、`make.bat`/`make.ps1`。
  - 两页切换：`KEY` 按键（GPIO18）单击与语音 `self.disp.switch`（`toggle`/`weather`/`cast`）在首页与无线投屏页间切换。

## 2. 实施阶段

1. **阶段 1：备份与编译环境准备**
2. **阶段 2：切换官方 `v2.6.0` 代码底座**
3. **阶段 3：移植两页 UI 与无线投屏至 `main/boards/waveshare/esp32-s3-rlcd-4.2/` 并启用唤醒词**
4. **阶段 4：全量编译、打包、串口烧录、推屏验证与提交推送**

## 3. 完成标准

- [x] `backup/v2.2.2-before-upgrade` 已创建，`ESP-IDF v6.0.1` `idf.py` 可正常执行。
- [x] 核心代码对齐官方 `v2.6.0`，音乐与番茄钟完全移除，唤醒词“你好小智”（`wn9_nihaoxiaozhi_tts`）已启用。
- [x] `make.bat build full` 零错误编出 `v2.6.0` 固件并更新 `flash_package/`，App 与资源分区余量充足（App 剩余 `33.51%`，Assets 剩余 `85.52%`）。
- [x] `tools/cast_server/tests/test_cast_server.py` `22/22` 通过，`flash_package\flash.bat -CheckOnly` 通过。
- [x] 固件已烧录至 `COM5`，Wi-Fi 联网、NTP 同步、中文字库、`srmodels` 唤醒模型与 `CastServer` 推帧全部实测通过。
