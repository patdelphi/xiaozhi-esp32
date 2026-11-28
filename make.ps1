# ============================================================================
# 程序说明: Waveshare ESP32-S3-RLCD-4.2 一键编译、打包便携烧录包与调试脚本
# 适用硬件: ESP32-S3 / 16MB Flash (DIO, 80MHz) / App 起始偏移 0x20000
# 运行环境: Windows PowerShell 5.1+ 或 PowerShell 7+ (由 make.bat 自动调度)
# ============================================================================
[CmdletBinding()]
param(
    [ValidateSet("menu", "build", "pack", "flash", "monitor", "clean")]
    [string]$Action = "menu",

    [ValidateSet("full", "app")]
    [string]$BuildType = "full",

    [string]$PackageDir = "",

    [string]$Port = "",

    [int]$Baud = 460800,

    [string]$StandaloneEsptool = "",

    [switch]$Clean
)

$ErrorActionPreference = "Stop"

# ============================================================================
# 全局工程与芯片参数配置
# ============================================================================
$script:ProjectRoot = $PSScriptRoot
$script:ProjectName = "xiaozhi-rlcd42"
$script:BinaryName  = "xiaozhi"
$script:BoardName   = "esp32-s3-rlcd-4.2"
$script:BoardType   = "waveshare/esp32-s3-rlcd-4.2"
$script:Chip        = "esp32s3"
$script:FlashMode   = "dio"
$script:FlashSize   = "16MB"
$script:FlashFreq   = "80m"
$script:AppOffset   = "0x20000"
$script:AssetsOffset = "0x800000"
$script:CmdExe      = "C:\WINDOWS\System32\cmd.exe"
$script:BuildDir    = Join-Path $script:ProjectRoot "build"
$script:DefaultPackageDir = Join-Path $script:ProjectRoot "flash_package"
$script:IdfLoaded   = $false

# 若未指定便携输出目录，默认输出至项目根目录下 flash_package
if ([string]::IsNullOrWhiteSpace($PackageDir)) {
    $script:ResolvedPackageDir = [System.IO.Path]::GetFullPath($script:DefaultPackageDir)
} else {
    if ([System.IO.Path]::IsPathRooted($PackageDir)) {
        $script:ResolvedPackageDir = [System.IO.Path]::GetFullPath($PackageDir)
    } else {
        $script:ResolvedPackageDir = [System.IO.Path]::GetFullPath((Join-Path $script:ProjectRoot $PackageDir))
    }
}
$script:ResolvedDefaultPackageDir = [System.IO.Path]::GetFullPath($script:DefaultPackageDir)

# ============================================================================
# 基础辅助函数
# ============================================================================
function Show-Banner {
    Write-Host ""
    Write-Host "  ====================================================" -ForegroundColor Cyan
    Write-Host "    ESP32-S3-RLCD-4.2 一键编译与便携烧录包工具" -ForegroundColor Cyan
    Write-Host "    板型: $($script:BoardName) | Flash: $($script:FlashSize) $($script:FlashMode) $($script:FlashFreq)" -ForegroundColor DarkGray
    Write-Host "    便携目录: $($script:ResolvedPackageDir)" -ForegroundColor DarkGray
    Write-Host "  ====================================================" -ForegroundColor Cyan
    Write-Host ""
}

function Show-Menu {
    Write-Host "  [1] 编译固件并打包便携烧录目录" -ForegroundColor Yellow
    Write-Host "  [2] 烧录固件" -ForegroundColor Yellow
    Write-Host "  [3] 仅刷新便携烧录目录 (无需重编)" -ForegroundColor Yellow
    Write-Host "  [4] 串口监视器" -ForegroundColor Yellow
    Write-Host "  [5] 清理编译目录" -ForegroundColor Yellow
    Write-Host "  [0] 退出" -ForegroundColor DarkGray
    Write-Host ""
}

