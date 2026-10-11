# 邻传维护约定

- 这是独立 Git 仓库；不提交 EXE、运行数据、私钥、聊天记录或虚拟环境。
- 网络工作放后台线程，Tk 控件只由主线程操作；通过 PeerNode.events 传递事件。
- 发现结果不代表可信身份。新增功能必须沿用证书 pin 和双方人工核对的授权流程。
- 只允许局域网 IPv4，不增加公网中继、遥测或自动上传。
- 控制消息、文件、缓存、请求速率均须有上限；接收路径不能由远端指定。
- 新功能独立模块，协议提供能力标识；旧版不支持时提示升级。
- 更新版本时同步 __init__.py、pyproject.toml、README、CHANGELOG 和验证记录。
- 检查：uv run ruff check .；uv run pytest --basetemp=outputs/test-temp。
- 发布前打包并运行 EXE --self-test；第二台实体电脑未验证时必须注明。
