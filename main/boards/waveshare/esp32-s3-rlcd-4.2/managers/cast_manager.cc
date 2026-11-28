/*
 * 程序说明：Waveshare ESP32-S3-RLCD-4.2 板载无线投屏 HTTP 接收管理器实现。
 */

#include "cast_manager.h"

#include <cstdlib>
#include <cstring>
#include <cJSON.h>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <wifi_manager.h>

#include "application.h"
#include "board.h"
#include "custom_lcd_display.h"

#if __has_include("secret_config.h")
#include "secret_config.h"
#endif

#ifndef CAST_AUTH_TOKEN
#define CAST_AUTH_TOKEN ""
#endif

CastManager::~CastManager() {
    stop();
}

bool CastManager::start(CustomLcdDisplay* display, uint16_t port) {
    if (running_.load()) {
        return true;
    }
    if (!display) {
        ESP_LOGE(TAG, "启动失败：display 指针为空");
        return false;
    }

    display_ = display;
    port_ = port;

    // 从 PSRAM 分配 15 KB 接收帧缓冲，避免占用宝贵内部 SRAM
    if (!rx_buf_) {
        rx_buf_ = static_cast<uint8_t*>(heap_caps_malloc(kFrameBytes, MALLOC_CAP_SPIRAM));
        if (!rx_buf_) {
            ESP_LOGE(TAG, "分配投屏 PSRAM 接收缓冲失败");
            return false;
        }
    }

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = port_;
    config.ctrl_port = 32769;  // 避免与配网 AP HTTP 服务默认控制端口冲突
    config.stack_size = 6144;
    config.task_priority = 4;
    config.max_uri_handlers = 8;
    config.lru_purge_enable = true;
    config.recv_wait_timeout = 5;
    config.send_wait_timeout = 5;

    esp_err_t err = httpd_start(&server_, &config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "启动投屏 HTTP 服务失败 (port=%u): %s", port_, esp_err_to_name(err));
        heap_caps_free(rx_buf_);
        rx_buf_ = nullptr;
        server_ = nullptr;
        return false;
    }

    const httpd_uri_t frame_uri = {
        .uri = "/api/cast/frame",
        .method = HTTP_POST,
        .handler = HandleFramePost,
        .user_ctx = this,
    };
    const httpd_uri_t hb_uri = {
        .uri = "/api/cast/heartbeat",
        .method = HTTP_POST,
        .handler = HandleHeartbeatPost,
        .user_ctx = this,
    };
    const httpd_uri_t disconnect_uri = {
        .uri = "/api/cast/disconnect",
        .method = HTTP_POST,
        .handler = HandleDisconnectPost,
        .user_ctx = this,
    };
    const httpd_uri_t status_uri = {
        .uri = "/api/cast/status",
        .method = HTTP_GET,
        .handler = HandleStatusGet,
        .user_ctx = this,
    };

    httpd_register_uri_handler(server_, &frame_uri);
    httpd_register_uri_handler(server_, &hb_uri);
    httpd_register_uri_handler(server_, &disconnect_uri);
    httpd_register_uri_handler(server_, &status_uri);

    running_.store(true);
    streaming_.store(false);
    audio_paused_.store(false);
    ESP_LOGI(TAG, "无线投屏 HTTP 服务已启动: 0.0.0.0:%u", port_);
    return true;
}

void CastManager::stop() {
    if (!running_.exchange(false)) {
        return;
    }

    if (server_) {
        httpd_stop(server_);
        server_ = nullptr;
    }
    if (rx_buf_) {
        heap_caps_free(rx_buf_);
        rx_buf_ = nullptr;
    }

    streaming_.store(false);
    audio_paused_.store(false);
    {
        std::lock_guard<std::mutex> lock(state_mutex_);
        last_touch_us_ = 0;
        last_frame_us_ = 0;
        fps_window_start_us_ = 0;
        fps_window_frames_ = 0;
        recent_fps_ = 0.0f;
    }
    if (display_) {
        display_->SetCastStreaming(false);
    }
    ESP_LOGI(TAG, "无线投屏 HTTP 服务已停止");
}

