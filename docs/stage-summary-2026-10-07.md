# 阶段总结 · 2026-10-06 ~ 10-07（交接用）

> 本轮从"Web 配置界面"一路做到"离线读微信库 + 每群单独总结"，并修掉 7 个真 bug。
> **新 session 请按本文件的「下一步」继续。**

## 1. 当前可用状态

| 项 | 状态 |
|---|---|
| 打包产物 | `dist\WeChatFerryApp\WeChatFerryApp.exe`（2026-10-07 00:22，10.93 MB；整包 286.1 MB）|
| 登录/抓取 | 正常（消息靠 `version.dll` 回调推送；抓取**不需要** keyhook）|
| 归档 | 每天 23:30 一份 `reports\digest-<日期>.md`（内部按群分节）。10-06 23:30 实测成功（5 条 / 1 会话）|
| 总结 | `digest.summarize_mode: per_group` → 每群一份 `reports\summary-<日期>-<群名>.md`，每群一次 LLM 调用 |
| 标定 | 可手工改，也可「从已抓消息挖昵称」/「从微信库离线导入昵称·群成员」|
| 监控名单 | 「🎯 监控规则」页（`/filter`）下拉勾选；决定归档范围 |
| 回归 | `tests/regression/run_all.ps1` → **ALL 27 SUITES PASSED**（3 套环境相关的默认跳过）|

## 2. 本轮新增/改动的主要文件

| 文件 | 作用 |
|---|---|
| `consumer/wechat_offline.py` | **离线读微信库**：口令候选→SQLCipher 只读解密→`data/wechat_contacts.json`（联系人/群/群成员）→合并进 `labels.json` |
| `consumer/name_harvest.py` | 从已抓消息里挖昵称（引用消息 XML）+ 人工补充 `data/name_overrides.json` |
| `consumer/contact_sync.py` | 走 DLL 的昵称发现（本机 DLL 不可用，保留作将来；含 `probe()` 诊断）|
| `consumer/summarize.py` | 每群一份/合并两种模式、`SummarizeRun`、文件名清洗、失败隔离 |
| `consumer/digest.py` | 归档选取规则：**监控名单**参与判定（`_in_digest(..., monitored)`）、`_monitored_groups()` |
| `app/supervisor.py` | keyhook 注入安全闸门 `inject_safety_error()`；默认不注入 |
| `web/app.py` | 总设置页表单化（`FIELDS` 描述表）、监控名单增删、离线导入接口、`/api/version`、NULL/DB 错误兜底 |
| `web/templates/` | `config_form.html`/`_monitor_block.html`/`config_focus.html`/`config.html`/`config_edit.html`/`settings.html`/`llm.html`/`browse.html`/`labels.html`/`filter.html`/`digest.html` |
| `tests/regression/` | 29 套回归 + `run_all.ps1` + `README.md` |
| `build_exe.ps1` | 加 `--collect-all sqlcipher3/zstandard`；**保持纯 ASCII**（PS 5.1 会把无 BOM 的 .ps1 当 GBK 读）|

## 3. 关键决策（含理由）

1. **昵称走"离线读库"，不走 DLL QueryDB**：本机那份 DLL 的 `g_IsLogin` 恒为 0（源码里从没置 1），`GetAllDBName` 永远空；而离线读用已采口令就能拿到 5492 联系人 + 3366 群成员。
2. **keyhook 注入默认关闭**：实测"app 启动微信 → 开库前注入"会让微信**对话历史空白**；"先手动开微信再启动 app"正常（两次都注入了，差别只在时机）。抓消息不需要 keyhook，所以默认关。
3. **安全用法**：先手动开微信 → 再启动 app；`launch_wechat: false`、`auto_inject_keyhook: false`。
4. **总结每群一份**（用户选）：群主题差别大，合并成一篇读不出条理；调用次数 = 有发言的群数。
5. **归档范围 = 监控名单**：没设重点人 → 整群归档；设了 → 只归档重点人。私聊只有 ★重点联系人才进。
6. **保存必须隔离**：三处保存都逐字段合并 + 误提交整份拒绝 + 写前备份（用户明确要求，且有一次真实事故）。

## 4. 遗留 / 下一步（建议顺序）

