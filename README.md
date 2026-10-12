# 小三月的 Agent · March's Agent

[English](README.en.md) · [部署配置](#部署) · [来源与许可](NOTICE.md)

一个为原生 Antigravity CLI（`agy`）制作的二次元网页聊天界面。把终端里的模型回复、文件操作和工具状态放进浏览器，搭配三月七的圆形头像与动态状态、流萤和遐蝶的双人主题，适合在电脑或安卓手机上使用。

**March's Agent is an anime-themed web interface for the native Antigravity CLI. It brings streaming conversations, agent activity, server files, and returned model information to desktop and mobile browsers, with March 7th avatars and Firefly / Castorice themes.**

项目采用 Python + FastAPI 和原生 HTML/CSS/JavaScript，无需前端构建。模型调用仍由你自己的 `agy` 登录账号完成，使用对应账号的额度。

## 功能

- **电脑与手机界面**：桌面侧栏、手机抽屉；针对约 19.8:9 的竖屏布局保留两个人物的构图。
- **两种背景切换**：原版背景与双人场景；独立外观设置可调整配色、遮罩、模糊和角色装饰。
- **三月七陪伴状态**：圆形消息头像；待机、思考、搜索、工作、等待、暂停、完成、出错的表情与动效。
- **聊天管理**：新建、恢复、改标题、置顶和删除；消息保存到服务器，浏览器刷新后可恢复，不按聊天时间自动删除。
- **模型与账号**：读取 `agy models` 的实际可用列表；账号管理与额度展示。
- **流式回复与思考区**：正文、CLI 返回的思考和工具事件分别展示；支持停止生成。只能显示上游实际提供的内容。
- **当前请求的错误**：CLI 的会话汇总可能带回旧错误。仅在原生步骤能证明错误早于本轮、且本轮已完成时将其标记为历史提示，避免重复追加到新回复；本轮失败和无法验证的错误仍会显示。
- **真实返回模型名**：区分“所选模型”和本次原始返回字段；支持 `fn`、`exp-a`、`exp-b` 或其他返回值，不把型号写成固定对应表。
- **文件交互**：上传图片/文件、选择服务器工作目录、下载允许目录里的生成文件到电脑或手机。
- **接口**：网页聊天的 SSE 流，以及 `/v1/chat/completions`、`/v1/models` 兼容端点；兼容端点沿用网站的会话认证。

## 部署

以下步骤面向 Linux VPS，要求 Python **3.10 或以上**、Git、可运行并已登录的 `agy`。已验证的组合为 `agy 1.3.3` 与桌面配置 `2.22.0`。

### 1. 安装并登录原生 CLI

安装方式以 [Antigravity 官方文档](https://www.antigravity.google/docs/cli/install/) 为准。Linux 官方安装命令：

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
agy
```

按 CLI 的首次启动提示完成登录。网站服务与 `agy` 必须使用同一个 Linux 用户。仓库不附带任何可直接使用的 Google 账号或登录凭据。

### 2. 安装网站

Ubuntu / Debian 如缺少基础工具，可先安装 `git`、`python3`、`python3-venv` 和 `curl`。

```bash
git clone https://github.com/BloodYi/march-agent.git "$HOME/march-agent"
cd "$HOME/march-agent"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
nano .env
```

在 `.env` 填写你自己的 `BASIC_AUTH_USER` 和 `BASIC_AUTH_PASS`，这是**网站登录**，与 Google 账号登录分开。需要访问已有项目时，将 `AGY_ALLOWED_ROOTS` 改成相应目录，例如 `/home/your-user/projects:/srv/projects`。

```bash
bash start.sh
```

服务默认监听 `127.0.0.1:26903`。健康检查：

```bash
curl http://127.0.0.1:26903/healthz
```

### 3. 使用域名与 Cloudflare Tunnel

将你已有隧道的域名路由指向 `http://127.0.0.1:26903`，浏览器通过 HTTPS 域名访问。网站登录使用 Secure Cookie，需要 HTTPS。

[deploy/cloudflared.yml.example](deploy/cloudflared.yml.example) 给出了路由格式；填写自己的隧道编号、凭据文件路径和域名，并合并到已有配置。不要把本机的隧道凭据提交到仓库。

### 4. 作为后台服务运行

示例默认项目放在 `~/march-agent`。如安装位置不同，先修改服务文件中的两条路径。

```bash
mkdir -p ~/.config/systemd/user
cp deploy/march-agent.service.example ~/.config/systemd/user/march-agent.service
systemctl --user daemon-reload
systemctl --user enable --now march-agent
systemctl --user status march-agent
```

需要退出 SSH 后仍运行时，可由有权限的管理员执行 `sudo loginctl enable-linger "$USER"`。更新后重新安装依赖并重启 `march-agent` 服务；保留原来的 `.env`、账号目录和聊天数据。

## 配置

| 配置项 | 含义 |
| --- | --- |
| `HOST` / `PORT` | 默认 `127.0.0.1` / `26903` |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` | 自己设置的网站账号与密码，启动入口要求填写 |
| `AGY_BIN` | CLI 可执行文件名称或绝对路径 |
| `AGY_UPSTREAM_PROFILE` | `desktop`：桌面兼容配置；`native`：CLI 原始配置 |
| `AGY_DESKTOP_VERSION` | 桌面兼容配置版本，当前验证值 `2.22.0` |
| `AGY_ALLOWED_ROOTS` | 网页目录选择与下载可访问的范围；Linux 多个目录用冒号分隔 |
| `AGY_TIMEOUT` | 每轮最长时间，默认 1800 秒；超时停止本轮，不删除已有聊天 |
| `AGY_SANDBOX` | `1` 启用 CLI 自身的沙箱；实际运行权限仍由 Linux 用户与 CLI 决定 |
| `AGY_DISABLE_MCP` / `AGY_DISABLED_MCP_SERVERS` | 可停用全部或指定的 MCP 工具服务 |

完整模板见 [.env.example](.env.example)。上传内容默认保存在 `uploads/`，消息在 `data/messages/`，聊天索引在 `.antigravity_conversations.json`。这些目录和文件均被 Git 忽略。

## Medium 与实际路由模型

曾复现：同一账号、同一 `gemini-3.8-flash-medium`，原生 CLI 配置返回 `gemini-3.8-flash-n`，桌面客户端配置返回 `gemini-3.8-flash-exp-b`。CLI 请求本身确实选择 Medium，思考预算为 4000；差别来自上游按客户端身份下发的配置。

`desktop` 模式通过每轮临时的本机转发器，对齐已验证的桌面 HTTP User-Agent。原生 CLI 仍执行请求、工具和会话保存；转发器保留请求内容和响应字节，只更换这个客户端标识，且只连接指定的 Google 官方 HTTPS 接口。它不会把 Medium 改为 High，也不会改写返回模型名。

上游实验分组会变化，**Medium 并不保证永远返回 exp-b**。界面按本次实际证据展示；多个返回值保留多个，没有字段时显示“未返回”，历史记录与新检测分开。点击“检测当前模型”会新发一次短请求，因此会使用账号的正常额度。客户端版本升级后应重新验证；可切回 `AGY_UPSTREAM_PROFILE=native`。

## 检查

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/test.py
```

测试在临时项目和临时用户目录中运行，不读取你的账号凭据，也不调用在线模型。GitHub Actions 在 Python 3.10 / 3.12 上运行同一套检查。测试覆盖原始模型字段、请求隔离、Medium 参数、思考分离、转发错误、停止清理、认证与文件接口。

## 来源

基于 [ericmaster/antigravity-chat](https://github.com/ericmaster/antigravity-chat)，保留 [MIT 许可证](LICENSE)。这是非官方项目；角色与美术的权利说明见 [NOTICE.md](NOTICE.md)。
