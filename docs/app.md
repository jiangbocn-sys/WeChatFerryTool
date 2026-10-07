# WeChatFerry 消息助手（app）

把原本散在多个终端的 consumer / Web / 定时归档 / 自动回复收进**一个常驻应用**：
双击启动 → 它自己拉起微信 → 抓取、评分、归档、按规则回复 → 退出时恢复微信。

## 快速开始

```powershell
# 1) 装依赖（首次）
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 先停掉旧的分散进程（重要！否则端口 8888/6060 冲突）
#    如果你之前用 start.sh / web.sh 起过 consumer 和 Web，先停掉它们

# 3) 双击 run_app.pyw，或：
.venv\Scripts\python.exe -m app.main
```

启动后右下角出现托盘图标，右键菜单：

| 菜单项 | 作用 |
|---|---|
| （首行） | 状态：`微信:抓取模式 / 抓取中 / 自动回复:关` |
| 打开管理页 | 浏览器打开 http://127.0.0.1:6060 |
| 自动回复 | 勾选开关（**默认关闭**） |
| 暂停抓取 | 清空 DLL 回调 → 不再收消息；取消即恢复 |
| 完全恢复 | 关微信 → 移走 `version.dll` → 重启微信到原始状态 |
| 退出 | 停抓取（不动微信） |

命令行参数：

```
--replies     启动时就启用自动回复（默认强制关闭）
--restore     退出时完全恢复（关微信 + 移走 hook + 重启微信）
--no-tray     控制台模式（不装 pystray/Pillow 时自动如此）
--no-launch   不主动启动微信，只等待你自己打开
```

## 启动/退出顺序

```
启动: 单实例检查 → Web(:6060) → consumer(:8888) → 启动微信
        → 进程一出现立刻注入 keyhook（抢在它打开数据库之前）
        → DLL API(:30001) 就绪 → consumer 注册回调 → 托盘常驻

退出: ① 先关自动回复（此后绝不可能再发消息）
      ② 停 consumer → 停 Web → 停托盘
      ③ 默认不动微信；--restore 才关微信并移走 version.dll
```

**"先开 app 再开微信"是设计前提**：consumer 的 `run(wait_for_dll=True)` 会一直等
DLL 出现；而 keyhook 必须在**微信打开数据库之前**注入，由 app 负责启动微信就能
精确抓住这个时机（手工操作时这一步很难卡准）。

## 关于"退出后让微信正常工作"

微信原版安装目录**并不包含** `version.dll`（可用另一版本的完整目录备份验证）。
该文件是 hook 本体，靠 Windows「应用目录优先」的 DLL 搜索顺序被加载。所以：

| 状态 | 判据 | 含义 |
|---|---|---|
| 抓取模式 | `version.dll` 存在 | 微信被 hook，可抓消息 |
| 原始模式 | 只有 `version.dll.disabled` | 微信完全原生，抓不到任何消息 |

因此**恢复原生 = 把文件改名**，不需要备份还原。但改名要求**微信完全退出**
（文件被占用），所以「完全恢复」会重启一次微信。

默认退出**不碰这个文件**：停止抓取，但微信进程里仍挂着 hook。这样下次启动最省事，
代价是 hook 常驻（有风控面）。要彻底干净就用「完全恢复」。

## 配置（`config.yaml` 可选的 `app:` 段）

```yaml
app:
  wechat_dir: "C:\\Program Files\\Tencent\\Weixin"
  keyhook_dll: "D:\\projects\\WeChatKeyHook\\build\\keyhook3.dll"
  launch_wechat: true          # app 负责启动微信
  auto_inject_keyhook: true    # 启动时抢注 keyhook（采密钥用）
  inject_timeout_s: 600
  dll_timeout_s: 600           # 等 DLL 就绪的上限
  web_host: 127.0.0.1
  web_port: 6060
```

## Web 状态卡片

仪表盘（http://127.0.0.1:6060）顶部有一张「应用状态」卡片，每 5 秒自动刷新：

| 显示项 | 含义 |
|---|---|
| 徽标 | 运行中 / 已暂停抓取 / app 未运行（状态过期会标注） |
| 抓取模式 | `抓取模式`（version.dll 在位）/ `原始模式`（hook 已移除，抓不到消息） |
| 微信进程 | 运行中（PID）或未运行 |
| DLL / 回调 | DLL 在线与否 + 当前回调地址 |
| keyhook 注入 | 已注入的 PID（只有采密钥才需要） |
| 自动回复 | 已开启 / 关闭 |

按钮：**开启/关闭自动回复**、**暂停/恢复抓取**、**完全恢复**、**退出 app**。
后两个都有二次确认。控制立即生效（自动回复开关不需要重启）。

两种运行方式的差别：

* **由 app 托管 Web**（`run_app.pyw`）：`/api/app` 走同进程控制器 →
  `controllable: true`，按钮可用。
* **独立跑 Web**（`web.sh`）：没有控制器，只能读 `data/app_state.json` 显示状态；
  控制端点返回 **409**，卡片会提示"本 Web 是独立进程"。

后端接口：

```
GET  /api/app                                   # 状态（live 或来自状态文件）
POST /api/app/control                           # {"action":"replies|pause|restore|quit","value":true|false}
```

