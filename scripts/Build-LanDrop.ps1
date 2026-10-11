# Build a portable Windows EXE from locked project dependencies.
param([string]$OutputDirectory = '', [string]$OpenSSLDirectory = 'C:\Program Files\Git\mingw64\bin')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run uv sync --locked first.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $taskRoot 'outputs\邻传' }
foreach ($taskFile in @('openssl.exe', 'libcrypto-3-x64.dll', 'libssl-3-x64.dll', 'zlib1.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $OpenSSLDirectory $taskFile))) { throw "Missing OpenSSL runtime: $taskFile. Supply -OpenSSLDirectory." }
}
$taskPreviousConfig = $env:PYINSTALLER_CONFIG_DIR
$env:PYINSTALLER_CONFIG_DIR = Join-Path $taskRoot '.cache\pyinstaller'
$taskArguments = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', '邻传', '--distpath', $OutputDirectory,
    '--icon', (Join-Path $taskRoot 'lan_drop\assets\lindrop.ico'),
    '--workpath', (Join-Path $taskRoot 'build\lan-drop'),
    '--specpath', (Join-Path $taskRoot 'build'), '--paths', $taskRoot,
    '--collect-all', 'tkinterdnd2',
    '--add-data', "$taskRoot\lan_drop\web;lan_drop\web",
    '--add-data', "$taskRoot\lan_drop\assets;lan_drop\assets",
    '--add-binary', "$OpenSSLDirectory\openssl.exe;openssl",
    '--add-binary', "$OpenSSLDirectory\libcrypto-3-x64.dll;openssl",
    '--add-binary', "$OpenSSLDirectory\libssl-3-x64.dll;openssl",
    '--add-binary', "$OpenSSLDirectory\zlib1.dll;openssl",
    (Join-Path $taskRoot 'lan_drop\desktop_entry.py')
)
try {
    Push-Location -LiteralPath $taskRoot
    & $taskPython @taskArguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
} finally {
    Pop-Location
    $env:PYINSTALLER_CONFIG_DIR = $taskPreviousConfig
}