function Get-Timestamp {
    return Get-Date -Format "yyyyMMdd_HHmmss"
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

function Write-Utf8BomCrlf {
    param(
        [string]$Path,
        [string[]]$Lines
    )
    # 统一使用 CRLF 与带 BOM 的 UTF-8 编码写入文件
    $text = ($Lines -join "`r`n") + "`r`n"
    $utf8Bom = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText($Path, $text, $utf8Bom)
}

function Get-SerialPorts {
    # 枚举本机串口并尝试关联即插即用设备友好名称
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
        # 个别精简系统可能缺少 CIM 服务，回退为仅显示端口号
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
        Write-Host "  检测到的可用串口:" -ForegroundColor Cyan
        if ($ports.Count -eq 0) {
            Write-Host "  [提示] 暂未检测到 COM 串口，请连接数据线或进入下载模式" -ForegroundColor Yellow
        } else {
            for ($i = 0; $i -lt $ports.Count; $i++) {
                Write-Host "  [$($i + 1)] $($ports[$i].Port)  - $($ports[$i].Description)" -ForegroundColor White
            }
        }
        Write-Host "  [R] 刷新串口列表" -ForegroundColor DarkGray
        Write-Host ""

        $defaultTip = if ($ports.Count -gt 0) { " [默认=1]" } else { "" }
        $inputVal = Read-Host "  请输入编号、串口号(如 COM4 或 4)，或按 R 刷新$defaultTip"

        if ($inputVal -eq "r" -or $inputVal -eq "R") {
            continue
        }

        if ([string]::IsNullOrWhiteSpace($inputVal)) {
            if ($ports.Count -gt 0) {
                return $ports[0].Port
            }
            Write-Host "  [错误] 当前没有可用默认串口，请手动输入或按 R 刷新" -ForegroundColor Red
            continue
        }

        $trimmed = $inputVal.Trim()
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

        Write-Host "  [错误] 无法识别的串口输入: $trimmed" -ForegroundColor Red
    }
}