| # | 事项 | 说明 |
|---|---|---|
| 1 | **让用户在界面上点一次「📥 从微信库导入昵称/群成员（离线）」** | 我的沙箱不能写账号目录，所以真实导入没跑；点完看统计（预计 5492/32/3366）|
| 2 | 观察 10-07 23:30 的**每群一份**总结 | 应有 `summary-2026-10-07-<群名>.md`（有发言的群才有）|
| 3 | `digest.exclude_types` 做成 Web 开关 | 现在只能在 `/config` 的归档段用勾选组改（已可用），可再加到 `/settings` |
| 4 | 把 `llm-test-20261006\reports\summary-2026-10-05.md` 拷进账号 `reports/` | 历史产物归档 |
| 5 | 单实例互斥体加开关 / `dist` 打 zip 分发 | 可选 |
| 6 | 清理临时目录 | `D:\projects\.wft-*`（我的测试残留）、`D:\projects\.acl-recovery-20261006\`、`D:\projects\llm-test-20261006\`、`config.yaml.wizard-accident`、`config.yaml.bak-*`（留最近几份即可）|
| 7 | git 提交 | ✅ `.gitignore` 已补（`config.yaml*` / `dist/` / `build/` / `*.bak-*` / `.wft-*/`），`config.example.yaml` 可提交；**本次仍未 add/commit**，等你看过再提交 |
| 8 | 可选：给"被覆盖的名字"做一键回退 | 导入把原名存进了 `name_prev`，可以做个按钮还原 |
| **9** | **【需求 R-001】消息库按天数自动清理（后台静默）** | **✅ 已完成（10-07 晚，见第 6 节）**。规格与决策记录在 `docs/requirements.md` R-001 |

## 5. 新 session 开工贴士

* 先看 `docs/checkpoint.md`（那里是踩坑清单）和本文件的「下一步」。
* 复跑回归：`powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\regression\run_all.ps1`。
* **DSH 沙箱对本项目的具体限制**（会反复遇到，别浪费时间）：
  - 子进程**不能写** `accounts\...\data`、`WeChatFerryTool\data`、`dist`、`build`；`Ask` 授权后才可写 `dist/build`。
  - 子进程**不能**建托盘图标（WinError 5）、不能 `chmod` 临时目录（`TemporaryDirectory` 清理会报权限错）。
  - 子进程打开真实 `messages.db` 有时会 `unable to open database file`（SQLite 要写 `-shm`）——**这是环境问题，不是代码 bug**。
  - 只有**我自己的文件工具**能写仓库里被 ACL 限制的文件（shell 会被拒），所以改那些文件要用编辑工具。
  - 如果遇到"本来能读的路径突然读不了 / 明明在工作区内却写不进去"，**先用技能
    `diagnose-windows-sandbox-acl`**（一条命令会检查该路径及所有祖先目录，并在同一次运行里修好它证实的 ACL 问题），
    不要盲目重试或改路径绕开。
* **不要**再让 app 自己去启动微信（会触发开库前注入）；也不要为了采密钥去改这个默认。
* 微信目录里**装着 hook**：`C:\Program Files\Tencent\Weixin\version.dll`（aixed，sha256 `23948E7D…`）。
  要还原微信：托盘「完全恢复」或 `WeChatFerryApp.exe --restore`（需管理员，把 `version.dll` 改名成 `version.dll.disabled`）。

## 6. 2026-10-07 晚（第二段）：git 脱敏 + R-001 消息库清理

### 6.1 git 脱敏（未提交，等你确认）

* `.gitignore` 补全并转成 **UTF-8**（原文件中文注释是乱码）：新增 `config.yaml*`（原来只有
  `config.yaml`，**备份文件 `config.yaml.bak-<ts>` 与 `config.yaml.wizard-accident` 不在忽略里**）、
  `dist/`、`build/`、`*.spec`、`*.bak-*`、`*.wizard-accident`、`.wft-*/`、`*.db-wal/-shm`。
* 核查结论：`config.yaml` **从未被 git 跟踪**，`git grep` 全历史也没有 `sk-...` 形态的密钥；
  当前**暂存区为空**（什么都没 add）。
* ⚠️ 仍未忽略、值得你决定的大件：`vendor/`（229 MB，含微信安装包与 keyhook DLL）、
  `accounts/`（会被 `data/` 兜住，只剩 `account.json`）。要我加规则就说一声。
* 提交命令（你自己跑或让我跑）：
  `git add .gitignore config.example.yaml consumer web tests docs setup.py paths.py app ... ; git commit -m "..."`
  —— 注意**别看都不看就 `git add -A`**。

### 6.2 R-001 消息库按期自动清理（已完成）

* 新增 `consumer/cleanup.py`：`retention_cutoff()` / `cleanup_messages()` / `should_run()` / CLI。
* consumer 增加 `_cleanup_loop()` 常驻线程（60 秒一跳，与归档循环同构，一天最多一次）。
* 总设置页新增「数据清理」区块：**保留最近多少天的消息（0 = 不清理）** / **启用自动清理** /
  **每天几点清理（默认 00:00）**；`/settings` 新增「🗄 数据清理」：当前生效状态、上次清理、
  **预演（dry-run 只报数）**、**立即清理一次**（可顺带 VACUUM）。
* 铁律仍然成立：**不让 app 启动微信**（本次没碰 supervisor / 注入逻辑，`wft_inject_guard_check` 全绿）；
  **保存必须隔离**（改配置只写 `storage` 里那三项；`wft_isolation_check` / `wft_form_check` 全绿）。
* 新增回归 `wft_retention_check`（A1~A9 + CLI）；全套 **ALL 27 SUITES PASSED**。
* 本次实际踩到并修掉的坑（两个都写进 `checkpoint.md` 了）：
  1. 全新库（有 db 文件、没 messages 表）时 `/settings` 500（`db_query(...)[0]` 越界）；
  2. 迁移到"原样保存一次"时，`_form_value` 的 time 分支给空值兜了默认 `"00:00"`、
     bool 分支给不存在的键写 `False` → 凭空多出配置项（旧的隔离性回归立刻抓到）。
