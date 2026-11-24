# ============================================================================
# 程序说明: ESP32-S3-RLCD-4.2 独立便携烧录脚本 (零 ESP-IDF / 零 Python 依赖)
# 适用硬件: Waveshare ESP32-S3-RLCD-4.2 (ESP32-S3, 16MB Flash, DIO, 80MHz)
# 使用方式: 将整个 flash_package 文件夹拷贝至任意 Windows 电脑，双击 flash.bat
# ============================================================================
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [string]$Firmware = "",
    [string]$Port = "",
    [string]$Address = "",
    [int]$Baud = 460800,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

# ============================================================================
# 固定路径与芯片烧录参数
# ============================================================================
$script:PackageRoot = $PSScriptRoot
$script:ToolsDir    = Join-Path $script:PackageRoot "tools"
$script:FirmwareDir = Join-Path $script:PackageRoot "firmware"
$script:EsptoolExe  = Join-Path $script:ToolsDir "esptool.exe"

$script:Chip        = "esp32s3"
$script:FlashMode   = "dio"
$script:FlashSize   = "16MB"
$script:FlashFreq   = "80m"
$script:AppOffset   = "0x20000"

# ============================================================================
# 辅助函数
# ============================================================================
function Show-Banner {
    Write-Host ""
    Write-Host "  ====================================================" -ForegroundColor Cyan
    Write-Host "    ESP32-S3-RLCD-4.2 独立便携烧录工具" -ForegroundColor Cyan
    Write-Host "    芯片: $($script:Chip) | Flash: $($script:FlashSize) $($script:FlashMode) $($script:FlashFreq) | 免安装环境" -ForegroundColor DarkGray
    Write-Host "  ====================================================" -ForegroundColor Cyan
    Write-Host ""
}

function Show-Menu {
    Write-Host "  [1] 烧录固件 (自动匹配地址 0x0 / $($script:AppOffset))" -ForegroundColor Yellow
    Write-Host "  [2] 检测开发板连接 (flash_id / 芯片信息)" -ForegroundColor Yellow
    Write-Host "  [3] 擦除整片 Flash (erase_flash，需二次确认)" -ForegroundColor Yellow
    Write-Host "  [0] 退出" -ForegroundColor DarkGray
    Write-Host ""
}

function Assert-EsptoolReady {
    if (-not (Test-Path -LiteralPath $script:EsptoolExe)) {
        throw "找不到独立烧录工具: $($script:EsptoolExe)。请确认已完整拷贝整个 flash_package 目录。"
    }
    $item = Get-Item -LiteralPath $script:EsptoolExe
    if ($item.Length -le 2MB) {
        throw "tools\esptool.exe 文件体积异常 ($($item.Length) 字节)，不是独立的 Windows 可执行程序。"
    }
}

