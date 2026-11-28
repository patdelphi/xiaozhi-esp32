#ifndef __CUSTOM_LCD_DISPLAY_H__
#define __CUSTOM_LCD_DISPLAY_H__

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <driver/gpio.h>
#include "lcd_display.h"
#include "rlcd_driver.h"
#include "managers/sensor_manager.h"
#include "managers/weather_manager.h"

// 天气站首页 + AI 混合显示 / 无线投屏双页显示
//
// 屏幕布局 (400x300, 1-bit 单色 RLCD)：
// ┌──────────────────┬──────────────────┐
// │   时钟卡片(248x128) │  日历卡片(130x128) │
// │    "14:30"        │   TUE / 15      │
// │                   │   晴 25°C       │
// ├──────────────────┼──────────────────┤
// │   AI 对话(252x122) │  备忘录(126x122)  │
// │  "聆听中..."      │   MEMO          │
// └──────────────────┴──────────────────┘
// 状态栏浮在右上角（WiFi + 电池 + 温湿度）
//
// 代码拆分为多个文件：
//   rlcd_driver.h/cc      - RLCD 硬件驱动层（SPI、像素映射、初始化命令）
//   weather_ui.cc          - 首页 UI 布局（SetupWeatherUI）
//   cast_ui.cc             - 无线投屏等待 UI 布局（SetupCastUI）
//   data_update_task.cc    - 后台数据更新任务（时间/天气/传感器/电池/WiFi/AI状态/投屏）
//   custom_lcd_display.cc  - 核心类（构造/析构/SetupUI/AI适配/备忘录/基类重写）
class CustomLcdDisplay : public LcdDisplay {
private:
    enum DisplayMode {
        MODE_WEATHER = 0,
        MODE_CAST = 1,
    };
    std::atomic<DisplayMode> display_mode_{MODE_WEATHER};

    // RLCD 硬件驱动（独立模块，负责 SPI 通信和像素操作）
    RlcdDriver *rlcd_ = nullptr;
    lv_obj_t *weather_page_ = nullptr;
    lv_obj_t *cast_page_ = nullptr;

    // ===== 首页 UI 组件 =====
    lv_obj_t *sensor_label_ = nullptr;      // 左上角温湿度标签
    lv_obj_t *time_label_ = nullptr;        // 时钟卡片大字时钟 "14:30"
    lv_obj_t *day_label_ = nullptr;         // 日历卡片星期 "TUE"
    lv_obj_t *date_num_label_ = nullptr;    // 日历卡片日期 "15"
    lv_obj_t *weather_label_ = nullptr;     // 日历卡片天气 "晴 25°C"

    // AI 对话卡片（左下）
    lv_obj_t *chat_card_ = nullptr;         // AI 卡片容器
    lv_obj_t *chat_status_label_ = nullptr; // AI 对话文字（右侧）
    lv_obj_t *emotion_label_ = nullptr;     // 表情文字（左侧下方）
    lv_obj_t *emotion_img_ = nullptr;       // 表情图片（左侧上方）

    // 备忘录卡片（右下）
    lv_obj_t *memo_list_label_ = nullptr;   // 多行备忘列表文字

    // ===== 无线投屏页 UI 组件与帧缓存 =====
    lv_obj_t *cast_time_label_ = nullptr;        // 顶部时钟
    lv_obj_t *cast_title_label_ = nullptr;       // 顶部标题 "无线投屏"
    lv_obj_t *cast_wifi_icon_img_ = nullptr;     // 状态栏 WiFi 图标
    lv_obj_t *cast_battery_icon_img_ = nullptr;  // 状态栏电池图标
    lv_obj_t *cast_battery_pct_label_ = nullptr; // 状态栏电量文字
    lv_obj_t *cast_status_label_ = nullptr;      // 等待状态提示
    lv_obj_t *cast_ip_label_ = nullptr;          // 本机 IPv4:端口 显示
    lv_obj_t *cast_info_label_ = nullptr;        // 接口提示
    lv_obj_t *cast_stats_label_ = nullptr;       // Wi-Fi 与帧统计文案
    lv_obj_t *cast_hint_label_ = nullptr;        // 底部按键提示

