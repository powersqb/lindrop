# 邻传手机版：目标与开发准备

核对日期：2026-10-11。目标设备由用户提供：华为 Mate 80，HarmonyOS 7.0.0.109 SP6。

**当前状态：开发准备，尚无可安装手机版。** Windows 0.6.0 保持现状。手机与电脑的实际互通、后台在线能力和安装流程均未验证，不应标为已支持。

## 平台选择

优先实现鸿蒙原生应用，采用 ArkTS 界面。Android APK 在 HarmonyOS 5 及以上可经卓易通等兼容环境安装，华为说明支持情况以实际结果为准；尚未验证该环境内邻传所需的 UDP 组播、TLS 监听及反向连接，不能仅凭 APK 能安装就承诺互通。

- [华为 APK 安装说明](https://consumer.huawei.com/cn/support/content/zh-cn16061787/)
- [DevEco Studio 官方下载](https://developer.huawei.com/consumer/cn/deveco-studio/resources/)
- [工具安装说明](https://developer.huawei.com/consumer/en/doc/harmonyos-guides/ide-software-install)
- [真机自动签名说明](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/ide-signing-auto)

这台电脑尚未安装 DevEco Studio / HarmonyOS SDK。官方工具下载需要用户登录华为账号；下载后记录实际工具版本、SDK/API 和编译结果，不把官网版本信息当成已安装版本。真机 HAP 需要签名；账号登录、身份认证（如要求）、设备调试授权由用户本人完成。签名证书、私钥、Profile、设备标识不进入公开仓库。

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

1. 安装并记录官方 DevEco Studio / SDK；先用官方模板完成编译及模拟器预览。
2. 验证目标 SDK 的 UDP 组播、TLS 服务端和证书指纹接口。若需要原生网络模块，使用可审查依赖并记录许可证；不以关闭 TLS 或身份验证绕过问题。
3. 实现协议和精简界面，使用本机电脑测试正确/错误验证码、指纹变化、取消、同名文件和 Unicode 文件名。
4. 用户连接 Mate 80，完成签名与调试授权；安装真机验证手机 ↔ Windows 0.6.0 的发现、聊天、双向文件字节一致性及原文件名。
5. 真机通过后才记录支持状态和发布手机版；手机更新机制另行设计，Windows 的 EXE 替换方案不能直接套用到 HAP。