function Get-FirmwareFiles {
    if (-not (Test-Path -LiteralPath $script:FirmwareDir)) {
        return @()
    }
    $files = @(Get-ChildItem -LiteralPath $script:FirmwareDir -Filter "*.bin" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
    return $files
}

function Resolve-DefaultAddress {
    param([string]$FileName)
    if ($FileName -match "_full_|merged") {
        return "0x0"
    }
    if ($FileName -match "_app_|xiaozhi\.bin") {
        return $script:AppOffset
    }
    return "0x0"
}

function Normalize-PortName {
    param([string]$InputPort)
    if ([string]::IsNullOrWhiteSpace($InputPort)) {
        return ""
    }
    $trimmed = $InputPort.Trim().ToUpperInvariant()
    if ($trimmed -match "^\d+$") {
        return "COM$trimmed"
    }
    return $trimmed
}

function Get-SerialPorts {
    $portNames = @()
    try {
        $rawPorts = [System.IO.Ports.SerialPort]::GetPortNames()
        if ($rawPorts) {
            $portNames = @($rawPorts | ForEach-Object { $_.Trim().ToUpperInvariant() } | Select-Object -Unique)
        }
    } catch {
        $portNames = @()
    }

    $nameMap = @{}
    try {
        $pnpDevices = Get-CimInstance -ClassName Win32_PnPEntity -Filter "Name LIKE '%(COM%'" -ErrorAction Stop
        foreach ($dev in $pnpDevices) {
            if ($dev.Name -match "\((COM\d+)\)") {
                $comKey = $matches[1].ToUpperInvariant()
                $nameMap[$comKey] = $dev.Name
            }
        }
    } catch {
        # 无 CIM/WMI 权限时回退显示纯 COM 端口号
    }

    foreach ($k in $nameMap.Keys) {
        if ($portNames -notcontains $k) {
            $portNames += $k
        }
    }

    $sortedPorts = @(
        $portNames | Sort-Object {
            if ($_ -match "^COM(\d+)$") { [int]$matches[1] } else { 99999 }
        }
    )

    $result = @()
    foreach ($p in $sortedPorts) {
        $desc = if ($nameMap.ContainsKey($p)) { $nameMap[$p] } else { $p }
        $result += [PSCustomObject]@{
            Port        = $p
            Description = $desc
        }
    }
    return $result
}

function Select-SerialPort {
    param([string]$ProvidedPort = "")
    if (-not [string]::IsNullOrWhiteSpace($ProvidedPort)) {
        return (Normalize-PortName -InputPort $ProvidedPort)
    }

    while ($true) {
        $ports = @(Get-SerialPorts)
        Write-Host ""
        Write-Host "  检测到的串口列表:" -ForegroundColor Cyan
        if ($ports.Count -eq 0) {
            Write-Host "  [提示] 未发现 COM 端口。请确认 USB 数据线已插好，或按住 BOOT(GPIO0) 不放并重新插拔 Type-C 上电（电池供电时可长按 PWR 关机后按住 BOOT 上电）。" -ForegroundColor Yellow
        } else {
            for ($i = 0; $i -lt $ports.Count; $i++) {
                Write-Host "  [$($i + 1)] $($ports[$i].Port)  - $($ports[$i].Description)" -ForegroundColor White
            }
        }
        Write-Host "  [R] 刷新串口列表" -ForegroundColor DarkGray
        Write-Host ""

        $defaultTip = if ($ports.Count -gt 0) { " [默认=1]" } else { "" }
        $raw = Read-Host "  请输入编号、串口号(如 COM4 或 4)，或按 R 刷新$defaultTip"

        if ($raw -eq "r" -or $raw -eq "R") {
            continue
        }

        if ([string]::IsNullOrWhiteSpace($raw)) {
            if ($ports.Count -gt 0) {
                return $ports[0].Port
            }
            Write-Host "  [错误] 当前没有可用的默认串口，请手动输入或按 R 刷新" -ForegroundColor Red
            continue
        }

        $trimmed = $raw.Trim()
        $num = 0
        if ([int]::TryParse($trimmed, [ref]$num)) {
            if ($ports.Count -gt 0 -and $num -ge 1 -and $num -le $ports.Count) {
                return $ports[$num - 1].Port
            }
            return "COM$num"
        }

        if ($trimmed -match "^(?i)COM\d+$") {
            return $trimmed.ToUpperInvariant()
        }

        Write-Host "  [错误] 无效串口格式: $trimmed" -ForegroundColor Red
    }
}

function Invoke-CheckOnly {
    Assert-EsptoolReady
    Write-Host "  [1/3] 校验独立版 esptool.exe..." -ForegroundColor Cyan
    & $script:EsptoolExe version
    if ($LASTEXITCODE -ne 0) {
        throw "esptool.exe version 执行失败，退出码: $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "  [2/3] 扫描便携固件目录 ($($script:FirmwareDir))..." -ForegroundColor Cyan
    $files = @(Get-FirmwareFiles)
    if ($files.Count -eq 0) {
        Write-Host "  [提示] 暂未发现 *.bin 固件文件" -ForegroundColor Yellow
    } else {
        foreach ($f in $files) {
            $addr = Resolve-DefaultAddress -FileName $f.Name
            $sizeMb = [math]::Round($f.Length / 1MB, 2)
            Write-Host "  - $($f.Name) | $sizeMb MB ($($f.Length) B) | 默认地址: $addr" -ForegroundColor White
        }
    }

    Write-Host ""
    Write-Host "  [3/3] 枚举当前可用串口..." -ForegroundColor Cyan
    $ports = @(Get-SerialPorts)
    if ($ports.Count -eq 0) {
        Write-Host "  - 未检测到活跃 COM 端口" -ForegroundColor Yellow
    } else {
        foreach ($p in $ports) {
            Write-Host "  - $($p.Port) ($($p.Description))" -ForegroundColor White
        }
    }
    Write-Host ""
    Write-Host "  [完成] 便携烧录环境自检通过，可拷贝至任意 Windows 电脑直接使用。" -ForegroundColor Green
}

function Show-FlashTroubleShooting {
    Write-Host ""
    Write-Host "  [排查提示]" -ForegroundColor Yellow
    Write-Host "  1. 请使用支持数据传输的 USB Type-C 数据线连接开发板串口/USB口。"
    Write-Host "  2. 若提示无法连接或等待下载超时: 按住 BOOT(GPIO0) 不放并重新插拔 Type-C 上电（电池供电时可长按 PWR 关机后按住 BOOT 上电），保持 1-2 秒后松开 BOOT 重试。"
    Write-Host "  3. 请关闭占用同一 COM 口的串口助手、Arduino IDE、idf.py monitor 等软件。"
    Write-Host "  4. 若高波特率通信丢包，可重试并改用 115200 波特率。"
}

# ============================================================================
# 烧录 / 检测 / 擦除操作
# ============================================================================
function Do-FlashFirmware {
    param(
        [string]$CliFirmware = "",
        [string]$CliPort = "",
        [string]$CliAddress = "",
        [int]$CliBaud = 460800,
        [switch]$SkipConfirm
    )

    Assert-EsptoolReady

    $selectedFile = $null
    if (-not [string]::IsNullOrWhiteSpace($CliFirmware)) {
        $candidatePath = $CliFirmware
        if (-not (Test-Path -LiteralPath $candidatePath)) {
            $candidatePath = Join-Path $script:FirmwareDir $CliFirmware
        }
        if (-not (Test-Path -LiteralPath $candidatePath)) {
            throw "找不到指定固件文件: $CliFirmware"
        }
        $selectedFile = Get-Item -LiteralPath $candidatePath
    } else {
        $files = @(Get-FirmwareFiles)
        if ($files.Count -eq 0) {
            throw "firmware 目录中没有 *.bin 固件文件。"
        }

        Write-Host ""
        Write-Host "  可选固件列表 (按修改时间倒序):" -ForegroundColor Cyan
        Write-Host "  ----------------------------------------------------"
        for ($i = 0; $i -lt $files.Count; $i++) {
            $f = $files[$i]
            $addrHint = Resolve-DefaultAddress -FileName $f.Name
            $tag = if ($addrHint -eq "0x0") { "[完整 0x0]  " } else { "[App $addrHint]" }
            $sizeMb = [math]::Round($f.Length / 1MB, 2)
            $timeStr = $f.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
            Write-Host "  [$($i + 1)] $tag $($f.Name)  ($sizeMb MB, $timeStr)" -ForegroundColor White
        }
        Write-Host ""

        $rawIdx = Read-Host "  请选择固件编号 [默认=1]"
        if ([string]::IsNullOrWhiteSpace($rawIdx)) { $rawIdx = "1" }
        $idx = 0
        if (-not [int]::TryParse($rawIdx.Trim(), [ref]$idx) -or $idx -lt 1 -or $idx -gt $files.Count) {
            throw "无效固件编号: $rawIdx"
        }
        $selectedFile = $files[$idx - 1]
    }

    $defaultAddr = Resolve-DefaultAddress -FileName $selectedFile.Name
    if (-not [string]::IsNullOrWhiteSpace($CliAddress)) {
        $targetAddr = $CliAddress.Trim()
    } elseif ($SkipConfirm) {
        $targetAddr = $defaultAddr
    } else {
        $rawAddr = Read-Host "  烧录起始地址 [默认=$defaultAddr]"
        $targetAddr = if ([string]::IsNullOrWhiteSpace($rawAddr)) { $defaultAddr } else { $rawAddr.Trim() }
    }

    $targetPort = Select-SerialPort -ProvidedPort $CliPort

    $targetBaud = $CliBaud
    if (-not $SkipConfirm) {
        $rawBaud = Read-Host "  烧录波特率 [默认=$CliBaud，不稳定可输 115200]"
        if (-not [string]::IsNullOrWhiteSpace($rawBaud)) {
            $parsedBaud = 0
            if (-not [int]::TryParse($rawBaud.Trim(), [ref]$parsedBaud) -or $parsedBaud -lt 9600) {
                throw "无效波特率: $rawBaud"
            }
            $targetBaud = $parsedBaud
        }
    }

    $sizeMb = [math]::Round($selectedFile.Length / 1MB, 2)
    Write-Host ""
    Write-Host "  即将执行烧录:" -ForegroundColor Yellow
    Write-Host "    固件: $($selectedFile.Name) ($sizeMb MB)"
    Write-Host "    地址: $targetAddr"
    Write-Host "    串口: $targetPort"
    Write-Host "    速率: $targetBaud"
    Write-Host ""

    if (-not $SkipConfirm) {
        $confirm = Read-Host "  确认开始烧录? [Y/n]"
        if ($confirm -eq "n" -or $confirm -eq "N") {
            Write-Host "  已取消烧录" -ForegroundColor Gray
            return
        }
    }

    $flashArgs = @(
        "--chip", $script:Chip,
        "-p", $targetPort,
        "-b", "$targetBaud",
        "--before", "default_reset",
        "--after", "hard_reset",
        "write_flash",
        "--flash_mode", $script:FlashMode,
        "--flash_size", $script:FlashSize,
        "--flash_freq", $script:FlashFreq,
        $targetAddr,
        $selectedFile.FullName
    )

    Write-Host "  [信息] 正在写入 Flash，请勿断开 USB 数据线..." -ForegroundColor Gray
    & $script:EsptoolExe @flashArgs
    if ($LASTEXITCODE -ne 0) {
        Show-FlashTroubleShooting
        throw "烧录失败，esptool.exe 退出码: $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "  [成功] 固件烧录完成，开发板已自动复位运行!" -ForegroundColor Green
}

function Do-DetectBoard {
    Assert-EsptoolReady
    $targetPort = Select-SerialPort -ProvidedPort $Port
    Write-Host "  [信息] 正在读取开发板芯片与 Flash 信息 ($targetPort)..." -ForegroundColor Gray
    & $script:EsptoolExe --chip $script:Chip -p $targetPort flash_id
    if ($LASTEXITCODE -ne 0) {
        Show-FlashTroubleShooting
        throw "检测开发板失败，退出码: $LASTEXITCODE"
    }
    Write-Host ""
    Write-Host "  [成功] 芯片连接正常!" -ForegroundColor Green
}

function Do-EraseFlash {
    Assert-EsptoolReady
    $targetPort = Select-SerialPort -ProvidedPort $Port
    Write-Host ""
    Write-Host "  [警告] 擦除整片 Flash 将清除所有固件、Wi-Fi 配网信息与 NVS 数据!" -ForegroundColor Red
    $confirm = Read-Host "  请输入大写的 ERASE 确认整片擦除，其余输入取消"
    if ($confirm -cne "ERASE") {
        Write-Host "  已取消擦除操作" -ForegroundColor Gray
        return
    }

    Write-Host "  [信息] 正在擦除整片 Flash ($targetPort)..." -ForegroundColor Gray
    & $script:EsptoolExe --chip $script:Chip -p $targetPort erase_flash
    if ($LASTEXITCODE -ne 0) {
        Show-FlashTroubleShooting
        throw "擦除 Flash 失败，退出码: $LASTEXITCODE"
    }
    Write-Host ""
    Write-Host "  [成功] Flash 擦除完成，请重新烧录完整固件 (_full_)。" -ForegroundColor Green
}

# ============================================================================
# 主入口
# ============================================================================
try {
    Show-Banner

    if ($CheckOnly) {
        Invoke-CheckOnly
        exit 0
    }

    if (-not [string]::IsNullOrWhiteSpace($Firmware) -or -not [string]::IsNullOrWhiteSpace($Port)) {
        Do-FlashFirmware -CliFirmware $Firmware -CliPort $Port -CliAddress $Address -CliBaud $Baud -SkipConfirm:$Yes
        exit 0
    }

    while ($true) {
        Show-Menu
        $choice = Read-Host "  请选择 [0-3]"
        try {
            switch ($choice.Trim()) {
                "1" { Do-FlashFirmware }
                "2" { Do-DetectBoard }
                "3" { Do-EraseFlash }
                "0" { Write-Host "  已退出" -ForegroundColor Gray; exit 0 }
                default { Write-Host "  [提示] 无效选择，请输入 0-3" -ForegroundColor Yellow }
            }
        } catch {
            Write-Host ""
            Write-Host "  [错误] $($_.Exception.Message)" -ForegroundColor Red
        }
        Write-Host ""
    }
} catch {
    Write-Host ""
    Write-Host "  [错误] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