# ============================================================================
# ESP-IDF 环境加载与构建保护
# ============================================================================
function Load-IDF {
    if ($script:IdfLoaded) {
        if (Test-Path -LiteralPath $script:CmdExe) {
            $env:ComSpec = $script:CmdExe
        }
        return
    }

    # 固定 ComSpec 为 cmd.exe，防止 Ninja 子规则错误调用 PowerShell 执行 cd /D
    if (Test-Path -LiteralPath $script:CmdExe) {
        $env:ComSpec = $script:CmdExe
    }

    # 本工程基于 xiaozhi v2.6.0（要求 ESP-IDF >= 6.0.1）；若当前会话仍继承了旧版 v5.x，自动切换到本机 v6.0.1
    $fastIdfPath = "D:\esp\v6.0.1\esp-idf"
    $fastToolsDir = "D:\Espressif\tools"
    $fastVenv = Join-Path $fastToolsDir "python\v6.0.1\venv"
    $fastPy = Join-Path $fastVenv "Scripts\python.exe"
    $legacyIdf = (-not [string]::IsNullOrWhiteSpace($env:IDF_PATH)) -and ($env:IDF_PATH -match "v[1-5]\.")
    if (($legacyIdf -or [string]::IsNullOrWhiteSpace($env:IDF_PATH) -or $env:IDF_PATH -eq $fastIdfPath) -and
        (Test-Path -LiteralPath $fastIdfPath) -and (Test-Path -LiteralPath $fastPy)) {
        $env:IDF_PATH = $fastIdfPath
        $env:IDF_TOOLS_PATH = $fastToolsDir
        $env:IDF_COMPONENT_LOCAL_STORAGE_URL = "file://$fastToolsDir"
        $env:IDF_PYTHON_ENV_PATH = $fastVenv
        $env:ESP_IDF_VERSION = "6.0.1"
        $env:ESP_ROM_ELF_DIR = Join-Path $fastToolsDir "esp-rom-elfs\20241011"
        $env:OPENOCD_SCRIPTS = Join-Path $fastToolsDir "openocd-esp32\v0.12.0-esp32-20260304\openocd-esp32\share\openocd\scripts"

        $toolBins = @(
            "$fastToolsDir\ccache\4.12.1\ccache-4.12.1-windows-x86_64",
            "$fastToolsDir\cmake\4.0.3\bin",
            "$fastToolsDir\ninja\1.12.1",
            "$fastToolsDir\dfu-util\0.11\dfu-util-0.11-win64",
            "$fastToolsDir\esp-clang\esp-20.1.1_20250829\esp-clang\bin",
            "$fastToolsDir\esp-rom-elfs\20241011",
            "$fastToolsDir\esp32ulp-elf\2.38_20240113\esp32ulp-elf\bin",
            "$fastToolsDir\idf-exe\1.0.3",
            "$fastToolsDir\openocd-esp32\v0.12.0-esp32-20260304\openocd-esp32\bin",
            "$fastToolsDir\riscv32-esp-elf\esp-15.2.0_20251204\riscv32-esp-elf\bin",
            "$fastToolsDir\xtensa-esp-elf-gdb\16.3_20250913\xtensa-esp-elf-gdb\bin",
            "$fastToolsDir\xtensa-esp-elf\esp-15.2.0_20251204\xtensa-esp-elf\bin",
            "$fastVenv\Scripts",
            "$fastIdfPath\tools"
        ) | Where-Object { Test-Path -LiteralPath $_ }
        $env:PATH = ($toolBins -join ";") + ";" + $env:PATH
        Write-Host "  [信息] 加载 ESP-IDF v6.0.1 工具链: $fastIdfPath" -ForegroundColor Gray
        $script:IdfLoaded = $true
        return
    }

    if ([string]::IsNullOrWhiteSpace($env:IDF_TOOLS_PATH) -and (Test-Path -LiteralPath "D:\Espressif\tools")) {
        $env:IDF_TOOLS_PATH = "D:\Espressif\tools"
    }
    if ([string]::IsNullOrWhiteSpace($env:IDF_PATH) -and (Test-Path -LiteralPath $fastIdfPath)) {
        $env:IDF_PATH = $fastIdfPath
    }

    $exportCandidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:IDF_PATH)) {
        $exportCandidates += (Join-Path $env:IDF_PATH "export.ps1")
    }
    $exportCandidates += "D:\esp\v6.0.1\esp-idf\export.ps1"
    $exportCandidates += "C:\esp\v6.0.1\esp-idf\export.ps1"

    $chosenScript = $null
    foreach ($cand in $exportCandidates) {
        if (-not [string]::IsNullOrWhiteSpace($cand) -and (Test-Path -LiteralPath $cand)) {
            $chosenScript = $cand
            break
        }
    }

    if ($null -eq $chosenScript) {
        throw "未找到可用的 ESP-IDF 激活脚本，请检查 D:\esp\v6.0.1\esp-idf\export.ps1 或 IDF_PATH / IDF_TOOLS_PATH 环境变量。"
    }

    Write-Host "  [信息] 加载 ESP-IDF 环境: $chosenScript" -ForegroundColor Gray
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $global:LASTEXITCODE = 0
        & $chosenScript
        $actExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
    }

    if ($actExit -ne 0 -or [string]::IsNullOrWhiteSpace($env:IDF_PATH)) {
        throw "加载 ESP-IDF 环境失败 (脚本: $chosenScript，退出码: $actExit)。"
    }

    if (Test-Path -LiteralPath $script:CmdExe) {
        $env:ComSpec = $script:CmdExe
    }
    $script:IdfLoaded = $true
}

function Invoke-IdfPy {
    param([string[]]$IdfArgs)

    if (Test-Path -LiteralPath $script:CmdExe) {
        $env:ComSpec = $script:CmdExe
    }

    $idfPyScript = Join-Path $env:IDF_PATH "tools\idf.py"
    if (-not (Test-Path -LiteralPath $idfPyScript)) {
        throw "未找到 idf.py 脚本: $idfPyScript"
    }

    # 直接定位虚拟环境 Python 调用 idf.py，避免子作用域中未全局导出的 idf.py 函数失效
    $idfPython = $null
    if (-not [string]::IsNullOrWhiteSpace($env:IDF_PYTHON_ENV_PATH)) {
        $candPy = Join-Path $env:IDF_PYTHON_ENV_PATH "Scripts\python.exe"
        if (Test-Path -LiteralPath $candPy) {
            $idfPython = $candPy
        }
    }
    if ($null -eq $idfPython) {
        $pyCmd = Get-Command python -ErrorAction SilentlyContinue
        if ($null -ne $pyCmd) {
            $idfPython = $pyCmd.Source
        }
    }
    if ($null -eq $idfPython) {
        throw "未找到可用的 ESP-IDF Python 解释器。"
    }

    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $idfPython $idfPyScript @IdfArgs
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
    }

    if ($exitCode -ne 0) {
        throw "idf.py $($IdfArgs -join ' ') 失败，退出码: $exitCode"
    }
}

