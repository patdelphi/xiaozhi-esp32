#include "weather_manager.h"
#include "esp_log.h"

static const char *TAG = "WeatherManager";

WeatherManager& WeatherManager::getInstance() {
    static WeatherManager instance;
    return instance;
}

void WeatherManager::setApiConfig(const char* key, const char* host) {
    (void)key;
    (void)host;
}

WeatherData WeatherManager::getLatestData() {
    std::lock_guard<std::mutex> lock(mutex_);
    return latest_data_;
}

bool WeatherManager::update() {
    ESP_LOGI(TAG, "板载和风天气拉取已停用，等待 MCP self.weather.update 同步");
    return false;
}

bool WeatherManager::updateFromExternal(const std::string& city,
                                        const std::string& weather_text,
                                        const std::string& temperature,
                                        const std::string& update_time) {
    if (city.empty() || weather_text.empty() || temperature.empty()) {
        ESP_LOGW(TAG, "外部天气数据无效：city/text/temp 不能为空");
        return false;
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        latest_data_.city = city;
        latest_data_.text = weather_text;
        latest_data_.temp = temperature;
        latest_data_.update_time = update_time.empty() ? "mcp" : update_time;
        latest_data_.valid = true;
    }

    ESP_LOGI(TAG, "天气已由外部写入: %s %s %s°C",
             city.c_str(),
             weather_text.c_str(),
             temperature.c_str());
    return true;
}