void CastManager::checkTimeout() {
    if (!running_.load() || !streaming_.load()) {
        return;
    }

    bool timed_out = false;
    const uint32_t timeout_s = timeout_sec_.load();
    {
        std::lock_guard<std::mutex> lock(state_mutex_);
        const int64_t now_us = esp_timer_get_time();
        if (timeout_s > 0 && last_touch_us_ > 0) {
            const int64_t elapsed_us = now_us - last_touch_us_;
            if (elapsed_us > static_cast<int64_t>(timeout_s) * 1000000LL) {
                timed_out = true;
                last_frame_us_ = 0;
                recent_fps_ = 0.0f;
                fps_window_frames_ = 0;
                fps_window_start_us_ = 0;
            }
        }
    }

    if (timed_out) {
        streaming_.store(false);
        audio_paused_.store(false);
        if (display_) {
            display_->SetCastStreaming(false);
        }
        ESP_LOGW(TAG, "投屏接收超时，已切回投屏等待连接页面");
    }
}

float CastManager::getRecentFps() const {
    std::lock_guard<std::mutex> lock(state_mutex_);
    const int64_t now_us = esp_timer_get_time();
    // 仅以最近实际帧到达时间判断 FPS 是否过期，避免保活心跳导致旧 FPS 长期不归零
    if (!streaming_.load() || last_frame_us_ == 0 || (now_us - last_frame_us_ > 3000000LL)) {
        return 0.0f;
    }
    return recent_fps_;
}

bool CastManager::verifyAuthToken(httpd_req_t* req) const {
    const char* expected = CAST_AUTH_TOKEN;
    if (!expected || expected[0] == '\0') {
        return true;
    }
    size_t hdr_len = httpd_req_get_hdr_value_len(req, "X-Cast-Token");
    if (hdr_len == 0 || hdr_len > 256) {
        return false;
    }
    std::string actual(hdr_len + 1, '\0');
    if (httpd_req_get_hdr_value_str(req, "X-Cast-Token", actual.data(), actual.size()) != ESP_OK) {
        return false;
    }
    actual.resize(std::strlen(actual.c_str()));
    return actual == expected;
}

int CastManager::parseIntHeader(httpd_req_t* req, const char* name, int fallback) const {
    size_t hdr_len = httpd_req_get_hdr_value_len(req, name);
    if (hdr_len == 0 || hdr_len > 32) {
        return fallback;
    }
    char buf[40] = {};
    if (httpd_req_get_hdr_value_str(req, name, buf, sizeof(buf)) != ESP_OK) {
        return fallback;
    }
    char* endptr = nullptr;
    long parsed = std::strtol(buf, &endptr, 10);
    if (endptr == buf) {
        return fallback;
    }
    return static_cast<int>(parsed);
}

esp_err_t CastManager::sendJsonResponse(httpd_req_t* req, const char* status, const char* json_str) const {
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, json_str);
}