    uint8_t *cast_frame_buf_ = nullptr;          // PSRAM 最新 1-bit 投屏帧缓冲（15,000 B）
    size_t cast_frame_size_ = 0;
    std::atomic<bool> cast_streaming_{false};    // 是否处于已连线投屏显示态
    bool cast_has_frame_ = false;
    bool cast_pending_blit_ = false;
    std::string last_cast_ip_text_;
    std::string last_cast_status_text_;
    std::string last_cast_stats_text_;

    // 首页状态栏图片图标
    lv_obj_t *wifi_icon_img_ = nullptr;
    lv_obj_t *battery_icon_img_ = nullptr;
    lv_obj_t *battery_pct_label_ = nullptr;

    // 数据更新任务句柄
    TaskHandle_t update_task_handle_ = nullptr;

    // 系统信息滚动标志（为 true 时暂停 DataUpdateTask 更新，避免锁竞争）
    std::atomic<bool> showing_system_info_{false};

    // 省电模式：5 分钟无活动后降低刷新频率（1秒 → 5秒）
    std::atomic<bool> power_saving_{false};
    uint32_t last_activity_ms_ = 0;
    static const uint32_t IDLE_TIMEOUT_MS = 5 * 60 * 1000;
    static const int NORMAL_REFRESH_MS = 1000;
    static const int SAVING_REFRESH_MS = 5000;

    // 上次更新缓存值（避免无效重绘）
    int last_min_ = -1;
    time_t last_valid_epoch_ = 0;
    float last_temp_ = -99.0f;
    float last_humi_ = -99.0f;

    // LVGL flush 回调（RGB565 → 1-bit 刷新到 RLCD）
    static void Lvgl_flush_cb(lv_display_t * disp, const lv_area_t * area, uint8_t * color_p);

    // UI 创建（实现在 weather_ui.cc / cast_ui.cc）
    void SetupWeatherUI();
    void SetupCastUI();
    void ApplyDisplayMode(bool allow_cast_blit = true);

    // 备忘录
    void LoadMemoFromNvs();

    // 数据更新任务（实现在 data_update_task.cc）
    static void DataUpdateTask(void *arg);

public:
    CustomLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                     int width, int height, int offset_x, int offset_y,
                     bool mirror_x, bool mirror_y, bool swap_xy,
                     spi_display_config_t spiconfig, spi_host_device_t spi_host = SPI3_HOST);
    ~CustomLcdDisplay();

    // 获取 RLCD 驱动与 UI 标签
    RlcdDriver* rlcd() const { return rlcd_; }
    lv_obj_t* GetChatStatusLabel() const { return chat_status_label_; }
    void SetShowingSystemInfo(bool showing) { showing_system_info_ = showing; }

    // 省电模式：记录用户活动，唤醒省电模式
    void NotifyUserActivity();
    bool IsPowerSaving() const { return power_saving_; }

    // 基类生命周期与 AI 显示重写（适配 v2.6.0）
    virtual void SetupUI() override;
    virtual bool IsMonochrome() const override { return true; }
    virtual void SetChatMessage(const char* role, const char* content) override;
    virtual bool SetEmotion(const char* emotion) override;
    virtual void ClearChatMessages() override;
    virtual void UpdateStatusBar(bool update_all = false) override;
    virtual void SetTheme(Theme* theme) override;

    // 启动数据更新任务（在 SetupUI 中调用）
    void StartDataUpdateTask();

    // 备忘录列表刷新
    void RefreshMemoDisplay();
    void RefreshMemoDisplayInternal();

    // 首页与无线投屏双页切换
    void CycleDisplayMode();
    bool IsCastMode() const { return display_mode_.load() == MODE_CAST; }
    bool IsCastStreaming() const { return cast_streaming_.load(); }
    const char* GetDisplayModeName() const;
    void SwitchToWeatherPage();
    void SwitchToCastPage();

    // 无线投屏帧更新与等待页控制
    bool UpdateCastFrame(const uint8_t* mono_buf, size_t len, bool auto_switch, bool allow_blit);
    void SetCastStreaming(bool active);
    bool FlushBufferedCastFrameIfNeeded();
    void UpdateCastWaitingInfo(const char* ip_text, const char* status_text, const char* stats_text);
};

#endif
