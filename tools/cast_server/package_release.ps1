# ============================================================================
# 程序说明: 将无线投屏接收端 CastServer.exe、投屏源 Quote0Local.exe 及 .env
#           打包到独立 release 发布目录，方便复制到任意 Windows 10/11 电脑运行
# ============================================================================
[CmdletBinding()]
param(
    [string]$OutputDir = "Y:\Dev\Waveshare\ESP32-S3-RLCD-4.2\release",
    [string]$Quote0Dir = "C:\Users\patde\Documents\GitHub\pat-quote0-burnout",
    [switch]$RebuildQuote0,
    [switch]$SkipCastBuild
)

$ErrorActionPreference = "Stop"

# ============================================================================
# 文件写入与工具解析辅助函数
# ============================================================================
function Write-Utf8NoBomCrlf {
    param(
        [string]$Path,
        [string]$Text
    )
    $clean = $Text.TrimStart([char]0xFEFF)
    $norm = ($clean -replace "`r`n", "`n") -replace "`n", "`r`n"
    if (-not $norm.EndsWith("`r`n")) {
        $norm += "`r`n"
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $norm, $utf8NoBom)
}

function Write-Utf8BomCrlf {
    param(
        [string]$Path,
        [string]$Text
    )
    $clean = $Text.TrimStart([char]0xFEFF)
    $norm = ($clean -replace "`r`n", "`n") -replace "`n", "`r`n"
    if (-not $norm.EndsWith("`r`n")) {
        $norm += "`r`n"
    }
    $utf8Bom = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText($Path, $norm, $utf8Bom)
}

function Resolve-PythonWithPyInstaller {
    $candidates = @()
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pyCmd) {
        $candidates += $pyCmd.Source
    }
    $candidates += "C:\Program Files\Python313\python.exe"

    foreach ($cand in ($candidates | Select-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($cand) -or -not (Test-Path -LiteralPath $cand)) {
            continue
        }
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $cand -c "import PyInstaller, PIL" 2>$null
            $ok = ($LASTEXITCODE -eq 0)
        } finally {
            $ErrorActionPreference = $prevPref
        }
        if ($ok) {
            return $cand
        }
    }
    throw "未找到同时安装 PyInstaller 与 Pillow 的 Python 环境，无法执行打包。"
}

function Invoke-PyInstallerBuild {
    param(
        [string]$PythonExe,
        [string]$WorkingDir,
        [string[]]$PyInstallerArgs
    )
    Push-Location -LiteralPath $WorkingDir
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $PythonExe -m PyInstaller @PyInstallerArgs
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
        Pop-Location
    }
    if ($code -ne 0) {
        throw "PyInstaller 打包失败 ($WorkingDir)，退出码: $code"
    }
}

function Prepare-Quote0EnvFile {
    param(
        [string]$SourcePath,
        [string]$DestPath
    )
    # 静默读取并处理配置，绝不在控制台回显密钥内容
    $raw = [System.IO.File]::ReadAllText($SourcePath, [System.Text.Encoding]::UTF8).TrimStart([char]0xFEFF)
    $lines = @(($raw -replace "`r`n", "`n") -split "`n")

    $hasEnabled = $false
    $updatedLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match "^(\s*(?:export\s+)?)RLCD_CAST_ENABLED\s*=([^#]*)(#.*)?$") {
            $hasEnabled = $true
            $prefix = $matches[1]
            $comment = if ($matches[3]) { " " + $matches[3].TrimStart() } else { "" }
            $updatedLines.Add("${prefix}RLCD_CAST_ENABLED=1${comment}")
        } else {
            $updatedLines.Add($line)
        }
    }

    if (-not $hasEnabled) {
        if ($updatedLines.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($updatedLines[$updatedLines.Count - 1])) {
            $updatedLines.Add("")
        }
        $updatedLines.Add("# ESP32-S3 RLCD 无线投屏自动推送配置（随 release 发布包默认开启）")
        $updatedLines.Add("export RLCD_CAST_ENABLED=1")
        $updatedLines.Add('export RLCD_CAST_URL="http://127.0.0.1:8787/push"')
        $updatedLines.Add("export RLCD_CAST_WIDTH=400")
        $updatedLines.Add("export RLCD_CAST_HEIGHT=300")
        $updatedLines.Add('export RLCD_CAST_TEMPLATE="sync"')
        $updatedLines.Add("export RLCD_CAST_INVERT=0")
        $updatedLines.Add("export RLCD_CAST_PUSH_NOW=1")
        $updatedLines.Add("export RLCD_CAST_TIMEOUT=5")
    }

    # local_app.py 使用 utf-8 读取 .env，必须写入无 BOM 的 UTF-8
    Write-Utf8NoBomCrlf -Path $DestPath -Text ($updatedLines -join "`r`n")
    Set-ItemProperty -LiteralPath $DestPath -Name Attributes -Value ([System.IO.FileAttributes]::Normal)
}

