$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskBuild = Join-Path $taskRoot '.cache\lan-build'
if (-not (Test-Path -LiteralPath (Join-Path $taskBuild 'PyInstaller'))) {
    throw 'Build tools missing. Run tools\uv\uv.exe pip install --python .venv\Scripts\python.exe --target .cache\lan-build --index-url https://pypi.org/simple pyinstaller==6.22.3'
}
$taskPreviousPythonPath = $env:PYTHONPATH
$taskPreviousConfig = $env:PYINSTALLER_CONFIG_DIR
$env:PYTHONPATH = $taskBuild
$env:PYINSTALLER_CONFIG_DIR = Join-Path $taskRoot '.cache\pyinstaller'
$taskOpenSSL = 'C:\Program Files\Git\mingw64\bin'
$taskArguments = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', '邻传', '--distpath', 'outputs\邻传', '--workpath', 'build\lan-drop',
    '--specpath', 'build', '--paths', $taskRoot,
    '--add-data', "$taskRoot\lan_drop\web;lan_drop\web",
    '--add-binary', "$taskOpenSSL\openssl.exe;openssl",
    '--add-binary', "$taskOpenSSL\libcrypto-3-x64.dll;openssl",
    '--add-binary', "$taskOpenSSL\libssl-3-x64.dll;openssl",
    '--add-binary', "$taskOpenSSL\zlib1.dll;openssl",
    'lan_drop\desktop_entry.py'
)
try {
    & $taskPython @taskArguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
} finally {
    $env:PYTHONPATH = $taskPreviousPythonPath
    $env:PYINSTALLER_CONFIG_DIR = $taskPreviousConfig
}