esp_err_t CastManager::HandleFramePost(httpd_req_t* req) {
    auto* self = static_cast<CastManager*>(req->user_ctx);
    if (!self || !self->running_.load() || !self->rx_buf_ || !self->display_) {
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "service not ready");
    }
    if (!self->verifyAuthToken(req)) {
        // 未读取完请求体时主动标记 Connection: close，避免后续复用请求被残余字节污染
        if (req->content_len > 0) {
            httpd_resp_set_hdr(req, "Connection", "close");
        }
        return self->sendJsonResponse(req, "401 Unauthorized", "{\"ok\":false,\"error\":\"unauthorized\"}");
    }
    if (req->content_len != kFrameBytes) {
        if (req->content_len > 0) {
            httpd_resp_set_hdr(req, "Connection", "close");
        }
        char err_json[128];
        snprintf(err_json, sizeof(err_json),
                 "{\"ok\":false,\"error\":\"invalid frame size\",\"expected\":%u,\"actual\":%d}",
                 static_cast<unsigned>(kFrameBytes), req->content_len);
        return self->sendJsonResponse(req, "400 Bad Request", err_json);
    }

    size_t received = 0;
    int timeout_retries = 0;
    while (received < kFrameBytes) {
        int ret = httpd_req_recv(req, reinterpret_cast<char*>(self->rx_buf_ + received), kFrameBytes - received);
        if (ret <= 0) {
            if (ret == HTTPD_SOCK_ERR_TIMEOUT) {
                // 限制连续超时重试次数，防止半开或卡死连接长期阻塞单线程 HTTP 服务
                if (++timeout_retries > 2) {
                    ESP_LOGW(TAG, "接收投屏帧数据连续超时，断开请求 (%u/%u)",
                             static_cast<unsigned>(received), static_cast<unsigned>(kFrameBytes));
                    return ESP_FAIL;
                }
                continue;
            }
            ESP_LOGW(TAG, "接收投屏帧数据中断 (%d)", ret);
            return ESP_FAIL;
        }
        timeout_retries = 0;
        received += static_cast<size_t>(ret);
    }

    const bool auto_switch = (self->parseIntHeader(req, "X-Auto-Switch", 0) != 0);
    int timeout_hdr = self->parseIntHeader(req, "X-Cast-Timeout", static_cast<int>(self->timeout_sec_.load()));
    if (timeout_hdr < 0) timeout_hdr = 0;
    if (timeout_hdr > 86400) timeout_hdr = 86400;

    // 检测当前是否正处于小智语音会话期间；通话时仅更新缓存而不争抢 SPI 总线
    auto& app = Application::GetInstance();
    const DeviceState ds = app.GetDeviceState();
    const bool in_audio = (ds == kDeviceStateConnecting ||
                           ds == kDeviceStateListening ||
                           ds == kDeviceStateSpeaking);
    self->audio_paused_.store(in_audio);

    const uint32_t total_frames = self->frames_received_.fetch_add(1) + 1;
    const int64_t now_us = esp_timer_get_time();
    self->timeout_sec_.store(static_cast<uint32_t>(timeout_hdr));
    {
        std::lock_guard<std::mutex> lock(self->state_mutex_);
        self->last_touch_us_ = now_us;
        self->last_frame_us_ = now_us;
        if (self->fps_window_start_us_ == 0) {
            self->fps_window_start_us_ = now_us;
        }
        self->fps_window_frames_++;
        const int64_t delta_us = now_us - self->fps_window_start_us_;
        if (delta_us >= 1000000LL) {
            self->recent_fps_ = (static_cast<float>(self->fps_window_frames_) * 1000000.0f) /
                                 static_cast<float>(delta_us);
            self->fps_window_start_us_ = now_us;
            self->fps_window_frames_ = 0;
        }
    }

    self->streaming_.store(true);
    self->display_->NotifyUserActivity();
    const bool blitted = self->display_->UpdateCastFrame(self->rx_buf_, kFrameBytes, auto_switch, !in_audio);

    char ok_json[128];
    snprintf(ok_json, sizeof(ok_json),
             "{\"ok\":true,\"frames\":%lu,\"blitted\":%s,\"audio_paused\":%s}",
             static_cast<unsigned long>(total_frames),
             blitted ? "true" : "false",
             in_audio ? "true" : "false");
    return self->sendJsonResponse(req, "200 OK", ok_json);
}

