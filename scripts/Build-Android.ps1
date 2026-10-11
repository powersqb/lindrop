param([string]$JdkHome, [string]$SdkRoot, [switch]$DebugOnly)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path $PSScriptRoot -Parent
$workspaceRoot = Split-Path (Split-Path $appRoot -Parent) -Parent
if (!$JdkHome) { $JdkHome = (Get-ChildItem (Join-Path $workspaceRoot 'tools/android-jdk') -Directory | Select-Object -First 1).FullName }
if (!$SdkRoot) { $SdkRoot = Join-Path $workspaceRoot 'tools/android-sdk' }
if (!(Test-Path "$JdkHome/bin/java.exe") -or !(Test-Path "$SdkRoot/platforms/android-35/android.jar")) { throw '需要 JDK 17 和 Android SDK 35；也可传入 -JdkHome 与 -SdkRoot。' }
$env:JAVA_HOME = $JdkHome
$env:ANDROID_HOME = $SdkRoot
$env:PATH = "$JdkHome/bin;$env:PATH"
$projectRoot = Join-Path $appRoot 'android'
# Gradle's Windows test-worker argument file mishandles some Chinese directory names.
# Build a small source-only copy under ASCII TEMP; source and signing material stay separate.
if ($projectRoot -match '[^\x00-\x7F]') {
    $scratch = Join-Path ([IO.Path]::GetTempPath()) ('lindrop-android-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $scratch | Out-Null
    Get-ChildItem $projectRoot -File | Copy-Item -Destination $scratch
    Copy-Item -LiteralPath (Join-Path $projectRoot 'gradle') -Destination $scratch -Recurse
    New-Item -ItemType Directory -Path (Join-Path $scratch 'app') | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'app/build.gradle') -Destination (Join-Path $scratch 'app')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'app/src') -Destination (Join-Path $scratch 'app') -Recurse
    $projectRoot = $scratch
}
$signing = Join-Path $appRoot 'outputs/android-signing'
if (!$DebugOnly) {
    New-Item -ItemType Directory -Force $signing | Out-Null
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $signing /inheritance:r /grant:r "${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '不能保护 APK 签名目录权限' }
    $passwordFile = Join-Path $signing 'password.txt'
    $keyFile = Join-Path $signing 'lindrop.jks'
    if ((Test-Path $keyFile) -and !(Test-Path $passwordFile)) { throw '签名密码缺失，请恢复备份；不能更换密钥覆盖现有安装。' }
    if (!(Test-Path $passwordFile)) { $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Fill($bytes); [IO.File]::WriteAllText($passwordFile,[Convert]::ToBase64String($bytes)) }
    $env:LINDROP_ANDROID_KEY_PASSWORD = [IO.File]::ReadAllText($passwordFile).Trim()
    $env:LINDROP_ANDROID_KEYSTORE = $keyFile
    if (!(Test-Path $keyFile)) {
        & "$JdkHome/bin/keytool.exe" -genkeypair -keystore $keyFile -alias lindrop -keyalg RSA -keysize 3072 -validity 10000 -dname 'CN=LinDrop Personal Android' -storepass:env LINDROP_ANDROID_KEY_PASSWORD -keypass:env LINDROP_ANDROID_KEY_PASSWORD
        if ($LASTEXITCODE -ne 0) { throw 'APK 签名密钥创建失败' }
    }
}
try {
    $tasks = @('assembleDebug','assembleDebugAndroidTest','testDebugUnitTest','lintDebug')
    if (!$DebugOnly) { $tasks += 'assembleRelease' }
    & (Join-Path $projectRoot 'gradlew.bat') -p $projectRoot --no-daemon @tasks --console plain
    if ($LASTEXITCODE -ne 0) { throw 'Android build/test failed' }
    $output = Join-Path $appRoot 'outputs/android'
    New-Item -ItemType Directory -Force $output | Out-Null
    if (!$DebugOnly) { Copy-Item (Join-Path $projectRoot 'app/build/outputs/apk/release/app-release.apk') (Join-Path $output 'lindrop-android.apk') -Force }
    Copy-Item (Join-Path $projectRoot 'app/build/outputs/apk/debug/app-debug.apk') (Join-Path $output 'lindrop-android-debug.apk') -Force
    Copy-Item (Join-Path $projectRoot 'app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk') (Join-Path $output 'lindrop-android-test.apk') -Force
    [IO.File]::WriteAllText((Join-Path $output 'build-directory.txt'),$projectRoot)
    Write-Output "APK: $output"
    Write-Output "Build/reports: $projectRoot"
} finally { Remove-Item Env:LINDROP_ANDROID_KEY_PASSWORD -ErrorAction SilentlyContinue; Remove-Item Env:LINDROP_ANDROID_KEYSTORE -ErrorAction SilentlyContinue }
