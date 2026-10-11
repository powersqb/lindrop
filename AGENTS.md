# 邻传维护约定

- 这是独立 Git 仓库；不提交 EXE、运行数据、私钥、聊天记录或虚拟环境。
- 网络工作放后台线程，Tk 控件只由主线程操作；通过 PeerNode.events 传递事件。
- 发现结果不代表可信身份。首次连接必须由双方核对验证码；用户确认后持久记住设备 ID 与证书 pin，后续自动连接。证书变化必须阻止连接，不能静默重新信任。
- 聊天和传输只允许局域网 IPv4，不增加公网中继、遥测或自动上传。
- 用户手动触发的软件更新可访问 powersqb/lindrop 的 GitHub HTTPS 发布接口；必须限制重定向、大小和时限，并校验 SHA-256。不后台联网或自动执行下载文件。
- 控制消息、文件、缓存、请求速率均须有上限；接收路径不能由远端指定。
- 新功能独立模块，协议提供能力标识；旧版不支持时提示升级。
- 更新版本时同步 __init__.py、pyproject.toml、README、CHANGELOG 和验证记录。
- 检查：uv run ruff check .；uv run pytest --basetemp=outputs/test-temp。
- 发布前打包并运行 EXE --self-test；第二台实体电脑未验证时必须注明。
