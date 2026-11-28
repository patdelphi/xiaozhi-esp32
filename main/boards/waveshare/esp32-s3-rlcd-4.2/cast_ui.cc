// 程序说明：第四屏无线投屏等待页 UI 布局（400x300 黑白单色 RLCD，纯黑白高对比样式）
//
// 布局：
// ┌──────────────────────────────────────────┐
// │ 14:30    无线投屏      [WiFi][电池][85%] │  顶部状态栏
// │                                          │
// │  ┌────────────────────────────────────┐  │
// │  │             等待连接               │  │
// │  │         192.168.1.100:8080         │  │  白色信息卡片
// │  │  ────────────────────────────────  │  │
// │  │  POST /api/cast/frame · 400x300    │  │
// │  └────────────────────────────────────┘  │
// │                                          │
// │      Wi-Fi 已就绪 · 已接收 0 帧           │  底部状态提示
// │    短按 KEY 键切页 · 语音说「切到投屏页」  │
// └──────────────────────────────────────────┘

#include "custom_lcd_display.h"
#include <esp_log.h>

// 字体声明（数字/IP 与时间用 alibaba_puhui_24/16，中文标签随 SetTheme 升级为全量中文字库）
LV_FONT_DECLARE(alibaba_puhui_16);
LV_FONT_DECLARE(alibaba_puhui_24);
LV_FONT_DECLARE(BUILTIN_TEXT_FONT);

// 状态栏图标
LV_IMAGE_DECLARE(ui_img_wifi);
LV_IMAGE_DECLARE(ui_img_wifi_off);
LV_IMAGE_DECLARE(ui_img_battery_full);

static const char *TAG = "CastUI";