# ============================================================================
# 主打包流程
# ============================================================================
try {
    $castDir       = $PSScriptRoot
    $castSpec      = Join-Path $castDir "CastServer.spec"
    $castWorkDir   = Join-Path $castDir "build"
    $castDistDir   = Join-Path $castDir "dist"
    $castExeBuilt  = Join-Path $castDistDir "CastServer.exe"

    $quote0DistDir = Join-Path $Quote0Dir "dist"
    $quote0ExeBuilt = Join-Path $quote0DistDir "Quote0Local.exe"

    $needPyInstaller = (-not $SkipCastBuild) -or $RebuildQuote0 -or (-not (Test-Path -LiteralPath $quote0ExeBuilt))
    $pyExe = $null
    if ($needPyInstaller) {
        $pyExe = Resolve-PythonWithPyInstaller
        Write-Host "  [信息] 使用打包 Python: $pyExe" -ForegroundColor Gray
    }

    # 1. 构建或校验 CastServer.exe
    if (-not $SkipCastBuild) {
        if (-not (Test-Path -LiteralPath $castSpec)) {
            throw "未找到打包规格文件: $castSpec"
        }
        Write-Host "  [1/4] 正在打包 CastServer.exe..." -ForegroundColor Cyan
        Invoke-PyInstallerBuild -PythonExe $pyExe -WorkingDir $castDir -PyInstallerArgs @(
            "--noconfirm",
            "--clean",
            "--workpath", $castWorkDir,
            "--distpath", $castDistDir,
            $castSpec
        )
    }
    if (-not (Test-Path -LiteralPath $castExeBuilt)) {
        throw "未找到已打包的 CastServer.exe: $castExeBuilt"
    }

    # 2. 校验或重新打包 Quote0Local.exe
    if ($RebuildQuote0 -or (-not (Test-Path -LiteralPath $quote0ExeBuilt))) {
        $quote0Spec = Join-Path $Quote0Dir "Quote0Local.spec"
        if (-not (Test-Path -LiteralPath $quote0Spec)) {
            throw "未找到 Quote0Local.spec: $quote0Spec"
        }
        Write-Host "  [2/4] 正在打包 Quote0Local.exe..." -ForegroundColor Cyan
        Invoke-PyInstallerBuild -PythonExe $pyExe -WorkingDir $Quote0Dir -PyInstallerArgs @(
            "--noconfirm",
            $quote0Spec
        )
    } else {
        Write-Host "  [2/4] 复用已有 Quote0Local.exe: $quote0ExeBuilt" -ForegroundColor Gray
    }
    if (-not (Test-Path -LiteralPath $quote0ExeBuilt)) {
        throw "未找到 Quote0Local.exe: $quote0ExeBuilt"
    }

    # 3. 装配 release 输出目录
    Write-Host "  [3/4] 正在组装便携发布目录: $OutputDir" -ForegroundColor Cyan
    $resolvedOut   = [System.IO.Path]::GetFullPath($OutputDir)
    $outCastDir    = Join-Path $resolvedOut "cast_server"
    $outQuoteDir   = Join-Path $resolvedOut "quote0_local"

    New-Item -ItemType Directory -Path $resolvedOut -Force | Out-Null
    New-Item -ItemType Directory -Path $outCastDir -Force | Out-Null
    New-Item -ItemType Directory -Path $outQuoteDir -Force | Out-Null

    # 3a. 写入 cast_server 子目录
    Copy-Item -LiteralPath $castExeBuilt -Destination (Join-Path $outCastDir "CastServer.exe") -Force
    $castExampleSrc = Join-Path $castDir ".env.example"
    if (Test-Path -LiteralPath $castExampleSrc) {
        $castExRaw = [System.IO.File]::ReadAllText($castExampleSrc, [System.Text.Encoding]::UTF8)
        Write-Utf8NoBomCrlf -Path (Join-Path $outCastDir ".env.example") -Text $castExRaw
    }

    $castEnvSrc = Join-Path $castDir ".env"
    if (-not (Test-Path -LiteralPath $castEnvSrc)) {
        $castEnvSrc = $castExampleSrc
    }
    if (Test-Path -LiteralPath $castEnvSrc) {
        $castEnvRaw = [System.IO.File]::ReadAllText($castEnvSrc, [System.Text.Encoding]::UTF8)
        $outCastEnv = Join-Path $outCastDir ".env"
        Write-Utf8NoBomCrlf -Path $outCastEnv -Text $castEnvRaw
        Set-ItemProperty -LiteralPath $outCastEnv -Name Attributes -Value ([System.IO.FileAttributes]::Normal)
    }

    # 3b. 写入 quote0_local 子目录（保留用户配置，清除 Hidden 属性，启用 RLCD_CAST_ENABLED=1）
    Copy-Item -LiteralPath $quote0ExeBuilt -Destination (Join-Path $outQuoteDir "Quote0Local.exe") -Force

    $quote0SampleSrc = Join-Path $Quote0Dir ".env.sample"
    if (-not (Test-Path -LiteralPath $quote0SampleSrc)) {
        $quote0SampleSrc = Join-Path $quote0DistDir ".env.sample"
    }
    if (Test-Path -LiteralPath $quote0SampleSrc) {
        $sampleRaw = [System.IO.File]::ReadAllText($quote0SampleSrc, [System.Text.Encoding]::UTF8)
        Write-Utf8NoBomCrlf -Path (Join-Path $outQuoteDir ".env.sample") -Text $sampleRaw
    }

    $quote0EnvSrc = $null
    foreach ($candEnv in @(
        (Join-Path $quote0DistDir ".env"),
        (Join-Path $Quote0Dir ".env"),
        $quote0SampleSrc
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candEnv) -and (Test-Path -LiteralPath $candEnv)) {
            $quote0EnvSrc = $candEnv
            break
        }
    }
    if ($null -eq $quote0EnvSrc) {
        throw "未找到可用于打包的 Quote0Local .env 或 .env.sample 文件。"
    }
    Prepare-Quote0EnvFile -SourcePath $quote0EnvSrc -DestPath (Join-Path $outQuoteDir ".env")

    # 4. 生成根目录启动批处理与使用说明
    Write-Host "  [4/4] 生成启动批处理与使用说明文档..." -ForegroundColor Cyan

    $startCastBat = @'
