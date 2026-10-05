# WeChatFerryTool（Windows 裸金属）

## 目标

在 Windows 台式机上搭建一套**微信消息实时监听 + 关键词/LLM 过滤 + 重要消息推送**的工具，用于解决"群消息过载"。

```
[Windows 微信 PC 客户端]
        ↓ dll 注入（version.dll 代理）
[aixed/WeChat-Hook DLL]              ← 默认监听 30001 端口，HTTP
        ↓
        ↓ POST /hook/callback（消息推送）
        ↓
[Python consumer（HTTP server）]   ← 监听 8888，接收回调
        ├→ 过滤（filter.py）
        ├→ SQLite 落库（store.py）
        ├→ DeepSeek/qwen 评分（scorer.py）
        └→ 评分 ≥4 推送 Bark（notifier.py）
```

## 配套版本

| 组件 | 版本 |
|------|------|
| 微信 PC 客户端 | **4.1.10.27**（aixed DLL 锁定的版本） |
| aixed/WeChat-Hook | v4.1.10.27（从 https://github.com/aixed/WeChat-Hook/releases 下载 `version.dll`） |
| Python | 3.11+ 64-bit |
| 操作系统 | Windows 10 1909+ / Windows 11 |

⚠️ 微信版本必须锁在 4.1.10.27。新版微信会让 DLL 的 hook 偏移失效，导致收不到消息。

## 与现有项目的关系

- 本方案作为 `WeChatFerryTool` 子项目，路径 `D:\projects\WeChatFerryTool`
- 配套微信需要降级到 4.1.10.27（不能用 4.1.13+ 等更新版本）
- **不要把 `version.dll` 提交到仓库**（虽然开源，但作者未明确许可证要求）

## 目录结构

```
WeChatFerryTool/
├── README.md                  ← 本文档
├── start.sh                   ← Git Bash 一键启动（微信 + consumer）
├── stop.sh                    ← 停掉所有进程（按 PID 精确停止）
├── requirements.txt           ← Python 依赖
├── config.example.yaml        ← 配置模板
├── .gitignore
├── consumer/
│   ├── __init__.py
│   ├── main.py                ← HTTP server 接收回调，串联各模块
│   ├── hook_client.py         ← 对 127.0.0.1:30001 DLL HTTP API 的封装
│   ├── filter.py              ← 群/发送人/关键词 白名单过滤
│   ├── scorer.py              ← DeepSeek/qwen LLM 评分 1~5
│   ├── notifier.py            ← Bark iOS 推送
│   └── store.py               ← SQLite 落库
├── scripts/
│   ├── test_hook.py           ← 测试 DLL 的探针（可单独跑）
│   └── inject_dll.ps1         ← 备用：远程线程注入 DLL（仅 4.1.13.65 验证用）
├── logs/                      ← 运行时日志（gitignore）
└── data/                      ← SQLite 文件（gitignore）
```

## 模块职责

| 文件 | 职责 | 改它的时机 |
|------|------|-----------|
| `consumer/main.py` | HTTP server 接收 DLL 回调；串联各模块；信号处理；启动期注册回调 | 加批处理、加新事件类型、改消息循环 |
| `consumer/hook_client.py` | 对 DLL HTTP API 的封装（status/set_callback/GetSelfProfile） | 加新 DLL endpoint |
| `consumer/filter.py` | 纯函数：判定是否入库 | 调整匹配规则 |
| `consumer/scorer.py` | 调 LLM；prompt 模板；解析输出 | 调整评分标准 |
| `consumer/notifier.py` | 推 Bark | 接新通道（飞书/企微/邮件） |
| `consumer/store.py` | SQLite CRUD | 加表、加查询、改字段 |

## 第一步：Windows 上装基础依赖

### 1.1 操作系统要求

- Windows 10 1909+ 或 Windows 11
- 建议 **64-bit**，8GB+ 内存
- 微信 PC 客户端必须能正常登录（即这台机器上你常用微信）

