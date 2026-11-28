// CustomLcdDisplay 核心类
//
// 负责：
// - 构造/析构（初始化 RLCD 驱动 + LVGL）
// - SetupUI() 创建首页与无线投屏等待页 UI，并启动后台数据更新任务
// - LVGL flush 回调（RGB565 → 1-bit 转换，投屏激活态自动旁路保护）
// - AI 消息适配（重写 SetChatMessage / SetEmotion / ClearChatMessages / SetTheme）
// - 备忘录加载/刷新与双页切换控制
//
// 其他功能拆分到独立文件：
//   rlcd_driver.cc        - RLCD 硬件驱动
//   weather_ui.cc          - 首页 UI 布局
//   cast_ui.cc             - 无线投屏等待页 UI 布局
//   data_update_task.cc    - 后台数据更新任务

#include <vector>
#include <string>
#include <cstring>
#include <cJSON.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <esp_log.h>
#include <esp_err.h>
#include <esp_heap_caps.h>
#include "custom_lcd_display.h"
#include "lcd_display.h"
#include "esp_lvgl_port.h"
#include "settings.h"
#include "config.h"
#include "board.h"
#include "application.h"
#include "lvgl_theme.h"
#include "lvgl_font.h"

static const char *TAG = "CustomDisplay";

// ===== LVGL flush 回调 =====

void CustomLcdDisplay::Lvgl_flush_cb(lv_display_t * disp, const lv_area_t * area, uint8_t * color_p)
{
    assert(disp != NULL);
    CustomLcdDisplay *self = (CustomLcdDisplay *)lv_display_get_user_data(disp);
    // 无线投屏激活态时跳过 LVGL 刷屏，防止后台状态更新覆盖投屏图像
    if (self->display_mode_.load() == MODE_CAST && self->cast_streaming_.load()) {
        lv_disp_flush_ready(disp);
        return;
    }
    RlcdDriver *rlcd = self->rlcd_;
    uint16_t *buffer = (uint16_t *)color_p;
    for (int y = area->y1; y <= area->y2; y++) {
        for (int x = area->x1; x <= area->x2; x++) {
            uint8_t color = (*buffer < 0x7fff) ? ColorBlack : ColorWhite;
            rlcd->RLCD_SetPixel(x, y, color);
            buffer++;
        }
    }
    rlcd->RLCD_Display();
    lv_disp_flush_ready(disp);
}

// ===== 构造 / 析构 / SetupUI =====

CustomLcdDisplay::CustomLcdDisplay(esp_lcd_panel_io_handle_t panel_io,
    esp_lcd_panel_handle_t panel,
    int width, int height, int offset_x, int offset_y,
    bool mirror_x, bool mirror_y, bool swap_xy,
    spi_display_config_t spiconfig,
    spi_host_device_t spi_host) : LcdDisplay(panel_io, panel, width, height)
{
    // 1. 初始化 RLCD 硬件驱动
    rlcd_ = new RlcdDriver(spiconfig, width, height, spi_host);

    // 2. 初始化 LVGL
    ESP_LOGI(TAG, "初始化 LVGL");
    lv_init();
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    port_cfg.task_priority = 2;
    port_cfg.timer_period_ms = 50;
    lvgl_port_init(&port_cfg);
    lvgl_port_lock(0);

    int transfer = width * height;
    display_ = lv_display_create(width, height);
    lv_display_set_flush_cb(display_, Lvgl_flush_cb);
    lv_display_set_user_data(display_, this);
    size_t lvgl_buffer_size = LV_COLOR_FORMAT_GET_SIZE(LV_COLOR_FORMAT_RGB565) * transfer;
    uint8_t *lvgl_buffer1 = (uint8_t *)heap_caps_malloc(lvgl_buffer_size, MALLOC_CAP_SPIRAM);
    assert(lvgl_buffer1);
    lv_display_set_buffers(display_, lvgl_buffer1, NULL, lvgl_buffer_size, LV_DISPLAY_RENDER_MODE_PARTIAL);

    // 3. 初始化 RLCD 屏幕并分配 1-bit 投屏帧缓冲
    ESP_LOGI(TAG, "初始化 RLCD 屏幕");
    rlcd_->RLCD_Init();
    cast_frame_size_ = static_cast<size_t>(transfer >> 3);
    cast_frame_buf_ = static_cast<uint8_t *>(heap_caps_malloc(cast_frame_size_, MALLOC_CAP_SPIRAM));
    assert(cast_frame_buf_);
    memset(cast_frame_buf_, ColorWhite, cast_frame_size_);

    lvgl_port_unlock();
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "显示初始化失败");
        return;
    }
}