@echo off
chcp 65001 >nul
setlocal
start "" /D "%~dp0cast_server" "%~dp0cast_server\CastServer.exe"
exit /b 0
'@
    Write-Utf8NoBomCrlf -Path (Join-Path $resolvedOut "start_cast_server.bat") -Text $startCastBat

    $startQuoteBat = @'
@echo off
chcp 65001 >nul
setlocal
start "" /D "%~dp0quote0_local" "%~dp0quote0_local\Quote0Local.exe"
exit /b 0
'@
    Write-Utf8NoBomCrlf -Path (Join-Path $resolvedOut "start_quote0_local.bat") -Text $startQuoteBat

    $startAllBat = @'
@echo off
chcp 65001 >nul
setlocal
start "" /D "%~dp0cast_server" "%~dp0cast_server\CastServer.exe"
ping -n 3 127.0.0.1 >nul
start "" /D "%~dp0quote0_local" "%~dp0quote0_local\Quote0Local.exe"
start "" "http://127.0.0.1:8787/"
exit /b 0
'@
    Write-Utf8NoBomCrlf -Path (Join-Path $resolvedOut "start_all.bat") -Text $startAllBat

    $readmeMd = @'
# ESP32-S3-RLCD-4.2 无线投屏与 Quote0Local 便携运行包

本目录为免安装绿色运行包，整文件夹拷贝到任意 Windows 10 或 Windows 11 电脑即可使用，无需在目标电脑安装 Python 或配置虚拟环境。

## 目录结构

