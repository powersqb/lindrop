# Package only the EXE and user docs, never runtime data, credentials or source caches.
param([string]$ExePath = '', [string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
if (-not $ExePath) { $ExePath = Join-Path $taskRoot 'outputs\邻传\邻传.exe' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $taskRoot 'outputs\release' }
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Push-Location -LiteralPath $taskRoot
try {
    $taskVersion = (& $taskPython -c 'from lan_drop import __version__; print(__version__)').Trim()
    if ($LASTEXITCODE -ne 0 -or $taskVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid source version' }
    if ($env:GITHUB_REF_TYPE -eq 'tag' -and $env:GITHUB_REF_NAME -ne "v$taskVersion") { throw 'Tag and app version differ' }
    New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
    $taskStage = Join-Path $OutputDirectory "stage-v$taskVersion"
    New-Item -ItemType Directory -Force -Path $taskStage | Out-Null
    Copy-Item -LiteralPath $ExePath -Destination (Join-Path $taskStage '邻传.exe') -Force
    Copy-Item -LiteralPath (Join-Path $taskRoot 'docs\USAGE.md') -Destination (Join-Path $taskStage '使用说明.md') -Force
    @"
# 邻传 $taskVersion

解压到可写目录，双击 邻传.exe。两台电脑都运行 App，即可在局域网中聊天和互传文件。
详见 使用说明.md；更新入口：设置 → 软件更新 · GitHub。

源码及发布：https://github.com/powersqb/lindrop
聊天和文件传输仅局域网直连；手动检查/下载更新访问 GitHub。
"@ | Set-Content -LiteralPath (Join-Path $taskStage 'README.md') -Encoding utf8
    $taskZip = Join-Path $OutputDirectory 'lindrop-windows-x64.zip'
    Compress-Archive -LiteralPath (Join-Path $taskStage '邻传.exe'), (Join-Path $taskStage 'README.md'), (Join-Path $taskStage '使用说明.md') -DestinationPath $taskZip -Force
    $taskZipHash = (Get-FileHash -LiteralPath $taskZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $taskExeHash = (Get-FileHash -LiteralPath $ExePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$taskZipHash  $(Split-Path $taskZip -Leaf)`n$taskExeHash  邻传.exe" | Set-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt') -Encoding utf8
    $taskNotes = "邻传 $taskVersion：局域网在线直聊、原生多文件拖拽和加密文件传输。首次核对记住设备，以后无需开始/结束聊天；文件仅接收方确认，可拒绝或中途取消。`n`n设置 → 软件更新 · GitHub：手动检查并下载 SHA-256 校验后的发布包，点击安装并重启自动替换 EXE，保留数据和设备信任。0.5.0 及更早版本需首次手动升级；接收文件保留原名，同名由接收方选择覆盖、另存或取消。`n`n固定名 lindrop-windows-x64.zip，更新始终使用原 EXE 路径；新饱满图标和启动授权窗口关闭后恢复界面。Windows x64 便携版，免安装。第二台物理电脑仍待验证。"
    $taskNotes | Set-Content -LiteralPath (Join-Path $OutputDirectory 'release-notes.md') -Encoding utf8
    Write-Output $taskZip
} finally { Pop-Location }