esp_err_t CastManager::HandleHeartbeatPost(httpd_req_t* req) {
    auto* self = static_cast<CastManager*>(req->user_ctx);
    if (!self || !self->running_.load()) {
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "service not ready");
    }
    if (!self->verifyAuthToken(req)) {
        if (req->content_len > 0) {
            httpd_resp_set_hdr(req, "Connection", "close");
        }
        return self->sendJsonResponse(req, "401 Unauthorized", "{\"ok\":false,\"error\":\"unauthorized\"}");
    }

    int timeout_hdr = self->parseIntHeader(req, "X-Cast-Timeout", static_cast<int>(self->timeout_sec_.load()));
    if (timeout_hdr < 0) timeout_hdr = 0;
    if (timeout_hdr > 86400) timeout_hdr = 86400;
    self->timeout_sec_.store(static_cast<uint32_t>(timeout_hdr));

    const bool is_streaming = self->streaming_.load();
    if (is_streaming) {
        {
            std::lock_guard<std::mutex> lock(self->state_mutex_);
            self->last_touch_us_ = esp_timer_get_time();
        }
        if (self->display_) {
            self->display_->NotifyUserActivity();
        }
    }

    char ok_json[96];
    snprintf(ok_json, sizeof(ok_json),
             "{\"ok\":true,\"streaming\":%s}",
             is_streaming ? "true" : "false");
    return self->sendJsonResponse(req, "200 OK", ok_json);
}

esp_err_t CastManager::HandleDisconnectPost(httpd_req_t* req) {
    auto* self = static_cast<CastManager*>(req->user_ctx);
    if (!self || !self->running_.load()) {
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "service not ready");
    }
    if (!self->verifyAuthToken(req)) {
        if (req->content_len > 0) {
            httpd_resp_set_hdr(req, "Connection", "close");
        }
        return self->sendJsonResponse(req, "401 Unauthorized", "{\"ok\":false,\"error\":\"unauthorized\"}");
    }

    self->streaming_.store(false);
    self->audio_paused_.store(false);
    {
        std::lock_guard<std::mutex> lock(self->state_mutex_);
        self->last_touch_us_ = 0;
        self->last_frame_us_ = 0;
        self->fps_window_frames_ = 0;
        self->fps_window_start_us_ = 0;
        self->recent_fps_ = 0.0f;
    }
    if (self->display_) {
        self->display_->SetCastStreaming(false);
    }
    ESP_LOGI(TAG, "收到主动断开请求，恢复投屏等待界面");
    return self->sendJsonResponse(req, "200 OK", "{\"ok\":true,\"streaming\":false}");
}

esp_err_t CastManager::HandleStatusGet(httpd_req_t* req) {
    auto* self = static_cast<CastManager*>(req->user_ctx);
    if (!self) {
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "service not ready");
    }
    if (!self->verifyAuthToken(req)) {
        return self->sendJsonResponse(req, "401 Unauthorized", "{\"ok\":false,\"error\":\"unauthorized\"}");
    }

    auto& wifi = WifiManager::GetInstance();
    int battery_level = 0;
    bool charging = false, discharging = false;
    Board::GetInstance().GetBatteryLevel(battery_level, charging, discharging);

    cJSON* root = cJSON_CreateObject();
    if (!root) {
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "oom");
    }

    cJSON_AddBoolToObject(root, "running", self->running_.load());
    cJSON_AddBoolToObject(root, "streaming", self->streaming_.load());
    cJSON_AddBoolToObject(root, "audio_paused", self->audio_paused_.load());
    cJSON_AddNumberToObject(root, "width", 400);
    cJSON_AddNumberToObject(root, "height", 300);
    cJSON_AddNumberToObject(root, "bpp", 1);
    cJSON_AddStringToObject(root, "mode", self->display_ ? self->display_->GetDisplayModeName() : "unknown");
    cJSON_AddStringToObject(root, "ip", wifi.GetIpAddress().c_str());
    cJSON_AddNumberToObject(root, "port", self->port_);
    cJSON_AddNumberToObject(root, "frames", static_cast<double>(self->frames_received_.load()));
    cJSON_AddNumberToObject(root, "fps", static_cast<double>(static_cast<int>(self->getRecentFps() * 10.0f + 0.5f)) / 10.0);
    cJSON_AddNumberToObject(root, "battery", battery_level);
    cJSON_AddNumberToObject(root, "rssi", wifi.GetRssi());

    char* raw = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!raw) {
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "json serialize failed");
    }
    esp_err_t ret = self->sendJsonResponse(req, "200 OK", raw);
    cJSON_free(raw);
    return ret;
}
