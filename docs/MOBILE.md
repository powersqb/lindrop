# 邻传手机版：目标与开发准备

核对日期：2026-10-11。目标设备由用户提供：华为 Mate 80，HarmonyOS 7.0.0.109 SP6。

**当前状态：Android 0.1.0 测试版开发与验证中。** 用户已选择安卓版，不再等待鸿蒙开发工具。Windows 0.6.0 保持现状。Mate 80 上的实际互通、后台在线能力和安装流程均未验证，不应标为已支持。

## 平台选择

按用户选择实现 Android APK，Java 原生控件，无第三方运行时依赖。Android APK 在 HarmonyOS 5 及以上可经卓易通等兼容环境安装，华为说明支持情况以实际结果为准；尚未验证该环境内邻传所需的 UDP 组播、TLS 监听及反向连接，不能仅凭 APK 能安装就承诺互通。组播受限时提供手动输入电脑 IPv4:端口入口，反向 TLS 连接仍然必须可达。

- [华为 APK 安装说明](https://consumer.huawei.com/cn/support/content/zh-cn16061787/)
- [Android 官方 SDK 下载](https://developer.android.com/studio#command-tools)
- [官方命令行编译说明](https://developer.android.com/build/building-cmdline)

工具链在工程 `tools/`：Temurin JDK 17.0.20.1、Gradle 8.11.1、Android SDK API 35 / Build Tools 35.0.0，命令行工具包 15859902；下载均已核对 SHA-256。AGP 固定 8.9.2，最低 Android 8（API 26），目标 API 35；固定兼容工具版本，不宣称全部为最新。APK 使用本地持久签名密钥，签名材料在 `outputs/android-signing/`，不进入公开 Git；日后覆盖升级必须保留该密钥并递增 versionCode。详见 [Android 开发说明](../android/README.md)。

## 第一版交互目标

- 同一局域网自动显示运行邻传的设备，点选后进入聊天。
- 首次连接双方核对验证码，确认后记住设备；以后在线直接发文字。
- 文件选择后直接发起接收请求，接收方接受、拒绝或中途取消。
- 保留文件内容与原文件名；同名由用户决定，不悄悄覆盖或自动改名。
- 聊天记录下方紧接输入框，文件入口集成在输入区；网络传输使用后台任务。
- 首版以 App 前台在线为验证目标；锁屏、切后台后的保活效果单独测试，不预先承诺。

## 与 Windows 0.6.0 的协议兼容要求

以 `lan_drop/discovery.py`、`peer.py`、`chat.py` 的现有行为为准。手机不能只实现 HTTPS 客户端：电脑收到请求时会反向连接发起设备的 `/peer/hello`，手机必须具备 TLS 服务端能力。

| 项目 | 现有约定 |
| --- | --- |
| 网络 | 私有 IPv4；禁止公网中继、遥测、自动上传 |
| 发现 | UDP 239.255.42.99:45876，TTL 1；约 2 秒广播一次，8 秒未见则过期 |
| 发现字段 | `app=lindrop`、`protocol=2`、`id`、`name`、`port`、`fingerprint`；广播源地址作为连接地址 |
| 设备身份 | 32 位小写十六进制 ID，TLS 证书 DER 的 SHA-256（64 位小写十六进制） |
| 连接 | TLS 至少 1.2；发控制消息前核对对端证书指纹；持久记住的指纹变化必须阻止 |
| 首次验证码 | SHA-256(sender_fp + receiver_fp + nonce + request_id)，ASCII 拼接；前 16 位十六进制大写，分为四组 |
| 控制消息 | JSON 请求最多 32 KiB，客户端响应最多 64 KiB；Origin 与接收端 HTTPS 地址一致 |
| 文件 | 每批最多 50 个、单个最多 2 GiB、总量最多 20 GiB；元数据包含 name、size、sha256 |
| 聊天 | 单条最多 4 KiB UTF-8；内存缓存最多 200 条，消息 ID 用于去重，不自动保存聊天记录 |

文件发送：`POST /peer/offer` → 接收方确认 → 使用 `X-Offer-Secret` 查询 `GET /peer/offers/<id>` → 使用返回 token 作为 `X-Transfer-Token` 上传 `POST /peer/upload/<id>/<index>`。上传体是原始文件字节，Content-Length 必须等于声明大小。取消入口 `POST /peer/cancel/<id>`。手机接收时先写私有临时文件，校验长度和 SHA-256 后才提交到用户选择的本地位置；远端不得指定保存目录。

聊天：`POST /peer/chat/offer` → 查询 `GET /peer/chat/<id>/<cursor>` 等待 accepted → `POST /peer/chat/<id>/activate` → `POST /peer/chat/<id>/send` 与游标轮询。查询和后续操作使用 `X-Offer-Secret`。只有双方已信任且请求 `automatic=true` 时才自动接受；未知设备仍需人工验证码确认。连接续期属于内部操作，不增加日常“开始/结束”按钮。

## 编译与验收关卡

1. 固定 JDK、Gradle、SDK，完成 APK 编译、8 项协议边界测试及 Android Lint。
2. 用同一 Java 通信核心对接电脑 Python 核心，验证验证码、双向聊天、原名文件、损坏上传保护和取消；JVM 测试不等于手机运行。
3. 在 Android 35 模拟器验证启动界面、Android Keystore 的持久身份、TLS 服务端/客户端和错误证书 pin。
4. 用户在 Mate 80 安装测试版，验证手机 ↔ Windows 0.6.0 的发现、聊天、双向原名文件；卓易通兼容环境的实际网络限制单独记录。
5. 手机更新机制另行设计，Windows 的 EXE 替换方案不能直接套用到 APK；首版提供手动下载安装、同一签名覆盖升级。