void CustomLcdDisplay::SetupUI() {
    if (setup_ui_called_) {
        return;
    }
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "LVGL display 未就绪，跳过 SetupUI");
        return;
    }

    ESP_LOGI(TAG, "创建首页与无线投屏页 UI");
    SetupWeatherUI();
    SetupCastUI();
    setup_ui_called_ = true;

    if (current_theme_ == nullptr) {
        current_theme_ = LvglThemeManager::GetInstance().GetTheme("light");
    }
    ApplyDisplayMode();
    LoadMemoFromNvs();
    StartDataUpdateTask();
}

CustomLcdDisplay::~CustomLcdDisplay() {
    if (update_task_handle_) {
        vTaskDelete(update_task_handle_);
    }
    if (cast_frame_buf_) {
        heap_caps_free(cast_frame_buf_);
        cast_frame_buf_ = nullptr;
    }
    delete rlcd_;
}

// ===== 备忘录功能 =====

void CustomLcdDisplay::LoadMemoFromNvs() {
    RefreshMemoDisplay();
}

void CustomLcdDisplay::RefreshMemoDisplayInternal() {
    if (!memo_list_label_) return;

    Settings settings("memo", false);
    std::string json_str = settings.GetString("items", "");

    if (json_str.empty()) {
        lv_label_set_text(memo_list_label_, "暂无待办");
        return;
    }

    cJSON *arr = cJSON_Parse(json_str.c_str());
    if (!arr || !cJSON_IsArray(arr)) {
        lv_label_set_text(memo_list_label_, "暂无待办");
        if (arr) cJSON_Delete(arr);
        return;
    }

    std::string display_text;
    int count = cJSON_GetArraySize(arr);
    for (int i = 0; i < count && i < 5; i++) {
        cJSON *item = cJSON_GetArrayItem(arr, i);
        cJSON *t = cJSON_GetObjectItem(item, "t");
        cJSON *c = cJSON_GetObjectItem(item, "c");

        if (i > 0) display_text += "\n";

        if (t && cJSON_IsString(t) && strlen(t->valuestring) > 0) {
            display_text += t->valuestring;
            display_text += " ";
        } else {
            display_text += "· ";
        }
        if (c && cJSON_IsString(c)) {
            display_text += c->valuestring;
        }
    }

    if (count > 5) {
        display_text += "\n...还有" + std::to_string(count - 5) + "条";
    }

    cJSON_Delete(arr);
    lv_label_set_text(memo_list_label_, display_text.c_str());
    ESP_LOGI(TAG, "备忘列表已刷新，共 %d 条", count);
}

void CustomLcdDisplay::RefreshMemoDisplay() {
    DisplayLockGuard lock(this);
    RefreshMemoDisplayInternal();
}

// ===== AI 消息适配 =====

void CustomLcdDisplay::SetChatMessage(const char* role, const char* content) {
    DisplayLockGuard lock(this);
    if (chat_status_label_ == nullptr) return;
    if (!content || strlen(content) == 0) return;

    lv_anim_delete(chat_status_label_, nullptr);
    SetShowingSystemInfo(false);

    lv_label_set_text(chat_status_label_, content);
    lv_label_set_long_mode(chat_status_label_, LV_LABEL_LONG_WRAP);
    lv_obj_align(chat_status_label_, LV_ALIGN_LEFT_MID, 64 + 20, 0);

    lv_obj_update_layout(chat_status_label_);
    int label_h = lv_obj_get_height(chat_status_label_);
    lv_obj_t *parent = lv_obj_get_parent(chat_status_label_);
    int visible_h = parent ? lv_obj_get_content_height(parent) : 108;

    if (label_h > visible_h) {
        const int text_x = 64 + 20;
        lv_obj_align(chat_status_label_, LV_ALIGN_TOP_LEFT, text_x, 0);

        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, chat_status_label_);
        lv_anim_set_values(&a, 0, -(label_h - visible_h));
        lv_anim_set_delay(&a, 1500);
        lv_anim_set_duration(&a, (label_h - visible_h) * 50);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_set_repeat_delay(&a, 2000);
        lv_anim_set_exec_cb(&a, [](void *obj, int32_t v) {
            lv_obj_set_y((lv_obj_t *)obj, v);
        });
        lv_anim_start(&a);
    }
}