### 1.2 安装微信 PC 客户端（必须 4.1.10.27）

**版本锁定是关键**，否则 dll 注入会因为协议变化失效。

1. 卸载现有的微信 PC（如果装了）
2. 去 [aixed/WeChat-Hook Releases](https://github.com/aixed/WeChat-Hook/releases/tag/v4.1.10.27) 下载 `WeChatWin_4.1.10.27.exe`（约 233 MB）
3. 安装这个 4.1.10.27 的安装包（不是最新版）
4. 登录你的微信，**关闭自动更新**：
   - 设置 → 通用 → 关闭"自动更新微信"
   - 卸载 `WeChatUpdate.exe`（位于微信安装目录）
5. 验证：打开微信，确认能收发消息

### 1.3 安装 Python

- 下载 Python 3.11+ 64-bit：https://www.python.org/downloads/windows/
- 安装选项勾选 `Add Python to PATH`
- 验证：`python --version` 输出 3.11+

### 1.4 安装 Git

- 下载：https://git-scm.com/download/win
- 配置：
  ```bash
  git config --global user.name "Your Name"
  git config --global user.email "your@email.com"
  ```

### 1.5 克隆代码

```cmd
cd C:\projects
git clone https://github.com/your-name/WeChatFerryTool.git
cd WeChatFerryTool
```

（如果你还没推 GitHub，先在 Mac 上 `git init` 并推到新 repo）

## 第二步：配置 SSH / Git 凭据（推到 GitHub）

如果你之前 Mac 上已经有 SSH key 推到 GitHub，Win 这台机器推荐用 SSH。

### 2.1 生成 SSH key

在 PowerShell（管理员）里：

```powershell
ssh-keygen -t ed25519 -C "your@email.com"
# 一直回车，不设 passphrase
```

### 2.2 把公钥加到 GitHub

```powershell
cat ~/.ssh/id_ed25519.pub
```

复制输出，去 GitHub → Settings → SSH and GPG keys → New SSH key，粘贴。

### 2.3 测试

```powershell
ssh -T git@github.com
```

看到 "Hi username! You've successfully authenticated..." 就 OK。

## 第三步：装 aixed/WeChat-Hook DLL

### 3.1 下载 DLL

去 [aixed/WeChat-Hook Releases v4.1.10.27](https://github.com/aixed/WeChat-Hook/releases/tag/v4.1.10.27) 下载 `version.dll`（约 472 KB）。

### 3.2 放到 WeChat 安装目录

把 `version.dll` 复制到微信 PC 的安装目录，例如 `C:\Program Files\Tencent\Weixin\`。

注意：
- **不要覆盖** `C:\Windows\System32\version.dll`（那是系统 DLL，会让 Windows 整个崩）
- 这一步需要**管理员权限**（右键 PowerShell → "以管理员身份运行"）
- **关闭杀毒软件**（360 / 火绒 / Defender 经常误报 dll）

### 3.3 验证 DLL 自动加载

启动微信（你刚才装的 4.1.10.27），扫码登录。

DLL 加载成功的标志：在浏览器访问 `http://127.0.0.1:30001/QueryDB/status`，看到 `{"IsLogin": 0, "hWeixin": ...}` 之类的 JSON（IsLogin 即使是 0 也说明 DLL 起来了，登录状态它有时候读不对，但 hook 已经生效）。

如果微信闪退，立刻：
```cmd
del "C:\Program Files\Tencent\Weixin\version.dll"
```
回滚。

## 第四步：装 Python 依赖

```powershell
cd C:\projects\WeChatFerryTool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` 内容（待补，模板见下）：

```
websocket-client>=1.6.0
pyyaml>=6.0
requests>=2.31.0
openai>=1.0.0  # DeepSeek/qwen 都兼容 OpenAI SDK
```

## 第五步：写配置

复制 `config.example.yaml` 为 `config.yaml`，根据需要修改：

```yaml
hook:
  api_base: http://127.0.0.1:30001
  callback_host: 127.0.0.1
  callback_port: 8888
  self_wxid: ""                  # 你的 wxid（可选，防止 DLL 不小心推自发消息）

filter:
  # 注意：aixed DLL 返回的是 roomid（"195940014@chatroom"），不是群显示名
  groups:
    - "195940014@chatroom"
  senders: []
  keywords:
    - "报价"
    - "合同"
    - "@你"

llm:
  base_url: https://api.deepseek.com/v1
  api_key: <你的 key>
  model: deepseek-chat
  push_threshold: 4

bark:
  enabled: true
  server: https://api.day.app
  key: <你的 Bark key>

storage:
  sqlite_path: data/messages.db
```

## 第六步：跑起来

### 6.1 启动顺序

1. **确认 version.dll 在 WeChat 目录**（`C:\Program Files\Tencent\Weixin\version.dll`）
2. **打开微信 PC 客户端**，登录
3. **启动 consumer**：
   ```powershell
   cd D:\projects\WeChatFerryTool
   .\.venv\Scripts\Activate.ps1
   python -m consumer.main
   ```

consumer 启动时会：
- 检查 DLL 是否在（探测 `127.0.0.1:30001`）
- 启 HTTP server 在 8888
- 调 DLL `/set_callback` 把自己的地址注册进去
- 进入消息循环

### 6.2 一键启动脚本

在 Git Bash 里：

```bash
./start.sh
```

会自动启动微信（要求 version.dll 已在位），然后 exec consumer。

### 6.3 验证

让群里发一条带"报价"关键词的消息，应该：
- consumer 日志打印 "入库 [roomid] sender: ... (reason=keyword:报价)"
- SQLite 里写入一条记录
- 调 LLM 评分，假设评分 >=4，Bark 收到推送

## 调试技巧

### 查看最近入库的消息

```bash
# 用 sqlite3 命令行（Win 上需要安装 sqlite3 或用 DB Browser for SQLite）
sqlite3 data/messages.db "SELECT id, group_name, sender, substr(content,1,50), score, pushed FROM messages ORDER BY received_at DESC LIMIT 20"
```

或者写个小脚本 `scripts/dump.py`：
```python
from consumer.store import Store
s = Store("data/messages.db")
for row in s.recent(20):
    print(f"[{row['received_at']}] {row['group_name']} {row['sender']}: {row['content'][:50]} (score={row['score']})")
```

### 常见故障

| 现象 | 原因 | 排查 |
|------|------|------|
| 微信启动后立即闪退 | 微信版本与 WechatFerry 不兼容 | 检查 WechatFerry README 列出的支持版本 |
| daemon 启动报 "injection failed" | 没以管理员权限运行 | 用管理员 PowerShell 重启 daemon |
| consumer 报 "Connection refused" | daemon 没起来，或端口不对 | 看 `wcf.exe --help`，检查防火墙 8888 端口 |
| 消息一直不来 | WebSocket 订阅类型不对 | 看 daemon 文档的"消息事件"协议 |
| 评分全是 3 分（score_failed） | LLM API key 错或网络问题 | 看 logs/consumer.log 里的 `score_failed:` 行 |
| Bark 推送不到 | key 是 REPLACE_ME / key 错 / server 错 | 先用 curl 直接测： `curl https://api.day.app/<key>/test/测试` |

### 单独测试某个模块

```powershell
# 测过滤器
python -c "from consumer.filter import Filter, FilterConfig; f=Filter(FilterConfig(groups=['项目群'], senders=[], keywords=['报价'])); print(f.match(group_name='项目群', sender='X', content='新报价 1000'))"

# 测评分
python -c "from consumer.scorer import Scorer, ScorerConfig; s=Scorer(ScorerConfig(base_url='...', api_key='...', model='deepseek-chat')); print(s.score(group_name='X', sender='Y', content='付款今天必须到'))"
```

### 临时关掉推送（只想看落库）

```yaml
# config.yaml
bark:
  enabled: false
```

## 第七步：日常运维

### 7.1 自动开机启动

把以下写到 `startup/startup.ps1`，再用 TaskScheduler 设为登录时启动（管理员权限）：

```powershell
# 启动 daemon
Start-Process -FilePath "C:\tools\WeChatFerry\wcf.exe"

# 延迟 5 秒后启动微信
Start-Sleep -Seconds 5
Start-Process -FilePath "C:\Program Files\Tencent\WeChat\WeChat.exe"

# 延迟 10 秒后启动 consumer
Start-Sleep -Seconds 10
Start-Sleep -Seconds 5
Start-Process -FilePath "C:\projects\WeChatFerryTool\.venv\Scripts\python.exe" -ArgumentList "-m consumer.main" -WorkingDirectory "C:\projects\WeChatFerryTool"
```

### 7.2 微信更新被禁止了，怎么办

如果自动更新漏掉（360 / 杀毒等），每次开机检查一下：

```powershell
# scripts/check_wechat_version.ps1
$path = "C:\Program Files\Tencent\WeChat\WeChat.exe"
$version = (Get-ItemProperty $path).VersionInfo.FileVersion
Write-Host "当前微信版本: $version"
# 对照 WechatFerry 支持的版本，不一致则告警
```

### 7.3 日志排查

- daemon 日志：`C:\tools\WeChatFerry\logs\`
- consumer 日志：`WeChatFerryTool\logs\consumer.log`
- 微信崩溃：检查 Windows 事件查看器 → 应用日志

## 第八步：开发模式（边改边测）

### 8.1 在 Mac 上编辑代码

```bash
cd /Users/bobo/projects/WeChatFerryTool
# 编辑 consumer/scorer.py 等
git add .
git commit -m "feat: 调整评分阈值"
git push origin main
```

### 8.2 在 Windows 上拉取

```powershell
cd C:\projects\WeChatFerryTool
git pull
# 不用重启进程（Python 是脚本），但修改了消息循环主体建议重启
Ctrl+C 停掉 consumer，重新跑
```

### 8.3 重要边界

- `consumer/main.py` 的消息循环一旦改了就要重启
- `config.yaml` 改了也要重启（建议改成启动时读一次，热重载是 nice-to-have）
- **不要 commit**：
  - `config.yaml`（含 API key）
  - `data/*.db`
  - `logs/*.log`
  - `wcf.exe` 等二进制
- `.gitignore` 模板：

```
.venv/
data/
logs/
__pycache__/
*.pyc
config.yaml
```

## 后续可以加的功能

- [ ] 热重载 config（用 watchdog）
- [ ] Web UI 看历史消息（Flask/FastAPI + 简单 HTML）
- [ ] 多账号支持（每个账号独立 daemon 端口）
- [ ] OCR 图片消息（命中关键词的图片也入规则）
- [ ] 飞书/企微 webhook 推送（Bark 之外的备选）

## 风险与限制

1. **微信版本绑定**：每次微信自动更新都会破坏 dll 注入，必须锁版本。**这是最常见的故障原因**
2. **合规风险**：个人自用风险低，不要把消息内容外发/商用
3. **dll 注入崩溃**：偶尔会让微信崩溃，daemon 通常会自动重连
4. **24h 挂机的电费**：Windows 台式机功耗 ~80-150W，每天约 2 度电，月成本约 ¥30-50

## 参考

- [WechatFerry 主仓库](https://github.com/lich0821/WeChatFerry)
- [WechatFerry-Agent](https://github.com/lich0821/WeChatFerry-Agent)
- 微信协议：[Protocol Buffers 简介](https://developers.google.com/protocol-buffers)
- Bark 推送：[https://github.com/Finb/Bark](https://github.com/Finb/Bark)
- DeepSeek：[https://platform.deepseek.com](https://platform.deepseek.com)