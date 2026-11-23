# Waveshare ESP32-S3-RLCD-4.2 板子分析文档

> 文档版本：v1.0
> 生成时间：2026-07-05
> 对应固件版本：xiaozhi-esp32 v2.2.2
> 板子目录：`main/boards/waveshare-s3-rlcd-4.2/`

## 目录

1. [板子定位](#1-板子定位)
2. [硬件配置](#2-硬件配置)
3. [自研 RLCD 驱动](#3-自研-rlcd-驱动)
4. [显示架构](#4-显示架构)
5. [Managers 模块](#5-managers-模块)
6. [UI 文件分工](#6-ui-文件分工)
7. [MCP 工具集](#7-mcp-工具集)
8. [电池读取算法](#8-电池读取算法)
9. [按键交互](#9-按键交互)
10. [音频配置](#10-音频配置)
11. [资源文件](#11-资源文件)
12. [初始化顺序](#12-初始化顺序)
13. [关键设计亮点](#13-关键设计亮点)
14. [文件路径索引](#14-文件路径索引)

---

## 1. 板子定位

这是 xiaozhi-esp32 项目中**唯一使用 1-bit 单色反射式 LCD（RLCD）** 的板子，定位为 **AI 天气站 + 备忘录 + 番茄钟 + 音乐播放器** 的多功能桌面屏。

- **板子目录**：`main/boards/waveshare-s3-rlcd-4.2/`
- **继承关系**：`WifiBoard` → `CustomBoard`（非 DualNetworkBoard，仅 WiFi）
- **构建配置**：`CONFIG_USE_DEVICE_AEC=y`（启用设备端 AEC）
- **硬件型号**：Waveshare ESP32-S3-RLCD-4.2
- **主控芯片**：ESP32-S3-WROOM-1-N16R8（16MB Flash + 8MB PSRAM）

---

## 2. 硬件配置

### 2.1 主控配置（`config.h`）

| 模块 | 配置 |
|------|------|
| 芯片 | ESP32-S3-WROOM-1-N16R8（16MB Flash + 8MB PSRAM）|
| 音频采样率 | 输入 24kHz / 输出 24kHz（双工）|
| AEC 参考 | `AUDIO_INPUT_REFERENCE=true` |
| I2C 主机 | `I2C_NUM_0` |
| LCD 主机 | `SPI3_HOST` |

### 2.2 引脚分配

| 功能 | 引脚 |
|------|------|
| I2C 总线（共享）| SDA=13 / SCL=14 |
| 音频 I2S MCLK | GPIO16 |
| 音频 I2S WS | GPIO45 |
| 音频 I2S BCLK | GPIO9 |
| 音频 I2S DIN | GPIO10 |
| 音频 I2S DOUT | GPIO8 |
| 音频 PA | GPIO46 |
| ES8311 解码器 | I2C 地址 `ES8311_CODEC_DEFAULT_ADDR` |
| ES7210 编码器 | I2C 地址 `ES7210_CODEC_DEFAULT_ADDR` |
| RLCD SPI3 MOSI | GPIO12 |
| RLCD SPI3 SCK | GPIO11 |
| RLCD DC | GPIO5 |
| RLCD CS | GPIO40 |
| RLCD RST | GPIO41 |
| RLCD TE | GPIO6 |
| RLCD 尺寸 | 400×300 |
| BOOT 按钮 | GPIO0 |
| USER 按钮 | GPIO18 |
| SD 卡 CLK | GPIO38 |
| SD 卡 CMD | GPIO21 |
| SD 卡 D0 | GPIO39 |
| 电池 ADC | ADC1_CH3（分压比 3:1）|

### 2.3 共享 I2C 总线设备

`InitializeI2c` 创建的主总线被 4 个设备共享：

- ES8311（音频输出 codec）
- ES7210（音频输入 codec）
- SHTC3（温湿度传感器，地址 0x70）
- PCF85063（RTC 实时时钟，地址 0x51）

### 2.4 板载外设

| 外设 | 型号 | 用途 |
|------|------|------|
| 显示屏 | 400×300 RLCD（1-bit 单色）| UI 显示 |
| 温湿度传感器 | SHTC3 | 状态栏温湿度 |
| 实时时钟 | PCF85063 | 断电保持时间 |
| 音频解码器 | ES8311 | 喇叭输出 |
| 音频编码器 | ES7210 | 麦克风输入 |
| D 类功放 | MAX98357A | 喇叭放大 |
| SD 卡槽 | SDMMC 接口 | 白噪音 MP3 |
| 锂电池接口 | + USB-C 充电 | 电源 |

---

## 3. 自研 RLCD 驱动

### 3.1 为什么需要自研

xiaozhi-esp32 主项目只支持 RGB565 彩屏，1-bit 单色 RLCD 没有现成驱动，必须从底层 SPI 命令到上层像素映射全部自研。

### 3.2 关键设计（`rlcd_driver.h` / `rlcd_driver.cc`）

1. **SPI 配置**：
   - `pclk_hz = 40MHz`
   - SPI mode 0
   - `trans_queue_depth=7`
   - `max_transfer_sz = width * height = 120000`

2. **1-bit 缓冲区**：
   - `DisplayLen = (400×300) >> 3 = 15000 字节`
   - 分配在 PSRAM（`MALLOC_CAP_SPIRAM`）

3. **像素映射 LUT（核心优化）**：
   - `PixelIndexLUT[x][y]` 与 `PixelBitLUT[x][y]` 两张查找表
   - 共 400×300×3 字节 ≈ 360KB PSRAM
   - **竖屏** `InitPortraitLUT`：每字节存 4 个像素（2×2 块）
   - **横屏** `InitLandscapeLUT`：每字节存 4 个像素（2×2 块），且做 Y 轴翻转
   - 作用：把 RGB565→1-bit 的转换从浮点运算降到查表 + 位运算，性能提升一个数量级

4. **`RLCD_Init`**：
   - 发送 30+ 条初始化命令（0xD6/0xC0/0xB3/0xC9/0x36 等）
   - 关键参数 `0x3A=0x11`（1-bit 颜色格式）
   - `0x36=0x48`（横屏翻转）
   - 复位序列：高 50ms → 低 20ms → 高 50ms

5. **`SetContrast`**：
   - 通过 VCOM 电压寄存器 0xC0 调节对比度
   - 推荐范围 0x0D-0x15
   - 默认 0x11（出厂值，最佳）

### 3.3 像素操作 API

| 方法 | 用途 |
|------|------|
| `RLCD_Init()` | 屏幕初始化（发送命令序列）|
| `RLCD_ColorClear(color)` | 清屏（填充指定颜色）|
| `RLCD_SetPixel(x, y, color)` | 设置单个像素（查表 + 位运算）|
| `RLCD_Display()` | 将缓冲区刷新到屏幕 |
| `SetContrast(level)` | 调节对比度（0x00-0x1F）|

---

## 4. 显示架构

### 4.1 继承与重写

`CustomLcdDisplay : LcdDisplay`（来自 `main/display/lcd_display.h`）

| 重写方法 | 原因 |
|----------|------|
| `SetTheme` | 空实现 — 单色屏不需要主题切换，避免基类操作不存在的控件崩溃 |
| `SetChatMessage` | 适配到左下角 AI 对话卡片 |
| `SetEmotion` | 适配到表情图片 + 文字组合 |
| `UpdateStatusBar` | 用 `lv_image` 图片图标替代 Font Awesome 文字（单色屏字体渲染问题）|
| `SetMusicInfo` / `SetMusicLyric` / `SetMusicProgress` | 适配音乐页三行歌词布局 |
| `SwitchToMusicPage` / `SwitchToWeatherPage` | 切换页面可见性 |

### 4.2 三页面模式

```cpp
enum DisplayMode {
    MODE_WEATHER = 0,   // 天气页（默认）
    MODE_MUSIC = 1,     // 音乐页
    MODE_POMODORO = 2,  // 番茄钟页
};
```

每个页面独立的 `lv_obj_t*` 容器（`weather_page_` / `music_page_` / `pomodoro_page_`），通过 `ApplyDisplayMode` 切换可见性。

### 4.3 天气页布局（400×300）

```
┌────────────────────────────────────────────┐
│ 🌡 24.5°C 60%        [WiFi] [🔋 85%]      │  ← 状态栏
├──────────────────┬─────────────────────────┤
│                  │        TUE             │
│      14:30       │        ──              │  ← 时钟(248×128) + 日历(130×128)
│                  │        15              │
│                  │     晴 ☀ 25°C          │
├──────────────────┼─────────────────────────┤
│  😊              │       MEMO             │
│  开心     正在聆听… │    ─────────────────  │  ← AI 对话(252×122) + 备忘录(126×122)
│                  │  · 15:00 开会          │
│                  │  · 买牛奶              │
└──────────────────┴─────────────────────────┘
```

| 区域 | 尺寸 | 内容 |
|------|------|------|
| 状态栏 | 浮动右上 | 温湿度 + WiFi 图标 + 电池图标 + 电量百分比 |
| 时钟卡片 | 248×128 | 大字时间 HH:MM（`time_label_`）|
| 日历卡片 | 130×128 | 星期 + 日期 + 天气文本（`day_label_` / `date_num_label_` / `weather_label_`）|
| AI 对话卡片 | 252×122 | 表情图片 + 状态文字 + 对话内容 |
| 备忘录卡片 | 126×122 | 多行备忘列表（最多 5 条）|

### 4.4 LVGL flush 回调（关键）

```cpp
void CustomLcdDisplay::Lvgl_flush_cb(lv_display_t * disp, const lv_area_t * area, uint8_t * color_p) {
    CustomLcdDisplay *self = (CustomLcdDisplay *)lv_display_get_user_data(disp);
    RlcdDriver *rlcd = self->rlcd_;
    uint16_t *buffer = (uint16_t *)color_p;
    for(int y = area->y1; y <= area->y2; y++) {
        for(int x = area->x1; x <= area->x2; x++) {
            uint8_t color = (*buffer < 0x7fff) ? ColorBlack : ColorWhite;
            rlcd->RLCD_SetPixel(x, y, color);
            buffer++;
        }
    }
    rlcd->RLCD_Display();
    lv_disp_flush_ready(disp);
}
```

把 LVGL 默认的 RGB565 颜色按"是否小于 0x7fff"二值化为黑白，写入 RLCD 缓冲区后调用 `RLCD_Display` 一次性刷新。

### 4.5 LVGL 初始化

- `lv_init()` + `lvgl_port_init`（任务优先级 2，timer 50ms）
- 缓冲区：RGB565 格式，大小 = `width * height * 2` 字节（240KB），分配在 PSRAM
- 渲染模式：`LV_DISPLAY_RENDER_MODE_PARTIAL`

### 4.6 省电策略

- 5 分钟无活动（`IDLE_TIMEOUT_MS = 5*60*1000`）→ `power_saving_=true`
- 正常刷新 1 秒（`NORMAL_REFRESH_MS`）
- 省电模式 5 秒（`SAVING_REFRESH_MS`）
- `NotifyUserActivity()` 重置计时器

### 4.7 「鱼咬尾」系统信息滚动

`ShowSystemInfo` 实现：

1. 拼接两份相同内容 `[A][A]`
2. 切换 `lv_obj_align` 到 `LV_ALIGN_TOP_LEFT`（原 `LEFT_MID` 会被 LVGL 布局覆盖动画 Y 值）
3. `lv_anim` 设置 Y 从 0 到 `-single_h`（单份高度），无限循环
4. 视觉上无缝衔接（第二个 A 开头 = 第一个 A 开头）
5. 开始前停顿 1.5 秒，速度每像素 30ms

---

## 5. Managers 模块

全部单例模式，由 `CustomBoard::InitializeSensors` / `InitializeSdcard` 触发初始化：

| Manager | 文件 | 职责 |
|---------|------|------|
| `SensorManager` | `managers/sensor_manager.h/.cc` | SHTC3 温湿度 + PCF85063 RTC + NTP 同步 |
| `WeatherManager` | `managers/weather_manager.h/.cc` | 支持 `update()` 走和风 API（当前禁用）+ `updateFromExternal()` 由 AI MCP 回写 |
| `SdcardManager` | `managers/sdcard_manager.h/.cc` | SDMMC 模式挂载 `/sdcard`，扫描白噪音 MP3 文件 |
| `PomodoroManager` | `managers/pomodoro_manager.h/.cc` | 倒计时任务 + 白噪音播放任务 |

### 5.1 SensorManager

- **SHTC3**：温湿度传感器，I2C 地址 0x70
- **PCF85063**：实时时钟，I2C 地址 0x51
- **NTP 同步**：首联网同步到 RTC，每 24 小时校准，检测篡改从 RTC 恢复
- **数据结构**：`SensorData { float temperature; float humidity; bool valid; }`

### 5.2 WeatherManager

- **当前模式**：MCP 回写（不主动请求和风 API）
- **`updateFromExternal(city, text, temp, update_time)`**：由 AI 调用 `self.weather.update` 写入
- **`update()`**：走和风天气 API（IP 定位城市 + 天气请求，当前在 `RefreshAllData` 中被注释禁用）
- **数据结构**：`WeatherData { string city; string temp; string text; string update_time; bool valid; }`

### 5.3 SdcardManager

- **接口**：SDMMC 模式（非 SPI）
- **挂载点**：`/sdcard`
- **白噪音目录**：`/sdcard/white-noise/`（支持 `.mp3`）
- **`listFiles(dir, ext)`**：扫描指定目录下文件

### 5.4 PomodoroManager

- **状态**：`IDLE` / `COUNTING` / `PAUSED`
- **任务**：`PomodoroTask`（倒计时）+ `WhiteNoiseTask`（白噪音循环播放）
- **原子变量**：`remaining_seconds_` / `total_seconds_` / `stop_requested_` / `noise_stop_requested_`
- **默认时长**：25 分钟
- **白噪音**：扫描 SD 卡多个文件随机选一个循环播放

---

## 6. UI 文件分工

| 文件 | 职责 |
|------|------|
| `custom_lcd_display.cc` | 核心类：构造/析构/flush 回调/AI 适配/备忘录/基类重写 |
| `weather_ui.cc` | 天气页布局（248×128 时钟 + 130×128 日历天气 + 252×122 AI + 126×122 备忘录）|
| `music_ui.cc` | 音乐页布局（歌名/歌手/三行歌词/进度条）|
| `pomodoro_ui.cc` | 番茄钟布局（状态文字 + 大号倒计时 + 进度条）|
| `data_update_task.cc` | 后台 FreeRTOS 任务，周期更新时间/天气/传感器/电池/WiFi/AI 状态/番茄钟 |

---

## 7. MCP 工具集

`InitializeTools` 注册 12 个 MCP 工具，分四组：

### 7.1 系统工具

| 工具 | 参数 | 用途 |
|------|------|------|
| `self.system.info` | 无 | 返回 CPU/内存/电池/WiFi 状态（自然语言格式）|
| `self.disp.network` | 无 | 进入 WiFi 配网模式 |
| `self.disp.switch` | `mode`（toggle/music/weather/pomodoro）| 切换屏幕页面 |

### 7.2 天气工具

| 工具 | 参数 | 用途 |
|------|------|------|
| `self.weather.update` | `city`/`text`/`temp`/`update_time` | AI 查完天气后回写到设备屏幕 |

### 7.3 番茄钟工具

| 工具 | 参数 | 用途 |
|------|------|------|
| `self.pomodoro.start` | `minutes`(1-120) / `white_noise`(bool) | 启动倒计时 + 白噪音，自动切到番茄钟页 |
| `self.pomodoro.stop` | 无 | 停止并切回天气页 |
| `self.pomodoro.status` | 无 | 查询状态 + 剩余时间 |
| `self.pomodoro.pause` | 无 | 暂停/恢复 |

### 7.4 备忘录工具（NVS 持久化）

| 工具 | 参数 | 用途 |
|------|------|------|
| `self.memo.add` | `content`/`time`(HH:MM) | 添加备忘（最多 10 条），存到 `Settings("memo").items` JSON 数组 |
| `self.memo.list` | 无 | 列出所有备忘 |
| `self.memo.done` | `index`(1-10) | 按序号完成/删除 |
| `self.memo.clear` | 无 | 清空所有 |

`IsValidMemoTimeLabel` 严格校验 HH:MM 24 小时制格式，拒绝"5分钟后"等自然语言。

备忘录 JSON 格式：
```json
[
  {"t": "15:00", "c": "开会"},
  {"t": "", "c": "买牛奶"}
]
```

---

## 8. 电池读取算法

不依赖 PMIC，纯 ADC + 软件滤波：

### 8.1 `BatterygetVoltage`

- ADC1_CH3 + `adc_cali_curve_fitting` 校准
- 电压 ×3（分压比 3:1）
- 静态变量缓存初始化结果，避免重复创建

### 8.2 `BatterygetPercent` 三层滤波

1. **10 次采样取平均**（减少瞬时噪声）
2. **EMA 指数移动平均**（`alpha=0.1`，约 10 次采样才跟上真实变化）
   - 公式：`ema = alpha * current + (1-alpha) * history`
3. **抛物线拟合**（电压 → 百分比）
   - 公式：`percent = (-V² + 9016V - 19189000) / 10000`

### 8.3 映射点

| 电压 | 电量 |
|------|------|
| 4200mV | 100% |
| 3700mV | 50% |
| 3300mV | 0% |

### 8.4 `GetBatteryLevel` 接口

```cpp
virtual bool GetBatteryLevel(int &level, bool& charging, bool& discharging) override {
    charging = false;
    discharging = !charging;
    level = (int)BatterygetPercent();
    return true;
}
```

注意：当前实现中 `charging` 恒为 false（无充电状态检测引脚），仅靠 ADC 电压估算。

---

## 9. 按键交互

| 按钮 | 操作 | 功能 |
|------|------|------|
| BOOT (GPIO0) | 单击 | 启动时进配网 / 运行时 Toggle AI 对话 |
| USER (GPIO18) | 单击 | `CycleDisplayMode` 切换页面 |
| USER (GPIO18) | 双击 | `RefreshAllData` 刷新 NTP + 传感器 |
| USER (GPIO18) | 长按 | `ShowSystemInfo` 鱼咬尾滚动系统信息 |

所有按键操作都会调用 `NotifyUserActivity()` 唤醒省电模式。

### 9.1 系统信息显示内容

长按 USER 显示：
- CPU 频率（240MHz）
- 运行时间（小时:分钟）
- SRAM 使用情况（已用/总量/百分比）
- PSRAM 使用情况（已用/总量/百分比）
- 电池电量和充电状态
- WiFi 连接状态

---

## 10. 音频配置

`GetAudioCodec` 使用 `BoxAudioCodec`：

```cpp
static BoxAudioCodec audio_codec(
    i2c_bus_,
    AUDIO_INPUT_SAMPLE_RATE,     // 24000
    AUDIO_OUTPUT_SAMPLE_RATE,    // 24000
    AUDIO_I2S_GPIO_MCLK,        // GPIO16
    AUDIO_I2S_GPIO_BCLK,        // GPIO9
    AUDIO_I2S_GPIO_WS,          // GPIO45
    AUDIO_I2S_GPIO_DOUT,        // GPIO8
    AUDIO_I2S_GPIO_DIN,         // GPIO10
    AUDIO_CODEC_PA_PIN,          // GPIO46
    AUDIO_CODEC_ES8311_ADDR,
    AUDIO_CODEC_ES7210_ADDR,
    AUDIO_INPUT_REFERENCE);     // true
```

- ES8311（输出，喇叭）+ ES7210（输入，麦克风）
- 24kHz 双向采样率
- 启用 `AUDIO_INPUT_REFERENCE=true`（硬件 AEC 参考路）
- PA 引脚 GPIO46
- 与 `CONFIG_USE_DEVICE_AEC=y` 配合实现设备端 AEC

---

## 11. 资源文件

### 11.1 字体（`assets/fonts/`）

4 套阿里字体（C 数组）：

| 文件 | 用途 |
|------|------|
| `alibaba_puhui_16.c` | 16px 普通（状态栏、备忘录）|
| `alibaba_puhui_24.c` | 24px 普通（AI 对话、天气文本）|
| `alibaba_puhui_48.c` | 48px 普通（番茄钟倒计时）|
| `alibaba_black_64.c` | 64px 粗体（大字时钟）|

### 11.2 图标（`assets/icons/`）

7 个 1-bit 单色图标 C 数组（替代 Font Awesome 文字）：

| 文件 | 用途 |
|------|------|
| `ui_img_wifi.c` | WiFi 信号强 |
| `ui_img_wifi_low.c` | WiFi 信号弱 |
| `ui_img_wifi_off.c` | WiFi 断开 |
| `ui_img_battery_charging.c` | 电池充电中 |
| `ui_img_battery_full.c` | 电池满 |
| `ui_img_battery_medium.c` | 电池中 |
| `ui_img_battery_low.c` | 电池低 |

---

## 12. 初始化顺序

`CustomBoard` 构造函数执行顺序：

```
InitializeI2c        → I2C 主总线（SDA=13/SCL=14）
InitializeSensors    → SensorManager（SHTC3 + PCF85063，依赖 I2C）
InitializeSdcard     → SdcardManager（SDMMC 挂载 /sdcard）
InitializeButtons    → BOOT + USER 按键回调注册
InitializeTools      → 12 个 MCP 工具注册
InitializeLcdDisplay → CustomLcdDisplay + StartDataUpdateTask
```

---

## 13. 关键设计亮点

1. **1-bit 单色屏适配**：自研 `RlcdDriver` + LUT 加速 + RGB565 二值化 flush 回调，是该仓库唯一实现
2. **多功能扩展**：从纯 AI 对话设备扩展为天气站+番茄钟+备忘录+音乐播放器，所有功能通过 MCP 工具暴露给大模型
3. **AI 天气回写**：放弃板载和风 API，改为 AI 侧查完天气后通过 `self.weather.update` MCP 工具下发，架构更灵活
4. **省电策略**：5 分钟无活动降频刷新（1s→5s），所有用户操作重置计时器
5. **电池滤波**：10 次采样 + EMA + 抛物线拟合，纯软件实现无 PMIC 依赖
6. **备忘录持久化**：NVS 存储 JSON 数组，重启不丢失
7. **鱼咬尾动画**：双份内容拼接 + Y 轴位移实现无缝循环滚动
8. **单色屏主题规避**：`SetTheme` 空实现避免基类操作不存在控件崩溃
9. **LUT 性能优化**：像素映射查找表把 RGB565→1-bit 转换从浮点运算降到查表+位运算
10. **图片图标替代文字**：单色屏 Font Awesome 字体渲染问题，改用 `lv_image` 图片图标

---

## 14. 文件路径索引

### 14.1 核心文件

| 文件 | 用途 |
|------|------|
| `main/boards/waveshare-s3-rlcd-4.2/waveshare-s3-rlcd-4.2.cc` | 板子主实现（CustomBoard）|
| `main/boards/waveshare-s3-rlcd-4.2/config.h` | 引脚定义 |
| `main/boards/waveshare-s3-rlcd-4.2/config.json` | 构建配置（AEC 启用）|
| `main/boards/waveshare-s3-rlcd-4.2/secret_config.h.example` | 私密配置示例 |

### 14.2 显示驱动

| 文件 | 用途 |
|------|------|
| `main/boards/waveshare-s3-rlcd-4.2/rlcd_driver.h` / `.cc` | RLCD 自研驱动 |
| `main/boards/waveshare-s3-rlcd-4.2/custom_lcd_display.h` / `.cc` | 自定义显示核心类 |

### 14.3 UI 布局

| 文件 | 用途 |
|------|------|
| `main/boards/waveshare-s3-rlcd-4.2/weather_ui.cc` | 天气页布局 |
| `main/boards/waveshare-s3-rlcd-4.2/music_ui.cc` | 音乐页布局 |
| `main/boards/waveshare-s3-rlcd-4.2/pomodoro_ui.cc` | 番茄钟布局 |
| `main/boards/waveshare-s3-rlcd-4.2/data_update_task.cc` | 后台数据更新任务 |

### 14.4 Managers

| 文件 | 用途 |
|------|------|
| `main/boards/waveshare-s3-rlcd-4.2/managers/sensor_manager.h` / `.cc` | 温湿度 + RTC |
| `main/boards/waveshare-s3-rlcd-4.2/managers/weather_manager.h` / `.cc` | 天气管理 |
| `main/boards/waveshare-s3-rlcd-4.2/managers/sdcard_manager.h` / `.cc` | SD 卡管理 |
| `main/boards/waveshare-s3-rlcd-4.2/managers/pomodoro_manager.h` / `.cc` | 番茄钟管理 |

### 14.5 资源

| 目录 | 用途 |
|------|------|
| `main/boards/waveshare-s3-rlcd-4.2/assets/fonts/` | 4 套阿里字体 |
| `main/boards/waveshare-s3-rlcd-4.2/assets/icons/` | 7 个单色图标 |

### 14.6 文档

| 文件 | 用途 |
|------|------|
| `main/boards/waveshare-s3-rlcd-4.2/README.md` | 板子完整说明文档 |

---

## 文档说明

- 本文档基于源码静态分析生成，未实际编译/烧录验证
- 涉及固件版本 v2.2.2，后续版本可能存在差异
- 板级实现细节以 `main/boards/waveshare-s3-rlcd-4.2/` 实际代码为准
- 如需深入特定模块，可参考第 14 章文件路径索引直接定位源码