bool CustomLcdDisplay::SetEmotion(const char* emotion) {
    DisplayLockGuard lock(this);
    const char* key = emotion ? emotion : "neutral";

    const char* text = "待命";
    if (strcmp(key, "neutral") == 0)         text = "待命";
    else if (strcmp(key, "happy") == 0)      text = "开心";
    else if (strcmp(key, "laughing") == 0)   text = "大笑";
    else if (strcmp(key, "funny") == 0)      text = "搞笑";
    else if (strcmp(key, "sad") == 0)        text = "难过";
    else if (strcmp(key, "angry") == 0)      text = "生气";
    else if (strcmp(key, "crying") == 0)     text = "哭泣";
    else if (strcmp(key, "loving") == 0)     text = "喜爱";
    else if (strcmp(key, "embarrassed") == 0) text = "害羞";
    else if (strcmp(key, "surprised") == 0)  text = "惊讶";
    else if (strcmp(key, "shocked") == 0)    text = "震惊";
    else if (strcmp(key, "thinking") == 0)   text = "思考";
    else if (strcmp(key, "winking") == 0)    text = "眨眼";
    else if (strcmp(key, "cool") == 0)       text = "耍酷";
    else if (strcmp(key, "relaxed") == 0)    text = "放松";
    else if (strcmp(key, "delicious") == 0)  text = "好吃";
    else if (strcmp(key, "kissy") == 0)      text = "亲亲";
    else if (strcmp(key, "confident") == 0)  text = "自信";
    else if (strcmp(key, "sleepy") == 0)     text = "犯困";
    else if (strcmp(key, "silly") == 0)      text = "调皮";
    else if (strcmp(key, "confused") == 0)   text = "困惑";
    else if (strcmp(key, "fear") == 0)       text = "害怕";
    else if (strcmp(key, "disgusted") == 0)  text = "嫌弃";
    else if (strcmp(key, "microchip_ai") == 0) text = "就绪";

    if (emotion_label_) {
        lv_label_set_text(emotion_label_, text);
    }

    if (current_theme_) {
        auto emoji_collection = static_cast<LvglTheme*>(current_theme_)->emoji_collection();
        auto image = emoji_collection ? emoji_collection->GetEmojiImage(key) : nullptr;
        bool has_image = (image && !image->IsGif());

        if (emotion_img_) {
            if (has_image) {
                lv_image_set_src(emotion_img_, image->image_dsc());
                lv_obj_remove_flag(emotion_img_, LV_OBJ_FLAG_HIDDEN);
            } else {
                lv_obj_add_flag(emotion_img_, LV_OBJ_FLAG_HIDDEN);
            }
        }
    }
    return true;
}

void CustomLcdDisplay::ClearChatMessages() {
    DisplayLockGuard lock(this);
    if (chat_status_label_) lv_label_set_text(chat_status_label_, "");
}

void CustomLcdDisplay::UpdateStatusBar(bool update_all) {
    // 自定义状态栏图片图标由 DataUpdateTask 统一管理，跳过基类文字图标刷新
    (void)update_all;
}

void CustomLcdDisplay::SetTheme(Theme* theme) {
    // RLCD 单色屏不切换配色主题，但保存 theme 并同步挂载的中文字库到中文标签
    current_theme_ = theme;
    if (!theme) {
        return;
    }

    auto* lvgl_theme = static_cast<LvglTheme*>(theme);
    auto font_ptr = lvgl_theme->text_font();
    const lv_font_t* text_font = font_ptr ? font_ptr->font() : nullptr;
    if (text_font) {
        DisplayLockGuard lock(this);
        lv_obj_t* labels[] = {
            emotion_label_,
            chat_status_label_,
            memo_list_label_,
            low_battery_label_,
            weather_label_,
            cast_title_label_,
            cast_status_label_,
            cast_stats_label_,
            cast_hint_label_,
        };
        for (lv_obj_t* lbl : labels) {
            if (lbl) {
                lv_obj_set_style_text_font(lbl, text_font, 0);
            }
        }
    }
    ESP_LOGI(TAG, "RLCD 单色屏主题与中文字体已同步");
}

// ===== 页面切换与无线投屏控制 =====

void CustomLcdDisplay::ApplyDisplayMode(bool allow_cast_blit) {
    if (weather_page_) lv_obj_add_flag(weather_page_, LV_OBJ_FLAG_HIDDEN);
    if (cast_page_) lv_obj_add_flag(cast_page_, LV_OBJ_FLAG_HIDDEN);

    switch (display_mode_.load()) {
        case MODE_WEATHER:
            if (weather_page_) lv_obj_remove_flag(weather_page_, LV_OBJ_FLAG_HIDDEN);
            break;
        case MODE_CAST:
            if (cast_page_) lv_obj_remove_flag(cast_page_, LV_OBJ_FLAG_HIDDEN);
            if (cast_streaming_.load() && cast_has_frame_ && rlcd_ && cast_frame_buf_) {
                if (allow_cast_blit) {
                    rlcd_->RLCD_DisplayMono1Bit(cast_frame_buf_, cast_frame_size_);
                    cast_pending_blit_ = false;
                } else {
                    // 处于语音保护阶段时只缓存待刷标志，通话结束后补刷
                    cast_pending_blit_ = true;
                }
            }
            break;
    }
}

