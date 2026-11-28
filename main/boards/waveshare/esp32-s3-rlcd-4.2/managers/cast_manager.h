#ifndef __CAST_MANAGER_H__
#define __CAST_MANAGER_H__

/*
 * 程序说明：Waveshare ESP32-S3-RLCD-4.2 板载无线投屏 HTTP 接收管理器。
 *
 * 提供以下 HTTP API（默认监听端口 8080）：
 *   - POST /api/cast/frame      接收 15,000 字节行优先 MSB-first 1-bit 位图并触发显示
 *   - POST /api/cast/heartbeat  静默刷新保活计时，配合后台去重跳过
 *   - POST /api/cast/disconnect 主动结束投屏并切回第四屏等待连接页
 *   - GET  /api/cast/status     返回分辨率、位深、页面模式、FPS、电量和信号强度 JSON
 */

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <esp_http_server.h>

class CustomLcdDisplay;

class CastManager {
public:
    static constexpr uint16_t kDefaultPort = 8080;
    static constexpr size_t kFrameBytes = (400 * 300) / 8;
    static constexpr uint32_t kDefaultTimeoutSec = 30;

    static CastManager& getInstance() {
        static CastManager instance;
        return instance;
    }

    // 启动板载 HTTP 接收服务（分配 PSRAM 帧缓冲并注册路由）
    bool start(CustomLcdDisplay* display, uint16_t port = kDefaultPort);

    // 停止服务并释放接收缓冲区
    void stop();

    // 周期超时检测（超过 timeout_sec 未收到帧与心跳时自动退出投屏激活态）
    void checkTimeout();

    bool isRunning() const { return running_.load(); }
    bool isStreaming() const { return streaming_.load(); }
    bool isAudioPaused() const { return audio_paused_.load(); }
    void clearAudioPaused() { audio_paused_.store(false); }

    uint16_t getPort() const { return port_; }
    uint32_t getFramesReceived() const { return frames_received_.load(); }
    float getRecentFps() const;

private:
    CastManager() = default;
    ~CastManager();

    CastManager(const CastManager&) = delete;
    CastManager& operator=(const CastManager&) = delete;

    // HTTP 请求处理器
    static esp_err_t HandleFramePost(httpd_req_t* req);
    static esp_err_t HandleHeartbeatPost(httpd_req_t* req);
    static esp_err_t HandleDisconnectPost(httpd_req_t* req);
    static esp_err_t HandleStatusGet(httpd_req_t* req);

    bool verifyAuthToken(httpd_req_t* req) const;
    int parseIntHeader(httpd_req_t* req, const char* name, int fallback) const;
    esp_err_t sendJsonResponse(httpd_req_t* req, const char* status, const char* json_str) const;

    static constexpr const char* TAG = "CastManager";

    CustomLcdDisplay* display_ = nullptr;
    httpd_handle_t server_ = nullptr;
    uint8_t* rx_buf_ = nullptr;
    uint16_t port_ = kDefaultPort;

    std::atomic<bool> running_{false};
    std::atomic<bool> streaming_{false};
    std::atomic<bool> audio_paused_{false};
    std::atomic<uint32_t> frames_received_{0};

    mutable std::mutex state_mutex_;
    int64_t last_touch_us_ = 0;
    int64_t last_frame_us_ = 0;
    std::atomic<uint32_t> timeout_sec_{kDefaultTimeoutSec};
    int64_t fps_window_start_us_ = 0;
    uint32_t fps_window_frames_ = 0;
    float recent_fps_ = 0.0f;
};

#endif