void CustomLcdDisplay::SetupCastUI() {
    DisplayLockGuard lock(this);

    lv_obj_t *root = lv_screen_active();
    const lv_font_t *font_num  = &alibaba_puhui_16;
    const lv_font_t *font_big  = &alibaba_puhui_24;
    const lv_font_t *font_cn   = &BUILTIN_TEXT_FONT;
    const lv_font_t *font_sm   = &BUILTIN_TEXT_FONT;

    const int SCR_W = 400;
    const int SCR_H = 300;
    const int PAD = 12;

    // ===== 无线投屏等待页容器（全屏纯黑背景，初始隐藏）=====
    cast_page_ = lv_obj_create(root);
    lv_obj_set_size(cast_page_, SCR_W, SCR_H);
    lv_obj_set_pos(cast_page_, 0, 0);
    lv_obj_set_style_bg_color(cast_page_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(cast_page_, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(cast_page_, 0, 0);
    lv_obj_set_style_pad_all(cast_page_, 0, 0);
    lv_obj_set_style_radius(cast_page_, 0, 0);
    lv_obj_remove_flag(cast_page_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(cast_page_, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *page = cast_page_;

    // ===== 第 1 层：顶部时间、标题与状态栏胶囊 =====
    cast_time_label_ = lv_label_create(page);
    lv_obj_set_style_text_font(cast_time_label_, font_big, 0);
    lv_obj_set_style_text_color(cast_time_label_, lv_color_white(), 0);
    lv_obj_align(cast_time_label_, LV_ALIGN_TOP_LEFT, 10, 5);
    lv_label_set_text(cast_time_label_, "00:00");

    cast_title_label_ = lv_label_create(page);
    lv_obj_set_style_text_font(cast_title_label_, font_cn, 0);
    lv_obj_set_style_text_color(cast_title_label_, lv_color_white(), 0);
    lv_obj_align(cast_title_label_, LV_ALIGN_TOP_LEFT, 86, 9);
    lv_label_set_text(cast_title_label_, "无线投屏");

    lv_obj_t *status_bar = lv_obj_create(page);
    lv_obj_set_size(status_bar, 115, 28);
    lv_obj_set_style_bg_opa(status_bar, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(status_bar, lv_color_white(), 0);
    lv_obj_set_style_border_width(status_bar, 0, 0);
    lv_obj_set_style_radius(status_bar, 14, 0);
    lv_obj_align(status_bar, LV_ALIGN_TOP_RIGHT, -8, 4);
    lv_obj_set_style_pad_left(status_bar, 8, 0);
    lv_obj_set_style_pad_right(status_bar, 8, 0);
    lv_obj_set_style_pad_column(status_bar, 5, 0);
    lv_obj_remove_flag(status_bar, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_flex_flow(status_bar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(status_bar, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

    cast_wifi_icon_img_ = lv_image_create(status_bar);
    lv_image_set_src(cast_wifi_icon_img_, &ui_img_wifi_off);
    cast_battery_icon_img_ = lv_image_create(status_bar);
    lv_image_set_src(cast_battery_icon_img_, &ui_img_battery_full);
    cast_battery_pct_label_ = lv_label_create(status_bar);
    lv_obj_set_style_text_font(cast_battery_pct_label_, font_num, 0);
    lv_obj_set_style_text_color(cast_battery_pct_label_, lv_color_black(), 0);
    lv_label_set_text(cast_battery_pct_label_, "---%");

    // ===== 第 2 层：中部白色圆角主卡片（展示等待状态与本机 IPv4）=====
    const int card_y = 54;
    const int card_w = SCR_W - PAD * 2;
    const int card_h = 166;

    lv_obj_t *main_card = lv_obj_create(page);
    lv_obj_set_size(main_card, card_w, card_h);
    lv_obj_set_pos(main_card, PAD, card_y);
    lv_obj_set_style_bg_color(main_card, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(main_card, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(main_card, 0, 0);
    lv_obj_set_style_radius(main_card, 16, 0);
    lv_obj_set_style_pad_all(main_card, 0, 0);
    lv_obj_remove_flag(main_card, LV_OBJ_FLAG_SCROLLABLE);

    cast_status_label_ = lv_label_create(main_card);
    lv_obj_set_style_text_font(cast_status_label_, font_cn, 0);
    lv_obj_set_style_text_color(cast_status_label_, lv_color_black(), 0);
    lv_obj_set_style_text_align(cast_status_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(cast_status_label_, card_w - 24);
    lv_label_set_text(cast_status_label_, "等待连接");
    lv_obj_align(cast_status_label_, LV_ALIGN_TOP_MID, 0, 22);

    cast_ip_label_ = lv_label_create(main_card);
    lv_obj_set_style_text_font(cast_ip_label_, font_big, 0);
    lv_obj_set_style_text_color(cast_ip_label_, lv_color_black(), 0);
    lv_obj_set_style_text_align(cast_ip_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(cast_ip_label_, card_w - 20);
    lv_label_set_long_mode(cast_ip_label_, LV_LABEL_LONG_DOT);
    lv_label_set_text(cast_ip_label_, "IPv4: --.--.--.--");
    lv_obj_align(cast_ip_label_, LV_ALIGN_TOP_MID, 0, 62);

    // 实心黑色分隔线（避免 50% 二值化阈值下淡色线条消失）
    lv_obj_t *divider = lv_obj_create(main_card);
    lv_obj_set_size(divider, card_w - 48, 2);
    lv_obj_set_style_bg_color(divider, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(divider, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(divider, 0, 0);
    lv_obj_set_style_radius(divider, 0, 0);
    lv_obj_align(divider, LV_ALIGN_TOP_MID, 0, 108);
    lv_obj_remove_flag(divider, LV_OBJ_FLAG_SCROLLABLE);

    cast_info_label_ = lv_label_create(main_card);
    lv_obj_set_style_text_font(cast_info_label_, font_sm, 0);
    lv_obj_set_style_text_color(cast_info_label_, lv_color_black(), 0);
    lv_obj_set_style_text_align(cast_info_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(cast_info_label_, card_w - 20);
    lv_label_set_text(cast_info_label_, "POST /api/cast/frame  |  400x300 1-bit");
    lv_obj_align(cast_info_label_, LV_ALIGN_TOP_MID, 0, 126);

    // ===== 第 3 层：底部状态与操作提示（纯黑底 + 纯白实字）=====
    cast_stats_label_ = lv_label_create(page);
    lv_obj_set_style_text_font(cast_stats_label_, font_sm, 0);
    lv_obj_set_style_text_color(cast_stats_label_, lv_color_white(), 0);
    lv_obj_set_style_text_align(cast_stats_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(cast_stats_label_, SCR_W - 20);
    lv_label_set_text(cast_stats_label_, "Wi-Fi 连接中 · 已接收 0 帧");
    lv_obj_align(cast_stats_label_, LV_ALIGN_BOTTOM_MID, 0, -40);

    cast_hint_label_ = lv_label_create(page);
    lv_obj_set_style_text_font(cast_hint_label_, font_sm, 0);
    lv_obj_set_style_text_color(cast_hint_label_, lv_color_white(), 0);
    lv_obj_set_style_text_align(cast_hint_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(cast_hint_label_, SCR_W - 20);
    lv_label_set_text(cast_hint_label_, "短按 KEY 键切页 · 语音说「切到投屏页」");
    lv_obj_align(cast_hint_label_, LV_ALIGN_BOTTOM_MID, 0, -16);

    ESP_LOGI(TAG, "无线投屏等待页面 UI 创建完成");
}