## 打包成 exe

```powershell
# 目录模式（onedir）；产物在 dist\WeChatFerryApp\，整个文件夹一起分发
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1
powershell ... -File .\build_exe.ps1 -Console   # 保留控制台，便于看日志
```

约 **49 MB**（刻意排除了 `sqlcipher3` 与 `zstandard` —— 只有离线回填脚本用得到，
app 运行时不 import）。

### 数据放哪里（重要）

| 方式 | 数据根 |
|---|---|
| 默认 | **exe 所在目录**（便携，首次运行自动建 `data\`、`logs\`） |
| `--data-dir <路径>` | 指定目录，例如复用现有项目数据 |
| 环境变量 `WCF_DATA_DIR` | 同上（`--data-dir` 只是把它写进环境变量的语法糖） |

想沿用你现有的标定与历史，用：

```powershell
WeChatFerryApp.exe --data-dir D:\projects\WeChatFerryTool
```

否则把 `config.yaml`（还有需要保留的 `data\`）拷到 exe 同级目录即可。

### 几个打包相关的取舍

* **不加 UAC 清单**：实测密钥**不需要每次启动都采**（微信重启后用旧密钥仍能打开
  `message_0.db`），所以日常完全不用管理员权限。只有将来想重新采密钥时，
  才手动用管理员权限跑一次注入。
* **不用单文件（onefile）**：本 app 会做 CreateRemoteThread 注入，行为特征本就
  接近注入型恶意软件；onefile 的单文件特征会显著提高杀软误报率，而且每次启动
  都要解包（2–5 秒）。
* **杀软**：如果被拦，给 `dist\WeChatFerryApp` 整个目录加白名单（不要只加 exe）。
* **打包适配**：`paths.py` 负责区分「可写数据根」与「只读资源根」。打包后
  `__file__` 指向 `_internal`，若不改，`config.yaml`/`data`/`logs` 会全部落进
  安装目录，用户改不到配置、升级还会丢数据。

## 多账号与数据隔离

换一个微信号登录会同时打破好几处假设，所以**每个账号一套独立数据根**：

```
<数据根>/
├── config.yaml                 ← 全局（LLM key、模板等账号无关项）
└── accounts/
    └── ruibo_jiang_542e/       ← 用微信自己的账号目录名做隔离键（天然唯一）
        ├── account.json        ← self_wxid 等元信息
        ├── data/               ← messages.db / labels.json / 密钥缓存 / 语音…
        ├── logs/
        └── reports/
```

隔离键直接取微信 `xwechat_files` 下的目录名（形如 `<别名>_<短哈希>`），
所以 **`self_wxid` 可以从目录名推导**：`ruibo_jiang_542e` → `ruibo_jiang`。

这条推导很关键 —— 换账号后若 `self_wxid` 对不上，你自己发的消息会被判成
"别人发的"，而 `consumer/main.py` 是 `if not is_self:` 才调用自动回复，
结果就是**机器人去回你自己的发言**。现在每个账号都用自己推导出的 `self_wxid`。

行为说明：

| 场景 | 表现 |
|---|---|
| 首次启用隔离 | 自动把基目录下旧的 `data/ logs/ reports/` **复制**进账号目录（原件保留，确认无误后可自行删除） |
| 启动时 | 取"最近活跃"的账号（看 `db_storage` 最新写入时间）；微信还没启动也正确，因为它反映的是上次使用 |
| 运行中切号 | 监控线程检出后会**告警并暂停抓取**，避免脏数据混装；要真正切过去需重启 app |
| 手动指定 | `--account <目录名>`，例如 `--account othername_xxxx` |
| 禁用隔离 | `--no-multi-account` 或环境变量 `WCF_NO_MULTI_ACCOUNT=1` |
| 完全自定义数据根 | `--data-dir <路径>` 或 `WCF_DATA_DIR`（优先级最高，此时不做账号隔离） |

注意：`config.yaml` 仍是全局的。若两个账号需要不同的模板/LLM 配置，
目前得手工切换 —— 按账号覆盖配置还没做。

## 故障排查

日志：`logs/app.log`（应用层）、`logs/consumer.log`（抓取/评分/回复）。

| 现象 | 原因与处理 |
|---|---|
| 一直显示"等待微信" | 微信没起来，或 `version.dll` 被移走了（原始模式）→ 用托盘「进入抓取模式」 |
| 端口被占用 | 旧的 consumer/Web 还在跑 → 先停掉它们 |
| 注入失败 `Win32=5` | 权限/完整性级别不足 → 以管理员身份运行；沙箱内运行也会这样 |
| 消息不进来 | 回调被清空（微信重启会清空）→ app 会自动重注册；也可用「暂停抓取」取消再勾上 |
| 自动回复不动 | 默认就是关的 → 托盘勾选「自动回复」，或加 `--replies` |

## 安全提示

* **自动回复封号风险最高**，所以 app 默认强制关闭；开启时日志会显著告警。
* 自动回复逻辑跑在 app 进程里，**app 一退出就不可能再回复**（天然失败安全）。
* 退出流程第一步就是关自动回复，避免退出过程中发出半截消息。
* `version.dll` 常驻意味着微信一直处于被注入状态；介意的话用「完全恢复」。
