#pragma once
#include <mutex>
#include <string>

// 天气数据结构
struct WeatherData {
    std::string city;     // 城市名
    std::string temp;     // 温度（字符串）
    std::string text;     // 天气描述（如"晴"、"多云"）
    std::string update_time;
    bool valid = false;
};

// 天气管理器：维护本地天气缓存（由 AI MCP 工具 self.weather.update 写入）
class WeatherManager {
public:
    static WeatherManager& getInstance();

    bool update();
    WeatherData getLatestData();
    void setApiConfig(const char* key, const char* host);

    bool updateFromExternal(const std::string& city,
                            const std::string& weather_text,
                            const std::string& temperature,
                            const std::string& update_time = "");

private:
    WeatherManager() = default;
    mutable std::mutex mutex_;
    WeatherData latest_data_;
};