```text
release/
├── start_all.bat           一键依次启动 CastServer、Quote0Local 并打开网页控台
├── start_cast_server.bat   单独启动无线投屏接收端与网页控台（隐藏到系统托盘）
├── start_quote0_local.bat  单独启动 Quote0Local 额度看板（自动向本机推流）
├── 使用说明.md             本说明文档
├── cast_server/
│   ├── CastServer.exe      无线投屏接收端与网页控台（无 CMD 黑框，常驻右下角托盘）
│   ├── cast_server.log     后台运行日志（自动生成，可在托盘菜单查看）
│   ├── .env                投屏后台配置文件（目标设备 IP、端口、抖动参数等）
│   └── .env.example        后台配置默认模板
└── quote0_local/
    ├── Quote0Local.exe     Quote0Local 桌面端与 RLCD 横屏渲染推送源
    ├── .env                Quote0Local 配置文件（已内置启用 RLCD_CAST_ENABLED=1）
    └── .env.sample         Quote0Local 配置参考模板
```

`cast_server\.env` 与 `quote0_local\.env` 各自放在独立子目录中，避免网页控台保存后台参数时互相影响。

## 快速启动（任意 Windows 电脑）

- 双击 `start_all.bat`：在系统右下角托盘静默启动 `CastServer.exe`（监听 `8787` 端口，无 CMD 命令行黑框），随后启动 `Quote0Local.exe` 并在浏览器打开控台 `http://127.0.0.1:8787/`。
- 仅需启动某一端时，分别双击 `start_cast_server.bat` 或 `start_quote0_local.bat`。
- `CastServer` 系统托盘操作：双击托盘图标打开网页控台；右键菜单支持「打开网页控台」「查看运行日志」（打开 `cast_server\cast_server.log`）与「退出」。
- 本机开发调试也可不通过本发布包，直接在源码目录运行 `python tools\cast_server\cast_server.py`（加 `--tray` 可同样以托盘方式启动）。

## 跨电脑配置调整

1. **填写设备 IP 地址**：
   - 打开 `http://127.0.0.1:8787/` 在右侧「目标设备 IP」栏填入 ESP32-S3-RLCD-4.2 屏幕第四屏显示的 IPv4 地址，保存配置并点击「恢复推流」即可。
   - 也可直接用记事本修改 `cast_server\.env` 中的 `DEVICE_IP`。
2. **调整 Quote0Local 投屏参数**：
   - 打开 `quote0_local\.env` 可调整 `RLCD_CAST_TEMPLATE`（`sync` / `bar` / `ring`）、`RLCD_CAST_INVERT`（`0` 白底 / `1` 黑底）、`RLCD_CAST_URL` 等参数。
3. **安全提醒**：
   - `quote0_local\.env` 中保留了个人 API Key 等密钥配置，请勿将该文件夹或 `.env` 文件上传至公开仓库或网盘分享。

## 常见问题排查

- **Windows 防火墙弹窗或端口拦截**：若局域网其他机器访问控台网页，请在防火墙弹窗允许 `CastServer.exe` 通信，或放行 TCP `8787` 端口。
- **8787 端口被占用**：关闭本机已运行的另一个 `CastServer` 实例，或修改 `cast_server\.env` 的 `LISTEN_PORT`。
- **新电脑运行报安全阻止**：从压缩包或网络下载后如遇系统拦截，请在 `release` 目录运行 PowerShell 命令解除文件锁定：

```powershell
Get-ChildItem -Recurse | Unblock-File
```

## 开发机重新打包更新

修改 `cast_server.py`、`web/` 或 `Quote0Local` 源码后，在开发机运行：

```bat
Y:\Dev\Waveshare\ESP32-S3-RLCD-4.2\xiaozhi-esp32\tools\cast_server\package_release.bat
```

如同时更新了 `Quote0Local` 源码并需要强制重新打包 `Quote0Local.exe`，附加 `-RebuildQuote0` 参数。
'@
    Write-Utf8BomCrlf -Path (Join-Path $resolvedOut "使用说明.md") -Text $readmeMd

    $castSizeMb  = [math]::Round((Get-Item -LiteralPath (Join-Path $outCastDir "CastServer.exe")).Length / 1MB, 2)
    $quoteSizeMb = [math]::Round((Get-Item -LiteralPath (Join-Path $outQuoteDir "Quote0Local.exe")).Length / 1MB, 2)
    Write-Host ""
    Write-Host "  ====================================================" -ForegroundColor Green
    Write-Host "  独立便携发布目录生成完成: $resolvedOut" -ForegroundColor Green
    Write-Host "  CastServer.exe : $castSizeMb MB" -ForegroundColor Cyan
    Write-Host "  Quote0Local.exe: $quoteSizeMb MB" -ForegroundColor Cyan
    Write-Host "  ====================================================" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "  [错误] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