function Repair-BuildShell {
    $ninjaFile = Join-Path $script:BuildDir "build.ninja"
    if (-not (Test-Path -LiteralPath $ninjaFile)) {
        return
    }

    $content = Get-Content -LiteralPath $ninjaFile -Raw
    if ($content -match "pwsh\.exe\s+/C" -or $content -match "powershell\.exe\s+/C") {
        Write-Host "  [信息] 检测到 build.ninja 包含 PowerShell 子命令，正在重配为 cmd.exe..." -ForegroundColor Yellow
        if (Test-Path -LiteralPath $script:CmdExe) {
            $env:ComSpec = $script:CmdExe
        }
        Invoke-IdfPy @("reconfigure")
    }
}

function Ensure-BoardConfig {
    $sdkconfigPath = Join-Path $script:ProjectRoot "sdkconfig"
    $boardMacro = "CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_RLCD_4_2=y"
    $wakeLines = @(
        "CONFIG_WAKE_WORD_DISABLED=n",
        "CONFIG_USE_AFE_WAKE_WORD=y",
        "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y"
    )

    if (-not (Test-Path -LiteralPath $sdkconfigPath)) {
        Write-Host "  [信息] 未找到 sdkconfig，初始化目标芯片 $($script:Chip) 与板型配置..." -ForegroundColor Yellow
        Remove-Item Env:\IDF_TARGET -ErrorAction SilentlyContinue
        Invoke-IdfPy @("set-target", $script:Chip)

        $appendLines = @(
            "",
            "# Append by make.ps1 for waveshare-s3-rlcd-4.2",
            $boardMacro,
            "CONFIG_USE_WECHAT_MESSAGE_STYLE=n",
            "CONFIG_USE_DEVICE_AEC=y"
        ) + $wakeLines
        Add-Content -LiteralPath $sdkconfigPath -Value $appendLines -Encoding Ascii
        return
    }

    $sdkContent = Get-Content -LiteralPath $sdkconfigPath -Raw
    if ($sdkContent -notmatch [regex]::Escape($boardMacro)) {
        throw "当前 sdkconfig 未启用 $boardMacro，请先执行清理或在 menuconfig 中选择 Waveshare ESP32-S3-RLCD-4.2 开发板。"
    }

    if (($sdkContent -notmatch "(?m)^CONFIG_USE_AFE_WAKE_WORD=y") -or
        ($sdkContent -notmatch "(?m)^CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y")) {
        Write-Host "  [信息] 更新 sdkconfig 启用唤醒词: 你好小智 (wn9_nihaoxiaozhi_tts)..." -ForegroundColor Yellow
        $updated = $sdkContent -replace "(?m)^CONFIG_WAKE_WORD_DISABLED=y\r?$", "# CONFIG_WAKE_WORD_DISABLED is not set"
        $updated = $updated -replace "(?m)^# CONFIG_USE_AFE_WAKE_WORD is not set\r?$", "CONFIG_USE_AFE_WAKE_WORD=y"
        $updated = $updated -replace "(?m)^# CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS is not set\r?$", "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y"
        if ($updated -notmatch "(?m)^CONFIG_USE_AFE_WAKE_WORD=y") { $updated += "`r`nCONFIG_USE_AFE_WAKE_WORD=y" }
        if ($updated -notmatch "(?m)^CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y") { $updated += "`r`nCONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y" }
        [System.IO.File]::WriteAllText($sdkconfigPath, $updated, (New-Object System.Text.UTF8Encoding($false)))
    }
}

