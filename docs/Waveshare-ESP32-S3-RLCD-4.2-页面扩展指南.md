# Waveshare ESP32-S3-RLCD-4.2 页面扩展指南

> 文档版本：v1.0
> 生成时间：2026-07-05
> 对应固件版本：xiaozhi-esp32 v2.2.2
> 适用板子：Waveshare ESP32-S3-RLCD-4.2（400×300 1-bit 单色 RLCD）

## 目录

1. [页面机制核心 4 件套](#1-页面机制核心-4-件套)
2. [添加新页面 7 步清单](#2-添加新页面-7-步清单)
3. [关键约束与注意事项](#3-关键约束与注意事项)
4. [示例：股票页完整代码](#4-示例股票页完整代码)
5. [可选 MCP 工具注册](#5-可选-mcp-工具注册)
6. [可选 Manager 模块](#6-可选-manager-模块)
7. [编译验证](#7-编译验证)
8. [常见问题](#8-常见问题)

---

## 1. 页面机制核心 4 件套

每个页面都由这 4 部分组成，全部围绕 `CustomLcdDisplay` 类：

| 组件 | 位置 | 作用 |
|------|------|------|
| ① 枚举 + 状态 | `custom_lcd_display.h` | `enum DisplayMode { MODE_WEATHER, MODE_MUSIC, MODE_POMODORO }` + `display_mode_` 当前页 |
| ② LVGL 容器 | `weather_page_` / `music_page_` / `pomodoro_page_` | 每页一个 `lv_obj_t*` 容器，子控件挂载其上 |
| ③ UI 布局函数 | `weather_ui.cc` / `music_ui.cc` / `pomodoro_ui.cc` | `Setup<Name>UI()` 创建页面控件 |
| ④ 切换逻辑 | `custom_lcd_display.cc` | `ApplyDisplayMode()` 隐藏所有页 → 显示当前页；`CycleDisplayMode()` USER 键循环 |

### 1.1 CMake 自动包含源文件

`main/CMakeLists.txt:651-659` 会自动 GLOB 板子目录下所有 `.cc/.c`：

```cmake
file(GLOB BOARD_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/boards/${BOARD_TYPE}/*.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/boards/${BOARD_TYPE}/*.c
)

file(GLOB_RECURSE BOARD_SUB_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/boards/${BOARD_TYPE}/managers/*.cc
    ${CMAKE_CURRENT_SOURCE_DIR}/boards/${BOARD_TYPE}/managers/*.c
    ${CMAKE_CURRENT_SOURCE_DIR}/boards/${BOARD_TYPE}/assets/*.c
)
```

**新增 `.cc/.c` 文件无需改 CMakeLists**，重新 `idf.py build` 即可。

### 1.2 文件分工速查

| 文件 | 修改性质 | 必改？ |
|------|----------|--------|
| `custom_lcd_display.h` | 改 | 必改（枚举 + 成员声明）|
| `custom_lcd_display.cc` | 改 | 必改（构造调用 + 切换逻辑）|
| `<name>_ui.cc` | 新建 | 必建（UI 布局）|
| `data_update_task.cc` | 改 | 可选（需要定时刷新时）|
| `waveshare-s3-rlcd-4.2.cc` | 改 | 可选（需要 MCP 工具时）|
| `managers/<name>_manager.h/.cc` | 新建 | 可选（需要数据管理时）|

---

## 2. 添加新页面 7 步清单

以"股票页"为例，新增 `MODE_STOCK`。

### 步骤 1：修改 `custom_lcd_display.h`

```cpp
// 1.1 扩展枚举（在 MODE_POMODORO = 2 后追加）
enum DisplayMode {
    MODE_WEATHER = 0,
    MODE_MUSIC = 1,
    MODE_POMODORO = 2,
    MODE_STOCK = 3,    // ← 新增
};

// 1.2 新增页面容器指针（在 pomodoro_page_ 后追加）
lv_obj_t *stock_page_ = nullptr;

// 1.3 新增该页的 UI 组件指针
lv_obj_t *stock_symbol_label_ = nullptr;   // 股票代码
lv_obj_t *stock_price_label_ = nullptr;    // 当前价（大字）
lv_obj_t *stock_change_label_ = nullptr;   // 涨跌幅

// 1.4 声明 UI 创建函数（在 SetupPomodoroUI() 后）
void SetupStockUI();

// 1.5 声明切换 + 更新方法（在 SwitchToPomodoroPage() 后）
void SwitchToStockPage();
bool IsStockMode() const { return display_mode_ == MODE_STOCK; }
void UpdateStockDisplay(const char* symbol, float price, float change_pct);
```

### 步骤 2：新建 `stock_ui.cc`

参考 `weather_ui.cc` 的写法：

```cpp
// 程序说明：股票页 UI 布局，创建 stock_page_ 容器及其子控件
#include "custom_lcd_display.h"

void CustomLcdDisplay::SetupStockUI() {
    DisplayLockGuard lock(this);
    stock_page_ = lv_obj_create(lv_screen_active());
    lv_obj_set_size(stock_page_, 400, 300);
    lv_obj_set_style_bg_color(stock_page_, lv_color_white(), 0);
    lv_obj_set_style_border_width(stock_page_, 0, 0);
    lv_obj_clear_flag(stock_page_, LV_OBJ_FLAG_SCROLLABLE);

    // 股票代码
    stock_symbol_label_ = lv_label_create(stock_page_);
    lv_obj_set_style_text_font(stock_symbol_label_, &alibaba_puhui_24, 0);
    lv_label_set_text(stock_symbol_label_, "AAPL");
    lv_obj_align(stock_symbol_label_, LV_ALIGN_TOP_MID, 0, 20);

    // 当前价（大字）
    stock_price_label_ = lv_label_create(stock_page_);
    lv_obj_set_style_text_font(stock_price_label_, &alibaba_black_64, 0);
    lv_label_set_text(stock_price_label_, "$185.50");
    lv_obj_center(stock_price_label_);

    // 涨跌幅
    stock_change_label_ = lv_label_create(stock_page_);
    lv_obj_set_style_text_font(stock_change_label_, &alibaba_puhui_24, 0);
    lv_label_set_text(stock_change_label_, "+1.25%");
    lv_obj_align(stock_change_label_, LV_ALIGN_BOTTOM_MID, 0, -20);

    // 默认隐藏（重要！否则启动时会重叠显示）
    lv_obj_add_flag(stock_page_, LV_OBJ_FLAG_HIDDEN);
}
```

### 步骤 3：修改 `custom_lcd_display.cc`

**3.1 构造函数中调用 Setup**（在 `SetupPomodoroUI();` 后加）：

```cpp
SetupStockUI();      // ← 新增
ApplyDisplayMode();
```

**3.2 修改 `ApplyDisplayMode()`** — 隐藏分支加一行，显示分支加 case：

```cpp
void CustomLcdDisplay::ApplyDisplayMode() {
    // 先隐藏所有页面
    if (weather_page_)  lv_obj_add_flag(weather_page_, LV_OBJ_FLAG_HIDDEN);
    if (music_page_)    lv_obj_add_flag(music_page_, LV_OBJ_FLAG_HIDDEN);
    if (pomodoro_page_) lv_obj_add_flag(pomodoro_page_, LV_OBJ_FLAG_HIDDEN);
    if (stock_page_)    lv_obj_add_flag(stock_page_, LV_OBJ_FLAG_HIDDEN);   // ← 新增

    // 显示当前页面
    switch (display_mode_) {
        case MODE_WEATHER:  if (weather_page_)  lv_obj_remove_flag(weather_page_, LV_OBJ_FLAG_HIDDEN); break;
        case MODE_MUSIC:    if (music_page_)    lv_obj_remove_flag(music_page_, LV_OBJ_FLAG_HIDDEN); break;
        case MODE_POMODORO: if (pomodoro_page_) lv_obj_remove_flag(pomodoro_page_, LV_OBJ_FLAG_HIDDEN); break;
        case MODE_STOCK:   if (stock_page_)    lv_obj_remove_flag(stock_page_, LV_OBJ_FLAG_HIDDEN); break;  // ← 新增
    }
}
```

**3.3 修改 `CycleDisplayMode()`** — 加循环分支：

```cpp
void CustomLcdDisplay::CycleDisplayMode() {
    DisplayLockGuard lock(this);
    switch (display_mode_) {
        case MODE_WEATHER:  display_mode_ = MODE_MUSIC; break;
        case MODE_MUSIC:    display_mode_ = MODE_POMODORO; break;
        case MODE_POMODORO: display_mode_ = MODE_STOCK; break;     // ← 改：番茄钟→股票
        case MODE_STOCK:    display_mode_ = MODE_WEATHER; break;   // ← 新增：股票→天气
    }
    ApplyDisplayMode();
    const char* name = "未知";
    switch (display_mode_) {
        case MODE_WEATHER:  name = "天气页"; break;
        case MODE_MUSIC:    name = "音乐页"; break;
        case MODE_POMODORO: name = "番茄钟"; break;
        case MODE_STOCK:    name = "股票页"; break;   // ← 新增
    }
    ESP_LOGI(TAG, "页面切换: %s", name);
}
```

**3.4 添加切换 + 更新方法**：

```cpp
void CustomLcdDisplay::SwitchToStockPage() {
    DisplayLockGuard lock(this);
    if (display_mode_ != MODE_STOCK) {
        display_mode_ = MODE_STOCK;
        ApplyDisplayMode();
        ESP_LOGI(TAG, "自动切换到股票页");
    }
}

void CustomLcdDisplay::UpdateStockDisplay(const char* symbol, float price, float change_pct) {
    DisplayLockGuard lock(this);
    if (stock_symbol_label_ && symbol) {
        lv_label_set_text(stock_symbol_label_, symbol);
    }
    if (stock_price_label_) {
        char buf[32];
        snprintf(buf, sizeof(buf), "$%.2f", price);
        lv_label_set_text(stock_price_label_, buf);
    }
    if (stock_change_label_) {
        char buf[32];
        snprintf(buf, sizeof(buf), "%+.2f%%", change_pct);
        lv_label_set_text(stock_change_label_, buf);
    }
}
```

### 步骤 4：修改 `data_update_task.cc`（可选）

如果新页有定时刷新需求（如股票每 30 秒拉一次数据）：

```cpp
// 在 DataUpdateTask 循环里加
if (display->IsStockMode()) {
    // 每 30 秒拉一次数据
    static uint32_t last_stock_fetch = 0;
    uint32_t now = xTaskGetTickCount() * portTICK_PERIOD_MS;
    if (now - last_stock_fetch > 30000) {
        last_stock_fetch = now;
        // 调用 StockManager 拉数据 → UpdateStockDisplay(...)
    }
}
```

### 步骤 5：修改 `waveshare-s3-rlcd-4.2.cc`（可选）

**5.1 在 `self.disp.switch` 工具中加分支**：

```cpp
if (mode == "toggle") {
    display_->CycleDisplayMode();
} else if (mode == "music") {
    display_->SwitchToMusicPage();
} else if (mode == "weather") {
    display_->SwitchToWeatherPage();
} else if (mode == "pomodoro") {
    display_->SwitchToPomodoroPage();
} else if (mode == "stock") {            // ← 新增
    display_->SwitchToStockPage();
} else {
    return std::string("参数 mode 无效，请使用 toggle/music/weather/pomodoro/stock");
}

// 同步更新返回值判断：
if (display_->IsMusicMode())    return std::string("已切换到音乐页");
if (display_->IsPomodoroMode()) return std::string("已切换到番茄钟页");
if (display_->IsStockMode())    return std::string("已切换到股票页");   // ← 新增
return std::string("已切换到天气页");

// 工具描述同步更新：
"  `mode`: 'toggle' | 'music' | 'weather' | 'pomodoro' | 'stock' (default: 'toggle')",
```

**5.2 注册新 MCP 工具**（详见第 5 章）。

### 步骤 6：新建 `managers/stock_manager.h/.cc`（可选）

如果新功能有数据管理需求（如缓存、定时拉取），参考 `weather_manager.h` 创建单例 Manager。`GLOB_RECURSE` 会自动包含。

### 步骤 7：编译验证

```bash
idf.py build
```

CMake 自动 GLOB，无需改构建脚本。

---

## 3. 关键约束与注意事项

### 3.1 必须遵守的规则

1. **页面容器必须 `lv_obj_add_flag(..., LV_OBJ_FLAG_HIDDEN)` 默认隐藏**，否则启动时会重叠显示
2. **所有 UI 操作必须包 `DisplayLockGuard lock(this);`**，避免与 DataUpdateTask 竞争
3. **字体只能用项目自带的 4 套**：
   - `alibaba_puhui_16`（状态栏、备忘录）
   - `alibaba_puhui_24`（普通文本）
   - `alibaba_puhui_48`（番茄钟倒计时）
   - `alibaba_black_64`（大字时钟）
   
   不能直接用 LVGL 内置字体（已被项目配置排除）
4. **单色屏颜色只有黑白**，不要用颜色区分状态，改用文字/图标/反白
5. **MCP 工具回调里访问硬件必须 `Application::Schedule` 投递到主线程**，避免抢音频线程
6. **DataUpdateTask 的刷新间隔受省电模式影响**（1s/5s），不要在任务里做高频更新

### 3.2 LVGL 控件创建顺序

```
lv_screen_active()              ← 根屏幕
  └─ stock_page_ (lv_obj_create) ← 页面容器
       ├─ stock_symbol_label_   ← 子控件
       ├─ stock_price_label_
       └─ stock_change_label_
```

页面容器必须挂在 `lv_screen_active()` 下，不要挂到其他页面容器里。

### 3.3 字体引用

字体变量声明在 `weather_ui.cc` 或 `custom_lcd_display.cc` 顶部，已 extern 到其他 ui.cc 文件可直接用。新增字体需要：

1. 用 `LvglFontTool` 生成 `.c` 字体文件
2. 放到 `assets/fonts/` 目录
3. 在某个 ui.cc 顶部声明 `LV_FONT_DECLARE(alibaba_xxx_32)`
4. 用 `lv_obj_set_style_text_font(label, &alibaba_xxx_32, 0)` 引用

### 3.4 省电模式注意事项

`power_saving_` 为 true 时，DataUpdateTask 刷新间隔从 1 秒变为 5 秒。新页面的定时刷新逻辑会受此影响。

调用 `NotifyUserActivity()` 可立即唤醒省电模式（USER 按键、MCP 工具调用都会自动唤醒）。

---

## 4. 示例：股票页完整代码

### 4.1 `stock_ui.cc`（新建）

```cpp
// 程序说明：股票页 UI 布局，创建 stock_page_ 容器及其子控件
// 显示股票代码、当前价格（大字）、涨跌幅
#include "custom_lcd_display.h"

// 字体声明（已 extern 在其他 ui.cc，可直接用）
LV_FONT_DECLARE(alibaba_puhui_24);
LV_FONT_DECLARE(alibaba_black_64);

void CustomLcdDisplay::SetupStockUI() {
    DisplayLockGuard lock(this);
    stock_page_ = lv_obj_create(lv_screen_active());
    lv_obj_set_size(stock_page_, 400, 300);
    lv_obj_set_style_bg_color(stock_page_, lv_color_white(), 0);
    lv_obj_set_style_border_width(stock_page_, 0, 0);
    lv_obj_set_style_pad_all(stock_page_, 0, 0);
    lv_obj_clear_flag(stock_page_, LV_OBJ_FLAG_SCROLLABLE);

    // 股票代码
    stock_symbol_label_ = lv_label_create(stock_page_);
    lv_obj_set_style_text_font(stock_symbol_label_, &alibaba_puhui_24, 0);
    lv_obj_set_style_text_color(stock_symbol_label_, lv_color_black(), 0);
    lv_label_set_text(stock_symbol_label_, "AAPL");
    lv_obj_align(stock_symbol_label_, LV_ALIGN_TOP_MID, 0, 20);

    // 当前价（大字，居中）
    stock_price_label_ = lv_label_create(stock_page_);
    lv_obj_set_style_text_font(stock_price_label_, &alibaba_black_64, 0);
    lv_obj_set_style_text_color(stock_price_label_, lv_color_black(), 0);
    lv_label_set_text(stock_price_label_, "$185.50");
    lv_obj_center(stock_price_label_);

    // 涨跌幅（底部）
    stock_change_label_ = lv_label_create(stock_page_);
    lv_obj_set_style_text_font(stock_change_label_, &alibaba_puhui_24, 0);
    lv_obj_set_style_text_color(stock_change_label_, lv_color_black(), 0);
    lv_label_set_text(stock_change_label_, "+1.25%");
    lv_obj_align(stock_change_label_, LV_ALIGN_BOTTOM_MID, 0, -20);

    // 默认隐藏（重要！）
    lv_obj_add_flag(stock_page_, LV_OBJ_FLAG_HIDDEN);
}
```

### 4.2 `custom_lcd_display.cc` 改动汇总

需要改动的 4 处：

```cpp
// 改动 1：构造函数末尾，SetupPomodoroUI() 之后
SetupStockUI();
ApplyDisplayMode();

// 改动 2：ApplyDisplayMode() 增加一行隐藏 + 一个 case 显示
// 改动 3：CycleDisplayMode() 增加两个 case 循环
// 改动 4：文件末尾追加 SwitchToStockPage() 和 UpdateStockDisplay()
```

---

## 5. 可选 MCP 工具注册

### 5.1 注册工具

在 `waveshare-s3-rlcd-4.2.cc` 的 `InitializeTools()` 中：

```cpp
mcp_server.AddTool("self.stock.query",
    "Query stock price by symbol.
"
    "Use when user asks: '查股票', '苹果股价', 'stock price'
"
    "Args:
"
    "  `symbol`: Stock symbol (e.g. AAPL, GOOGL)",
    PropertyList({
        Property("symbol", kPropertyTypeString)
    }),
    [this](const PropertyList& props) -> ReturnValue {
        auto sym = props["symbol"].value<std::string>();
        
        // 调用 StockManager 查询，这里简化为返回固定字符串
        // 实际应该调用 HTTP API 查询实时价格
        std::string result = sym + " 当前价 $185.50，涨幅 +1.25%";
        
        // 自动切换到股票页
        if (display_) {
            display_->NotifyUserActivity();
            display_->SwitchToStockPage();
            display_->UpdateStockDisplay(sym.c_str(), 185.50f, 1.25f);
        }
        
        return result;
    });
```

### 5.2 工具命名规范

- 必须以 `self.` 开头
- 多级命名用 `.` 分隔：`self.stock.query` / `self.stock.list`
- 工具名应语义化，方便 AI 调用

### 5.3 返回值类型

```cpp
return std::string("文本结果");           // 文本
return true;                              // 布尔
return 42;                                // 整数
return cJSON_CreateObject();              // JSON 对象
```

### 5.4 异步执行注意事项

工具回调在 MCP 线程执行，访问硬件（如显示）必须用 `Application::Schedule`：

```cpp
[this, sym](const PropertyList& props) -> ReturnValue {
    // 简单字符串返回可直接返回
    // 访问硬件必须 Schedule
    Application::GetInstance().Schedule([this, sym]() {
        display_->SwitchToStockPage();
        display_->UpdateStockDisplay(sym.c_str(), 185.50f, 1.25f);
    });
    return std::string("已切换到股票页");
}
```

---

## 6. 可选 Manager 模块

### 6.1 何时需要 Manager

- 需要缓存数据（避免重复请求）
- 需要后台定时拉取
- 需要跨页面共享数据
- 需要持久化（NVS）

### 6.2 Manager 模板

参考 `weather_manager.h` 创建单例：

```cpp
// managers/stock_manager.h
#ifndef STOCK_MANAGER_H
#define STOCK_MANAGER_H

#include <string>
#include <atomic>

class StockManager {
public:
    static StockManager& getInstance() {
        static StockManager instance;
        return instance;
    }
    
    // 查询股票（返回 true 表示成功）
    bool query(const std::string& symbol, float& price, float& change_pct);
    
    // 缓存最近查询结果
    struct StockData {
        std::string symbol;
        float price = 0.0f;
        float change_pct = 0.0f;
        bool valid = false;
    };
    const StockData& getLastData() const { return last_data_; }

private:
    StockManager() = default;
    ~StockManager() = default;
    StockManager(const StockManager&) = delete;
    StockManager& operator=(const StockManager&) = delete;
    
    StockData last_data_;
    std::atomic<bool> querying_{false};
};

#endif
```

### 6.3 Manager 调用

```cpp
// 在 MCP 工具回调中
auto& stock = StockManager::getInstance();
float price, change;
if (stock.query("AAPL", price, change)) {
    display_->UpdateStockDisplay("AAPL", price, change);
    return std::string("查询成功");
}
return std::string("查询失败");
```

---

## 7. 编译验证

### 7.1 编译命令

```bash
cd y:\Dev\Waveshare\ESP32-S3-RLCD-4.2\xiaozhi-esp32
idf.py build
```

### 7.2 检查清单

编译前确认：

- [ ] `custom_lcd_display.h` 枚举、成员声明、方法声明都已加
- [ ] `custom_lcd_display.cc` 构造函数调用 `Setup<Name>UI()`
- [ ] `custom_lcd_display.cc` 的 `ApplyDisplayMode()` 加了隐藏 + 显示 case
- [ ] `custom_lcd_display.cc` 的 `CycleDisplayMode()` 加了循环 case
- [ ] `<name>_ui.cc` 文件已创建，`Setup<Name>UI()` 实现完整
- [ ] 页面容器默认 `LV_OBJ_FLAG_HIDDEN` 已加
- [ ] 所有 UI 操作有 `DisplayLockGuard lock(this);`

### 7.3 烧录测试

```bash
idf.py -p COM3 flash monitor
```

测试项：

- [ ] 启动后默认进入天气页（不重叠）
- [ ] USER 键单击循环：天气 → 音乐 → 番茄钟 → 股票 → 天气
- [ ] MCP 工具 `self.disp.switch` mode=stock 能切换
- [ ] 切换到股票页后能正常显示数据
- [ ] 切换走后页面完全隐藏

---

## 8. 常见问题

### Q1：新增页面启动时与其他页重叠显示？

**原因**：未在 `Setup<Name>UI()` 末尾调用 `lv_obj_add_flag(<page>, LV_OBJ_FLAG_HIDDEN)`。

**解决**：确保每个新页面容器默认隐藏。

### Q2：USER 键循环到新页面就停止？

**原因**：`CycleDisplayMode()` 的 switch 没加新 case。

**解决**：在 switch 中加 `case MODE_STOCK: display_mode_ = MODE_WEATHER; break;` 让循环回到第一页。

### Q3：编译报错 `undefined reference to SetupStockUI`？

**原因**：`stock_ui.cc` 文件未被包含到编译。

**解决**：确认文件在 `boards/waveshare-s3-rlcd-4.2/` 目录下，CMake GLOB 会自动包含。如仍未包含，重新运行 `idf.py reconfigure`。

### Q4：字体找不到 `alibaba_xxx_32`？

**原因**：新字体未声明或未在 `assets/fonts/` 目录。

**解决**：
1. 用 `LvglFontTool` 生成 `.c` 字体文件
2. 放到 `assets/fonts/`
3. 在 ui.cc 顶部加 `LV_FONT_DECLARE(alibaba_xxx_32);`

### Q5：MCP 工具调用后页面没切？

**原因**：可能在 MCP 线程直接操作 LVGL，未 Schedule。

**解决**：用 `Application::Schedule()` 投递到主线程：

```cpp
Application::GetInstance().Schedule([this]() {
    display_->SwitchToStockPage();
});
```

### Q6：DataUpdateTask 刷新太慢？

**原因**：省电模式（`power_saving_=true`）下刷新间隔从 1 秒变 5 秒。

**解决**：调用 `NotifyUserActivity()` 唤醒，或在 DataUpdateTask 内部根据 `power_saving_` 状态决定是否跳过低优先级更新。

### Q7：单色屏如何表示"涨/跌"？

**A**：用文字/符号，不要用红绿色：

- 涨：`↑ +1.25%` 或 反白显示
- 跌：`↓ -0.85%`
- 平：`= 0.00%`

---

## 文档说明

- 本文档基于 `xiaozhi-esp32 v2.2.2` 源码静态分析生成
- 示例代码以"股票页"为例，实际添加其他功能页面时按相同 7 步流程替换名称即可
- 编译验证需 ESP-IDF v5.4+
- 如需深入特定模块，参考板子目录下 `README.md` 和现有 `weather_ui.cc` / `music_ui.cc` / `pomodoro_ui.cc` 实现