void CustomLcdDisplay::CycleDisplayMode() {
    DisplayLockGuard lock(this);
    // 双页循环：首页 ↔ 无线投屏
    if (display_mode_.load() == MODE_WEATHER) {
        display_mode_.store(MODE_CAST);
    } else {
        display_mode_.store(MODE_WEATHER);
    }
    ApplyDisplayMode();
    ESP_LOGI(TAG, "页面切换: %s", GetDisplayModeName());
}

const char* CustomLcdDisplay::GetDisplayModeName() const {
    switch (display_mode_.load()) {
        case MODE_WEATHER:  return "weather";
        case MODE_CAST:     return "cast";
        default:            return "unknown";
    }
}

void CustomLcdDisplay::SwitchToWeatherPage() {
    DisplayLockGuard lock(this);
    if (display_mode_.load() != MODE_WEATHER) {
        display_mode_.store(MODE_WEATHER);
        ApplyDisplayMode();
        ESP_LOGI(TAG, "切换到小智首页");
    }
}

void CustomLcdDisplay::SwitchToCastPage() {
    DisplayLockGuard lock(this);
    if (display_mode_.load() != MODE_CAST) {
        display_mode_.store(MODE_CAST);
        ApplyDisplayMode();
        ESP_LOGI(TAG, "切换到无线投屏页");
    }
}

bool CustomLcdDisplay::UpdateCastFrame(const uint8_t* mono_buf, size_t len, bool auto_switch, bool allow_blit) {
    if (!mono_buf || !cast_frame_buf_ || len != cast_frame_size_) {
        return false;
    }

    DisplayLockGuard lock(this);
    memcpy(cast_frame_buf_, mono_buf, len);
    cast_has_frame_ = true;
    cast_streaming_.store(true);

    if (auto_switch && display_mode_.load() != MODE_CAST) {
        display_mode_.store(MODE_CAST);
        // 切页时统一由下方逻辑受 allow_blit 约束决定立即刷屏还是暂缓
        ApplyDisplayMode(false);
    }

    if (display_mode_.load() == MODE_CAST) {
        if (allow_blit && rlcd_) {
            cast_pending_blit_ = false;
            return rlcd_->RLCD_DisplayMono1Bit(cast_frame_buf_, cast_frame_size_);
        }
        cast_pending_blit_ = true;
    }
    return false;
}

void CustomLcdDisplay::SetCastStreaming(bool active) {
    DisplayLockGuard lock(this);
    const bool prev = cast_streaming_.exchange(active);
    if (!active) {
        cast_pending_blit_ = false;
        if (prev && display_mode_.load() == MODE_CAST && cast_page_) {
            lv_obj_invalidate(lv_screen_active());
        }
    } else if (!prev && display_mode_.load() == MODE_CAST && cast_has_frame_ && rlcd_ && cast_frame_buf_) {
        rlcd_->RLCD_DisplayMono1Bit(cast_frame_buf_, cast_frame_size_);
        cast_pending_blit_ = false;
    }
}

bool CustomLcdDisplay::FlushBufferedCastFrameIfNeeded() {
    DisplayLockGuard lock(this);
    if (display_mode_.load() == MODE_CAST && cast_streaming_.load() && cast_has_frame_ &&
        cast_pending_blit_ && rlcd_ && cast_frame_buf_) {
        cast_pending_blit_ = false;
        return rlcd_->RLCD_DisplayMono1Bit(cast_frame_buf_, cast_frame_size_);
    }
    return false;
}

void CustomLcdDisplay::UpdateCastWaitingInfo(const char* ip_text, const char* status_text, const char* stats_text) {
    DisplayLockGuard lock(this);
    if (cast_ip_label_ && ip_text && last_cast_ip_text_ != ip_text) {
        lv_label_set_text(cast_ip_label_, ip_text);
        last_cast_ip_text_ = ip_text;
    }
    if (cast_status_label_ && status_text && last_cast_status_text_ != status_text) {
        lv_label_set_text(cast_status_label_, status_text);
        last_cast_status_text_ = status_text;
    }
    if (cast_stats_label_ && stats_text && last_cast_stats_text_ != stats_text) {
        lv_label_set_text(cast_stats_label_, stats_text);
        last_cast_stats_text_ = stats_text;
    }
}

// ===== 省电模式 =====

void CustomLcdDisplay::NotifyUserActivity() {
    last_activity_ms_ = xTaskGetTickCount() * portTICK_PERIOD_MS;
    if (power_saving_) {
        power_saving_ = false;
        ESP_LOGI(TAG, "用户活动检测到，退出省电模式");
    }
}