# ============================================================================
# 独立烧录工具解析与便携目录打包
# ============================================================================
function Resolve-StandaloneEsptool {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($StandaloneEsptool)) {
        $candidates += $StandaloneEsptool
    }
    $candidates += (Join-Path $script:ResolvedPackageDir "tools\esptool.exe")
    $candidates += (Join-Path $script:ResolvedDefaultPackageDir "tools\esptool.exe")
    $candidates += "Y:\Dev\Cardputer\M5Burner-v3-beta-win-x64\packages\tool\esptool.exe"

    foreach ($cand in $candidates) {
        if ([string]::IsNullOrWhiteSpace($cand) -or -not (Test-Path -LiteralPath $cand)) {
            continue
        }
        $item = Get-Item -LiteralPath $cand
        # 严格校验文件大小大于 2MB，排除仅 108KB、依赖本机 Python 的脚本启动器
        if ($item.Length -gt 2MB) {
            return $item.FullName
        }
    }
    return $null
}

function Export-FlashPackage {
    param(
        [ValidateSet("full", "app")]
        [string]$PackageType = "full"
    )

    $appSource = Join-Path $script:BuildDir "$($script:BinaryName).bin"
    if (-not (Test-Path -LiteralPath $appSource)) {
        throw "未找到 App 固件 $appSource，请先执行编译。"
    }

    $mergedSource = Join-Path $script:BuildDir "merged-binary.bin"
    if ($PackageType -eq "full" -and -not (Test-Path -LiteralPath $mergedSource)) {
        throw "未找到完整合并固件 $mergedSource，请先运行编译(full)或执行 idf.py merge-bin。"
    }

    $srcEsptool = Resolve-StandaloneEsptool
    if ($null -eq $srcEsptool) {
        throw "未找到独立版 Windows esptool.exe (大小需大于 2MB)。请通过 -StandaloneEsptool 指定，或预先放到 flash_package\tools\esptool.exe。"
    }

    $toolsDir    = Join-Path $script:ResolvedPackageDir "tools"
    $firmwareDir = Join-Path $script:ResolvedPackageDir "firmware"
    New-Item -ItemType Directory -Path $script:ResolvedPackageDir -Force | Out-Null
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $firmwareDir -Force | Out-Null

    # 若指定了自定义便携输出目录，同步复制便携引导脚本和说明文档
    $isCustomPackageDir = ($script:ResolvedPackageDir.TrimEnd('\') -ine $script:ResolvedDefaultPackageDir.TrimEnd('\'))
    if ($isCustomPackageDir) {
        $supportFiles = @("flash.bat", "flash.ps1", "烧录说明.md")
        foreach ($sf in $supportFiles) {
            $srcSupport = Join-Path $script:ResolvedDefaultPackageDir $sf
            $dstSupport = Join-Path $script:ResolvedPackageDir $sf
            if (Test-Path -LiteralPath $srcSupport) {
                if ([System.IO.Path]::GetFullPath($srcSupport) -ine [System.IO.Path]::GetFullPath($dstSupport)) {
                    Copy-Item -LiteralPath $srcSupport -Destination $dstSupport -Force
                }
            } else {
                Write-Host "  [警告] 默认目录缺少随包文件: $srcSupport" -ForegroundColor Yellow
            }
        }
    }

    # 复制独立版 esptool.exe 到便携目录 tools\esptool.exe
    $dstEsptool = Join-Path $toolsDir "esptool.exe"
    $needCopyTool = $true
    if ([System.IO.Path]::GetFullPath($srcEsptool) -ieq [System.IO.Path]::GetFullPath($dstEsptool)) {
        $needCopyTool = $false
    } elseif (Test-Path -LiteralPath $dstEsptool) {
        $srcHash = (Get-FileHash -LiteralPath $srcEsptool -Algorithm SHA256).Hash
        $dstHash = (Get-FileHash -LiteralPath $dstEsptool -Algorithm SHA256).Hash
        if ($srcHash -eq $dstHash) {
            $needCopyTool = $false
        }
    }
    if ($needCopyTool) {
        Copy-Item -LiteralPath $srcEsptool -Destination $dstEsptool -Force
    }

    $ts = Get-Timestamp
    $appDestName = "$($script:ProjectName)_app_${ts}.bin"
    $appDestPath = Join-Path $firmwareDir $appDestName
    Copy-Item -LiteralPath $appSource -Destination $appDestPath -Force

    $appInfo = Get-Item -LiteralPath $appDestPath
    $appHash = (Get-FileHash -LiteralPath $appDestPath -Algorithm SHA256).Hash

    $fullDestName = $null
    $fullDestPath = $null
    $fullInfo = $null
    $fullHash = $null
    if ($PackageType -eq "full") {
        $fullDestName = "$($script:ProjectName)_full_${ts}.bin"
        $fullDestPath = Join-Path $firmwareDir $fullDestName
        Copy-Item -LiteralPath $mergedSource -Destination $fullDestPath -Force
        $fullInfo = Get-Item -LiteralPath $fullDestPath
        $fullHash = (Get-FileHash -LiteralPath $fullDestPath -Algorithm SHA256).Hash
    }

    # 读取 flash_args 记录本次实际分区布局
    $flashArgsPath = Join-Path $script:BuildDir "flash_args"
    $flashArgsLines = @()
    if (Test-Path -LiteralPath $flashArgsPath) {
        $flashArgsLines = @(Get-Content -LiteralPath $flashArgsPath)
    } else {
        $flashArgsLines = @(
            "--flash_mode $($script:FlashMode) --flash_freq $($script:FlashFreq) --flash_size $($script:FlashSize)",
            "0x0 bootloader/bootloader.bin",
            "$($script:AppOffset) $($script:BinaryName).bin",
            "0x8000 partition_table/partition-table.bin",
            "0xd000 ota_data_initial.bin",
            "$($script:AssetsOffset) generated_assets.bin"
        )
    }

    $esptoolVersion = ""
    try {
        $esptoolVersion = (& $dstEsptool version 2>&1 | Select-Object -First 1)
    } catch {
        $esptoolVersion = "standalone esptool.exe"
    }

    $logName = "$($script:ProjectName)_build_${ts}.log"
    $logPath = Join-Path $firmwareDir $logName
    $logLines = @(
        "=== ESP32-S3-RLCD-4.2 固件打包日志 ==="
        "时间戳: $ts"
        "生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "开发板: $($script:BoardName)"
        "芯片: $($script:Chip)"
        "Flash 参数: $($script:FlashSize), $($script:FlashMode), $($script:FlashFreq)"
        "打包模式: $(if ($PackageType -eq 'full') { 'full (完整镜像 + App 镜像)' } else { 'app (仅 App 镜像)' })"
        "独立工具: tools\esptool.exe ($esptoolVersion)"
        ""
        "--- 固件文件 ---"
        "App 固件: firmware\$appDestName ($($appInfo.Length) 字节)"
        "App SHA256: $appHash"
    )
    if ($null -ne $fullInfo) {
        $logLines += "完整固件: firmware\$fullDestName ($($fullInfo.Length) 字节)"
        $logLines += "完整 SHA256: $fullHash"
    }
    $logLines += @(
        ""
        "--- Flash 偏移布局 (build/flash_args) ---"
    )
    $logLines += $flashArgsLines
    $logLines += @(
        ""
        "--- 便携目录手动烧录命令参考 (在 flash_package 目录执行) ---"
    )
    if ($null -ne $fullDestName) {
        $logLines += "完整烧录: .\tools\esptool.exe --chip $($script:Chip) -p COMx -b 460800 --before default_reset --after hard_reset write_flash --flash_mode $($script:FlashMode) --flash_size $($script:FlashSize) --flash_freq $($script:FlashFreq) 0x0 `"firmware\$fullDestName`""
    }
    $logLines += "仅App烧录: .\tools\esptool.exe --chip $($script:Chip) -p COMx -b 460800 --before default_reset --after hard_reset write_flash --flash_mode $($script:FlashMode) --flash_size $($script:FlashSize) --flash_freq $($script:FlashFreq) $($script:AppOffset) `"firmware\$appDestName`""

    Write-Utf8BomCrlf -Path $logPath -Lines $logLines

    Write-Host ""
    Write-Host "  ====================================================" -ForegroundColor Green
    Write-Host "  便携烧录包生成完成!" -ForegroundColor Green
    Write-Host "  目标目录: $($script:ResolvedPackageDir)" -ForegroundColor Cyan
    Write-Host "  独立工具: $dstEsptool ($([math]::Round((Get-Item -LiteralPath $dstEsptool).Length / 1MB, 2)) MB)" -ForegroundColor Cyan
    Write-Host "  App 固件: firmware\$appDestName ($([math]::Round($appInfo.Length / 1MB, 2)) MB)" -ForegroundColor Cyan
    if ($null -ne $fullInfo) {
        Write-Host "  完整固件: firmware\$fullDestName ($([math]::Round($fullInfo.Length / 1MB, 2)) MB)" -ForegroundColor Cyan
    }
    Write-Host "  打包日志: firmware\$logName" -ForegroundColor Cyan
    Write-Host "  ====================================================" -ForegroundColor Green
}

# ============================================================================
# 核心功能实现
# ============================================================================
function Do-Build {
    param(
        [ValidateSet("full", "app")]
        [string]$SelectedType = "full",
        [switch]$PromptUser,
        [switch]$CleanBuild
    )

    if ($PromptUser) {
        Write-Host ""
        Write-Host "  编译类型:" -ForegroundColor Cyan
        Write-Host "  [1] 完整固件 full (含 bootloader + 分区表 + ota_data + app + assets，烧录 0x0) [默认]"
        Write-Host "  [2] 仅 App 固件 (只更新主程序，烧录 $($script:AppOffset))"
        Write-Host ""
        $choice = Read-Host "  选择 [1/2]"
        if ([string]::IsNullOrWhiteSpace($choice) -or $choice.Trim() -eq "1") {
            $SelectedType = "full"
        } elseif ($choice.Trim() -eq "2") {
            $SelectedType = "app"
        } else {
            throw "无效编译类型选择: $choice"
        }
    }

    Write-Host ""
    Load-IDF

    if ($CleanBuild) {
        if (Test-Path -LiteralPath $script:BuildDir) {
            Write-Host "  [信息] 清理旧编译目录 (idf.py fullclean)..." -ForegroundColor Yellow
            Invoke-IdfPy @("fullclean")
        } else {
            Write-Host "  [信息] 编译目录不存在，跳过 fullclean。" -ForegroundColor Gray
        }
    }

    Repair-BuildShell
    Ensure-BoardConfig

    Write-Host "  [信息] 开始编译 ($($script:BoardName))..." -ForegroundColor Gray
    Invoke-IdfPy @("-DBOARD_NAME=$($script:BoardName)", "-DBOARD_TYPE=$($script:BoardType)", "build")

    if ($SelectedType -eq "full") {
        Write-Host "  [信息] 正在合并完整镜像 (idf.py merge-bin)..." -ForegroundColor Gray
        Invoke-IdfPy @("merge-bin")
    }

    Export-FlashPackage -PackageType $SelectedType
}

function Do-Pack {
    param(
        [ValidateSet("full", "app")]
        [string]$SelectedType = "full"
    )
    Write-Host ""
    Write-Host "  [信息] 使用现有 build\ 产物刷新便携烧录目录..." -ForegroundColor Gray
    Export-FlashPackage -PackageType $SelectedType
}

function Do-Flash {
    $firmwareDir = Join-Path $script:ResolvedPackageDir "firmware"
    $files = @(Get-ChildItem -LiteralPath $firmwareDir -Filter "*.bin" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)

    if ($files.Count -eq 0) {
        Write-Host "  [提示] $firmwareDir 下暂无固件文件，请先执行 [1] 编译打包 或 [3] 刷新便携目录" -ForegroundColor Yellow
        return
    }

    Write-Host ""
    Write-Host "  可用固件 (按时间倒序):" -ForegroundColor Cyan
    Write-Host "  ----------------------------------------------------"
    for ($i = 0; $i -lt $files.Count; $i++) {
        $f = $files[$i]
        $sizeMb = [math]::Round($f.Length / 1MB, 2)
        $timeStr = $f.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        $tag = if ($f.Name -match "_full_|merged") { "[完整 0x0]  " } else { "[App $($script:AppOffset)]" }
        Write-Host "  [$($i + 1)] $tag $($f.Name)  ($sizeMb MB, $timeStr)" -ForegroundColor White
    }
    Write-Host ""

    $choice = Read-Host "  选择固件编号 [默认=1]"
    if ([string]::IsNullOrWhiteSpace($choice)) { $choice = "1" }
    $idxNum = 0
    if (-not [int]::TryParse($choice.Trim(), [ref]$idxNum) -or $idxNum -lt 1 -or $idxNum -gt $files.Count) {
        throw "无效固件编号: $choice"
    }
    $selectedFw = $files[$idxNum - 1]

    $defaultAddr = "0x0"
    if ($selectedFw.Name -match "_app_|xiaozhi\.bin" -and $selectedFw.Name -notmatch "_full_|merged") {
        $defaultAddr = $script:AppOffset
    }
    $addr = Read-Host "  烧录起始地址 [默认=$defaultAddr]"
    if ([string]::IsNullOrWhiteSpace($addr)) { $addr = $defaultAddr } else { $addr = $addr.Trim() }

    $targetPort = Select-SerialPort -ProvidedPort $Port

    Write-Host ""
    Write-Host "  即将烧录:" -ForegroundColor Yellow
    Write-Host "    固件: $($selectedFw.Name)"
    Write-Host "    地址: $addr"
    Write-Host "    串口: $targetPort"
    Write-Host "    速率: $Baud"
    Write-Host ""
    $confirm = Read-Host "  确认开始烧录? [Y/n]"
    if ($confirm -eq "n" -or $confirm -eq "N") {
        Write-Host "  已取消烧录" -ForegroundColor Gray
        return
    }

    $flashArgs = @(
        "--chip", $script:Chip,
        "-p", $targetPort,
        "-b", "$Baud",
        "--before", "default_reset",
        "--after", "hard_reset",
        "write_flash",
        "--flash_mode", $script:FlashMode,
        "--flash_size", $script:FlashSize,
        "--flash_freq", $script:FlashFreq,
        $addr,
        $selectedFw.FullName
    )

    $standaloneExe = Resolve-StandaloneEsptool
    if ($null -ne $standaloneExe) {
        Write-Host "  [信息] 使用独立工具烧录: $standaloneExe" -ForegroundColor Gray
        & $standaloneExe @flashArgs
    } else {
        Write-Host "  [信息] 未找到独立 esptool.exe，回退使用 ESP-IDF Python 模块..." -ForegroundColor Yellow
        Load-IDF
        & python -m esptool @flashArgs
    }

    if ($LASTEXITCODE -ne 0) {
        throw "烧录失败，退出码: $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "  [成功] 烧录完成!" -ForegroundColor Green
}

function Do-Monitor {
    $targetPort = Select-SerialPort -ProvidedPort $Port
    Load-IDF
    Write-Host "  [信息] 启动串口监视器 ($targetPort)，按 Ctrl+] 退出..." -ForegroundColor Gray
    Invoke-IdfPy @("-p", $targetPort, "monitor")
}

function Do-Clean {
    Write-Host ""
    $confirm = Read-Host "  确认清理编译目录 (idf.py fullclean)? [Y/n]"
    if ($confirm -eq "n" -or $confirm -eq "N") {
        Write-Host "  已取消清理" -ForegroundColor Gray
        return
    }
    if (-not (Test-Path -LiteralPath $script:BuildDir)) {
        Write-Host "  [提示] 编译目录不存在，无需清理" -ForegroundColor Yellow
        return
    }
    Load-IDF
    Invoke-IdfPy @("fullclean")
    Write-Host "  [完成] 编译目录已清理" -ForegroundColor Green
}

# ============================================================================
# 主入口调度
# ============================================================================
Push-Location -LiteralPath $script:ProjectRoot
try {
    Show-Banner

    if ($Action -ne "menu") {
        switch ($Action) {
            "build"   { Do-Build -SelectedType $BuildType -CleanBuild:$Clean }
            "pack"    { Do-Pack -SelectedType $BuildType }
            "flash"   { Do-Flash }
            "monitor" { Do-Monitor }
            "clean"   { Do-Clean }
        }
        exit 0
    }

    while ($true) {
        Show-Menu
        $choice = Read-Host "  请选择 [0-5]"
        try {
            switch ($choice.Trim()) {
                "1" { Do-Build -PromptUser }
                "2" { Do-Flash }
                "3" { Do-Pack -SelectedType "full" }
                "4" { Do-Monitor }
                "5" { Do-Clean }
                "0" { Write-Host "  已退出" -ForegroundColor Gray; exit 0 }
                default { Write-Host "  [提示] 无效选择，请输入 0-5" -ForegroundColor Yellow }
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
} finally {
    Pop-Location
}
