# 自己修改和升级邻传

本目录 `apps/lindrop` 是独立 Git 仓库。打开此目录作为 VS Code 工作区；源码入口是 `lan_drop/__main__.py`。不需要运行工程分析项目。

## 固定工具链

| 工具 | 本次使用 | 说明 |
| --- | --- | --- |
| Windows | Windows 11 x64 | 当前 EXE 面向 Windows x64 |
| Python | 3.12.15 | `.python-version` 固定；需要带 Tcl/Tk 的发行版 |
| uv | 0.13.0 | 创建隔离环境和生成 `uv.lock`；最新版本见工程主文档 |
| Tk / ttk | Python 自带 | 原生窗口，不依赖浏览器 |
| tkinterdnd2 | 0.6.3 | 原生系统拖拽；已核对官方 [PyPI](https://pypi.org/project/tkinterdnd2/)，2026-10-11 |
| PyInstaller | 6.22.3 | 单文件打包；已核对官方 [PyPI](https://pypi.org/project/pyinstaller/)，2026-10-11 |
| pytest / Ruff | 9.1.1 / 0.17.0 | 与现有验证环境一致，固定在项目；最新状态待核实 |
| OpenSSL CLI | 3.5.7（Git for Windows 自带） | 仅用于本地生成临时证书；最新版本待核实 |

`pyproject.toml` 声明直接依赖；`uv.lock` 固定全部解析版本和包校验信息。不要复制别的电脑的 `.venv`。

## 在另一台开发电脑恢复

先安装 Git、uv 和 Python 3.12.15（带 Tk），复制整个仓库或从自己建立的远程仓库 clone。开发时安装依赖需要联网；应用运行和文件传输只使用局域网。

```powershell
cd <你的邻传源码目录>
uv sync --locked
uv run --locked python -m lan_drop
```

本机 uv 未加入 PATH，可在工程根目录使用：

```powershell
& .\tools\uv\uv.exe sync --project apps\lindrop --locked
& .\apps\lindrop\.venv\Scripts\python.exe -m lan_drop
```

也可进入 `apps/lindrop` 后执行 `.venv\Scripts\python.exe -m lan_drop`。VS Code 解释器选择本仓库 `.venv\Scripts\python.exe`。

## 从哪改起

| 文件 | 负责什么 | 适合修改什么 |
| --- | --- | --- |
| `lan_drop/layout.py` | 所有主要控件、尺寸、配色、原生拖拽注册 | 窗口布局、颜色、按钮文字 |
| `lan_drop/desktop.py` | 主线程事件处理、文件传输交互、设置 | 文件选择、进度、传输结果 |
| `lan_drop/chat_ui.py` | 会话核对、输入框、消息显示和轮询 | 聊天体验、快捷键、提醒 |
| `lan_drop/chat.py` | 有授权的双向聊天接口 | 新消息类型、会话生命周期 |
| `lan_drop/discovery.py` | 组播发现与在线状态 | 设备能力与发现逻辑 |
| `lan_drop/peer.py` | 证书 pin、验证码、文件批次和流式收发 | 文件协议、完整性检查 |
| `lan_drop/server.py` | HTTPS 服务、私有地址限制、文件名安全 | 传输底层；修改前读现有测试 |
| `lan_drop/selftest.py` | 包含在 EXE 中的诊断 | 发布后的基础运行验证 |
| `tests/` | 真实本机 HTTPS、Tk 交互回归 | 修改协议或交互后验证 |
| `scripts/Build-LanDrop.ps1` | 打包入口和 DLL 收集 | 发布路径、OpenSSL 路径 |

Tk 控件只能从主线程更新。后台任务用 `node.emit(...)` 传递事件，主线程 `Desktop.tick()` 消费。拖拽文件路径是 **Tcl list**，用 `tk.splitlist` 解析，不能按空格拆分，否则中文和带空格路径会出错。

## 新增局域网功能

1. 独立建模块，例如 `clipboard.py`，不要把所有功能写进布局文件。
2. 在 `/peer/hello` 提供能力标识，客户端先判断支持情况。现有文件协议仍为 2，新增聊天能力为 `chat: true`，旧版文件互传继续可用。
3. 复用 HTTPS 和精确证书 pin；发现广播仅提供待验证身份，不可据此自动接收内容。验证码绑定双方证书、随机 nonce 和请求 ID。
4. 设计明确的人工授权、请求大小、速率、超时和内存上限；不要把本机任意文件、剪贴板或目录默认共享。
5. 后台 I/O + 事件队列；UI 按设备 ID 组织消息，不用设备名称作为身份。
6. 写覆盖授权边界和实际双向通信的测试，再更新版本、README 和 CHANGELOG。

## 验证与打包

在仓库目录执行：

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
New-Item -ItemType Directory -Force outputs | Out-Null
uv run --locked pytest -q --basetemp=outputs/test-temp
& .\scripts\Build-LanDrop.ps1
& .\outputs\邻传\邻传.exe --self-test --data-dir outputs/self-test
```

每次自检使用新的空输出目录（重复验证时换一个 `--data-dir`）。最后一个程序为窗口版，退出后检查 `outputs/self-test/self-test.json` 中的 `ok`。实际 Windows 原生拖拽可运行 `scripts/Verify-NativeDrop.py`，把底部合成文件按钮拖入聊天区，检查 `outputs/native-drop-test/result.json`，此过程不会自动接收文件。

测试自检使用合成数据模拟双方接受，正常 App 始终要求用户核对。打包默认从 Git 的 `C:\Program Files\Git\mingw64\bin` 收集 OpenSSL 和 3 个 DLL；安装位置不同则传 `-OpenSSLDirectory '你的目录'`。生成的 EXE 运行时不需要 Git 或 Python。

单文件启动会解压到系统临时目录，首次启动可能稍慢。发布时保留 ZIP 和源码对应的 Git 标签；不要把运行数据或 EXE 加入 Git。

## Git 工作流

已保存 `v0.2.0` 基线和后续版本。Git 使用本机已有作者配置。公开源码仓库为 `powersqb/lindrop`，GitHub 操作状态见验证记录。

```powershell
git status
git switch -c codex/my-feature
# 修改后执行上述检查
git add lan_drop tests docs README.md CHANGELOG.md pyproject.toml uv.lock
git commit -m "feat: 描述新增功能"
git log --oneline --decorate
```

日常保留提交即可恢复历史。查看旧代码：`git show v0.2.0:lan_drop/desktop.py`；旧版没有原生拖拽依赖，单独运行旧版应恢复对应工具链。需要异地备份可把整个仓库（包含 `.git`）备份到 NAS，或备份到自己的其他 Git 远程。不要用未备份的 `reset --hard` 清理修改。

## GitHub 发布和后续升级

源码仓库：https://github.com/powersqb/lindrop ，正式版页面：https://github.com/powersqb/lindrop/releases 。

`updates.py` 是独立的更新模块，只在用户点击时发出公网 HTTPS 请求。`update_ui.py` 负责交互，后台任务依旧通过主线程队列返回。此模块不会把局域网地址、设备名、聊天或文件发送到 GitHub。更新 ZIP 保存到数据目录的 `updates/`；不自动运行或安装。

当前发布包名为 `lindrop-vX.Y.Z-windows-x64.zip`。GitHub Releases 必须是正式发布（非 draft / prerelease），附件上传完成且 API 提供 `sha256:` 摘要；源仓库、标签和下载地址必须匹配，客户端才允许下载。HTTPS 与官方摘要提供附件一致性保证；GitHub 账户若被攻破不在此校验可解决的范围内。维护账户应自行保管授权。

后续版本先更新 `__init__.py`、`pyproject.toml`、README、USAGE 和 CHANGELOG，再运行 `uv lock` 和测试。使用 Git 标签发布：

```powershell
git add .
git commit -m "feat: 本次升级内容"
git push origin main
git tag -a v0.5.0 -m "邻传 0.5.0"
git push origin v0.5.0
```

标签必须与源版本一致。`.github/workflows/windows.yml` 在 main/PR 上验证，在版本标签上验证后发布：固定的官方 Action 提交号 → Python 3.12.15 / uv 0.13.0 → `uv sync --locked` → Ruff / pytest → EXE → 回环自检 → ZIP 与 SHA256SUMS → Releases。依赖的 Action 发布页和核对日期见 `actions-versions.json`。普通构建仅有 contents:read，只有标签发布 job 才有 contents:write；不需要在源码里存 PAT。Github Actions runner 的真实网卡/组播和第二台实体电脑不由回环自检覆盖。

本机也可运行 `scripts/New-ReleasePackage.ps1` 打包，然后用 GitHub CLI `gh release create` 上传明确的 ZIP 与 SHA256SUMS，不把运行数据加入附件。首次建立远程与发布需要本人 GitHub 授权；连接 Codex 的 GitHub 工具与本机 CLI 登录不是同一份授权。
