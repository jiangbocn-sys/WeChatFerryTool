# WeChatFerry 二次开发方案（Windows 裸金属）

## 目标

在 Windows 台式机上搭建一套**微信消息实时监听 + 关键词/LLM 过滤 + 重要消息推送**的工具，用于解决"群消息过载"。

```
[Windows 微信 PC 客户端]
        ↓ dll 注入
[WechatFerry daemon]              ← 默认监听 8888 端口，WebSocket
        ↓
[Python 消费脚本]                ← 连接 daemon，过滤 + 评分 + 落库
        ├→ SQLite 持久化（所有命中规则的消息）
        ├→ DeepSeek/qwen-flash 评分（1~5 分）
        └→ 评分 ≥4 的推送 Bark
```

## 与现有项目的关系

- `WeChatBridge`（已克隆到 `/Users/bobo/projects/WeChatBridge`）：不参与本方案。本方案是独立的"实时监听 + 过滤"工具
- 本方案作为 `WeChatFerryTool` 子项目，路径 `/Users/bobo/projects/WeChatFerryTool`
- **不要把 WechatFerry 的 dll / daemon 代码混入仓库**（微信协议二进制敏感，作者未明确许可证的部分不要 commit）

## 目录结构

```
WeChatFerryTool/
├── README.md                  ← 本文档
├── start.sh                   ← 一键启动（daemon + consumer）
├── stop.sh                    ← 停掉所有进程
├── config.example.yaml        ← 配置模板（拷贝为 config.example 后填）
├── consumer/
│   ├── main.py                ← Python 主入口，连接 daemon
│   ├── filter.py              ← 关键词/群/发送人白名单过滤
│   ├── scorer.py              ← LLM 评分（DeepSeek/qwen-flash）
│   ├── notifier.py            ← Bark 推送
│   └── store.py               ← SQLite 落库
├── logs/                      ← 运行时日志（gitignore）
├── data/                      ← SQLite 文件（gitignore）
└── scripts/
    └── check_wechat_version.ps1  ← 启动前校验微信版本
```

## 第一步：Windows 上装基础依赖

### 1.1 操作系统要求

- Windows 10 1909+ 或 Windows 11
- 建议 **64-bit**，8GB+ 内存
- 微信 PC 客户端必须能正常登录（即这台机器上你常用微信）

### 1.2 安装微信 PC 客户端

**版本锁定是关键**，否则 dll 注入会因为协议变化失效。

1. 卸载现有的微信 PC（如果装了）
2. 去 [WechatFerry README](https://github.com/lich0821/WeChatFerry) 看当前支持的微信版本号（通常会写 `3.9.12.xx` 之类的）
3. 找对应的离线安装包安装
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

## 第三步：装 WechatFerry daemon

### 3.1 下载 release

去 [WechatFerry Releases](https://github.com/lich0821/WeChatFerry/releases) 下载最新 `WeChatFerry.zip`。

解压到固定目录，例如 `C:\tools\WeChatFerry\`。

### 3.2 启动 daemon

以**管理员身份**运行（注入 dll 需要权限）：

```powershell
cd C:\tools\WeChatFerry
.\wcf.exe --help     # 看参数
.\wcf.exe            # 默认启动，监听 0.0.0.0:8888
```

**注意**：daemon 启动时**不要打开微信**。先启动 daemon，再开微信，否则 dll 注入不到。

启动成功的标志：看到 `WeChatFerry is ready` 类似的日志。

### 3.3 验证 daemon

另开一个终端：

```powershell
# 用任意 WebSocket 客户端连 8888 测试
# 或者装个 Python 测试
python -c "import websocket; print('ok')"
```

如果 daemon 启动后微信打开时没有崩溃弹窗（注入失败通常会让微信闪退），那就 OK。

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

复制 `config.example.yaml` 为 `config.yaml`：

```yaml
# WechatFerry daemon 连接
wcf:
  ws_url: ws://127.0.0.1:8888

# 过滤规则
filter:
  # 白名单群（只监听这些群），空数组 = 全部群都监听
  groups:
    - "客户 A 项目群"
    - "项目 X 内部讨论"
  # 白名单发送人（群里只关心这些人的消息）
  senders: []
  # 关键词命中（任一命中就入库）
  keywords:
    - "报价"
    - "合同"
    - "付款"
    - "deadline"
    - "@bobo"

# LLM 评分
llm:
  base_url: https://api.deepseek.com/v1
  api_key: <你的 key>
  model: deepseek-chat
  # 评分阈值，>= threshold 的会推送通知
  push_threshold: 4

# 推送通道
bark:
  enabled: true
  server: https://api.day.app
  key: <你的 Bark key>

# 存储
storage:
  sqlite_path: data/messages.db
```

## 第六步：跑起来

### 6.1 启动顺序（重要）

1. 启动 WechatFerry daemon（管理员 PowerShell）
2. 打开微信 PC 客户端，登录
3. 启动 consumer：
   ```powershell
   cd C:\projects\WeChatFerryTool
   .\.venv\Scripts\Activate.ps1
   python -m consumer.main
   ```

### 6.2 一键启动脚本

`start.sh`（在 Git Bash 里跑）：

```bash
#!/bin/bash
# 启动顺序：先 daemon，再微信（手动），再 consumer

# 1. 启动 daemon（后台）
echo "Starting WechatFerry daemon..."
start /B "C:\tools\WeChatFerry\wcf.exe" || echo "请手动以管理员身份启动 wcf.exe"

sleep 3

# 2. 启动 consumer
echo "Starting consumer..."
cd "$(dirname "$0")"
source .venv/Scripts/activate
python -m consumer.main
```

### 6.3 验证

让群里发一条带"报价"关键词的消息，应该：
- consumer 日志打印 "received msg from group X"
- SQLite 里写入一条记录
- 调 LLM 评分，假设评分 >=4，Bark 收到推送

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