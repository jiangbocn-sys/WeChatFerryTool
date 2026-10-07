# 需求台账（WeChatFerryTool）

> 用法：客户/用户提出的需求先落到这里（一条一个编号），确认设计后再改代码。
> **状态**：`待确认` → `已确认` → `实现中` → `已完成`（或 `已放弃` + 原因）
> 相关文档：`STATUS.md`（状态与待办）、`docs/checkpoint.md`（坑清单）、`tests/regression/README.md`（回归）

| 编号 | 需求 | 状态 | 提出日期 |
|---|---|---|---|
| R-001 | 消息数据库按天数自动清理（后台静默） | **已完成**（2026-10-07，见下「实现记录」） | 2026-10-07 |
| R-002 | **抓取范围全开：所有会话都入库；监控名单只用于筛选/归档/总结** | **已完成**（2026-10-07） | 2026-10-07 |

---

## R-002 · 抓取范围全开（监控名单只管归档/总结）

**状态**：已完成（2026-10-07）
**提出人**：姜波

### 1. 需求原文

> 我需要的是抓符合设置条件的全部消息，监控名单是做筛选和总结用的。

### 2. 改动前的行为（问题）

`filter.groups`（就是「监控名单」）**同时**是"入库白名单"：名单外的群/会话，
消息在 `Filter.match()` 就被丢掉，**连库都进不去**。后果（2026-10-07 实测）：

* 9:47 文璟和颂业主群（`34414106436@chatroom`，不在名单里）的消息在系统里**查无此条**，
  用户以为"抓取坏了"；实际是 `filter.match` 静默丢弃（只写 debug 日志，INFO 看不到）。
* 控制台/浏览面板里只有监控名单里那几个群的消息 → 看着像"只抓了几个群"。

### 3. 改动后的语义（用户确认）

| 环节 | 规则 |
|---|---|
| **入库** | **所有会话都入库**，只受「入库类型闸门」限制（`storage.ingest_exclude_types`，表情 47 恒排除）；`filter.groups` / `filter.senders` / `filter.keywords` **都不再参与入库** |
| **归档 / 总结** | 仍由 `digest._monitored_groups()`（= `filter.groups`）决定范围：群在名单里且没设重点人 → 整群归档；设了重点人 → 只归档这些人；另有"每群敏感关键词命中"和"全局重点联系人"两条补充 |
| **自动回复** | 不受影响：生效范围由**模板自己的 scope** 决定（`replier._match_scope`），入库放开不会让回复变宽 |
| **`filter.keywords`** | **成为空转项**（入库不看、`_in_digest` 也不读它）。本次**保留配置值不删**，页面上标注"当前未生效"，并引导去标定页配"每群敏感关键词"。要不要正式废弃/改名为 `digest.keywords`，留待用户决定 |

### 4. 代码落点

| 文件 | 改动 |
|---|---|
| `consumer/main.py` | 删掉 `handle_message` 里的 `self.filter.match()` 闸门与 `Filter` 的构造/导入；入库日志去掉 `reason=`；补注释说明 R-002 |
| `web/app.py` | `FIELDS` 里 `filter` 段改名「监控名单（只决定归档/总结范围）」并重写 4 个字段的文案；`/filter/update` **不再接收/写入 keywords**（避免"看着能改、其实没用"） |
| `web/templates/filter.html` | 页头改成「监控规则（归档范围）」+ 醒目提示"本页不影响入库"；删掉「入库关键词」表单，改为「归档关键词（当前未生效）」说明卡 + 标定页入口 |
| `web/templates/_monitor_block.html` / `config_form.html` | 文案改为"只决定归档范围" |
| `tests/regression/wft_ingest_scope_check.py` | **新增回归**：真 `Consumer` + 假 hook，断言名单外的群/私聊**照样入库**、类型闸门仍有效、`_in_digest` 仍按监控名单筛选、入库日志不再有旧 `reason=` |
| `wft_isolation_check` / `wft_rules_page_check` / `wft_formstruct_check` | 同步更新到新语义（关键词不再被写入、页面不再有 keywords 表单/form 数 5→4） |

### 5. 验收（回归 `wft_ingest_scope_check`，全绿）

* 名单里的群 / **名单外的群** / 名单外的私聊 → **都入库**；
* 表情 47、系统 51 → 仍不入库（类型闸门没被放开）；
* `_in_digest`：名单里的群进归档，**名单外的群和私聊不进归档**；
* 入库日志不再出现旧过滤原因 `reason=group_match`。

### 6. 顺带发现（未修，已记录）

`storage.sqlite_path` 是**相对路径**，而 `Store` 按**当前工作目录**解析它
（只有 app 会 `chdir` 到数据根）。所以绕开 app 直接用 `Consumer`/`Store` 时
（脚本、测试），相对路径会落到**项目目录下的老库** `WeChatFerryTool\data\messages.db`，
而不是账号数据根 —— 10-07 写回归时踩到（进程里出现 4233 条真实数据，说明打开了真库）。
**默认安全**（app 一直会 chdir），但想让工具也能脱离 app 安全运行，需要把
`Store` 改成"相对路径 → 数据根优先"。已记进 `docs/checkpoint.md`。

---

## R-001 · 消息数据库按期自动清理（后台静默执行）

**状态**：已完成（2026-10-07 实现；验收标准 A1~A9 由回归套件 `wft_retention_check` 覆盖）
**提出人**：姜波

### 1. 需求原文

> app 自己的消息数据库需要定期清理，在设置中需要增加自动清除超过多少天的记录，
> app 需要在后台静默的完成过期数据的清理。

拆成三条：

1. **设置项**：总设置里能配"自动清除超过 **N** 天的记录"。
2. **自动**：不需要用户手动点，按配置定期清理（一天最多一次）。
3. **静默**：后台完成，不弹窗、不弹通知、不打断抓取；只在日志里留一行，页面上可查状态。

### 2. 现状与数据事实（2026-10-07 实测）

| 项 | 值 |
|---|---|
| 库文件 | `accounts\<账号>\data\messages.db`（当前 **7.07 MB**，增长主要来自群消息）|
| 日志模式 | **WAL**（`PRAGMA journal_mode=WAL`，`synchronous=NORMAL`）|
| 表 | `messages(id, msg_id UNIQUE, group_name, sender, sender_id, content, msg_type, received_at, score, score_reason, pushed, priority, direction, transcript)` |
| 索引 | `idx_messages_received_at`（按时间删可行）、`group_name`、`score`、`priority` |
| 无 FTS | 目前**没有** FTS 影子表，删除不需要同步索引 |
| 相关文件 | `logs\*.log` 1.76 MB（会持续长）、`reports\*.md`（归档/总结产物）、`digest_state.json`、`labels.json(.bak-*)`、`wechat_contacts.json` 0.31 MB |

### 3. 建议设计（待确认）

**配置项**（放在总设置页「存储 / 数据」区块；`FIELDS` 描述表驱动，带「影响 / 建议」文案）：

```yaml
storage:
  retention_days: 0          # 0 = 不自动清理（建议默认，先观察再打开）
  auto_cleanup: true         # 开关
  cleanup_time: '04:30'      # 每天这个点之后跑一次（低峰）
  cleanup_vacuum: false      # 是否顺带 VACUUM 回收磁盘（默认否）
```

**调度**：挂在 consumer 的日常循环里（与 23:30 归档同一个循环，60 秒一跳）：

* 条件：`auto_cleanup` 为真 **且** `retention_days >= 7` **且**
  今天还没跑过（状态记在 `data/cleanup_state.json`：`{"last_date": "YYYY-MM-DD", "deleted": N, "at": ts}`）
  **且** 当前时间已过 `cleanup_time`；
* 一天最多一次；抓取繁忙时自动让路（`busy_timeout` + 分批）。

**删除**：`DELETE FROM messages WHERE received_at < ?`（阈值 = 现在 - N×86400，走 `received_at` 索引）

* **分批**：每批 2000 条 + 批间 `sleep(0.2)`，循环直到删完或收到 `_stop` 信号（可中断、不卡死抓取）
* 只删**早于**阈值的行；当天的数据绝不动
* 删完 `PRAGMA wal_checkpoint(TRUNCATE)` 收缩 WAL；`cleanup_vacuum` 为真时才 `VACUUM`
  （VACUUM 需要约等于库大小的临时空间且会持锁较久 → 默认关闭，另给一个"立即回收空间"的手动按钮更稳）

**静默与可观测**：

* 只在 `logs\consumer.log` 写一行 INFO：`数据清理：删除 N 条（保留 N 天），耗时 x ms，当前共 M 条`
* **不发**托盘通知、不弹窗、不阻断入库
* 页面上可查：保留天数 / 上次清理时间 / 上次删除条数 / 当前总条数（放 `/settings` 的「数据清理」区块或仪表盘）
* 配置页给一句"按当前设置将删除 X 条"的**预览**（dry-run，只统计不删）

**CLI**（便于排查/手动跑）：

```powershell
.\.venv\Scripts\python.exe -m consumer.cleanup --dry-run --days 90   # 只报数
.\.venv\Scripts\python.exe -m consumer.cleanup --days 90             # 真删 + checkpoint
.\.venv\Scripts\python.exe -m consumer.cleanup --days 90 --vacuum    # 顺带回收空间
```

### 4. 必须一起考虑的影响面

| 影响 | 说明 / 对策 |
|---|---|
| **历史归档重跑** | 消息被删后，`/digest` 或 `summarize --date <旧日期>` 再也生不出内容（返回 0 条）。→ 保留天数要 ≥ 用户可能回看的天数；**已生成的 Markdown 报告不会被删**；页面在"0 条"时给可读提示而不是崩 |
| **当天的归档/总结** | 清理不碰当天；且 `cleanup_time`（默认 04:30）与 `digest.time`（23:30）错开 |
| **磁盘其实不会变小** | SQLite 删行不缩文件；要么 checkpoint（WAL），要么 VACUUM。预期管理：默认只 checkpoint |
| **抓取不能被打断** | 分批 + 批间 sleep + `busy_timeout` + 可中断；清理失败只记日志，绝不影响入库/回调 |
| **多账号** | 只清当前数据根（`accounts\<账号>\data\messages.db`）|
| **备份文件类** | `labels.json.bak-*` 已只留最近 5 份；可选一并按天数清理 `logs\*.log`（建议做，1.76 MB 且持续长）|
| **不该清理的** | `reports\*.md`（用户产物，默认永不删）、`labels.json`、`wechat_contacts.json`、`app_state.json` |

### 5. 验收标准（可测；实现后加一套 `wft_retention_check`）

* **A1** 总设置页出现「保留天数 / 自动清理 / 清理时间」三项，改其中一项**不影响**其它配置段（`wft_isolation_check` 仍全绿）
* **A2** 边界：正好 N 天的记录**保留**，N+1 天的**删除**
* **A3** `retention_days: 0` → 完全不删；小于 7 的非法值 → 拒绝保存或按 7 处理（要定）
* **A4** 一天最多跑一次；`cleanup_state.json` 记录 `last_date`
* **A5** 可中断：设置 stop 后当前批结束即退出，进程能正常收尾
* **A6** 静默：过程中无托盘/弹窗调用（回归里断言没调用通知接口），仅日志
* **A7** dry-run 报数与真删条数一致
* **A8** 删除后 `wal_checkpoint(TRUNCATE)` 被调用（`--vacuum` 时才 VACUUM）
* **A9** 清理后 `/digest`、`/browse`、`/labels`、`/` 全部正常（旧日期归档显示"0 条"且不 500）

### 6. 待用户确认的 4 个点

1. **保留天数默认值**：建议 `0`（先不自动删，观察一段时间）→ 打开后建议 `90` 天。
   你也可以直接定一个数（例如 30 / 60 / 180）。
2. **是否连带清理 `logs\*.log`**（例如按 30 天或单文件 > 10 MB 轮转）？建议**做**。
3. **要不要 VACUUM**（能真正缩小文件，但会持锁较久、需要额外磁盘）？建议默认关，另给手动按钮。
4. **要不要"立即清理一次"按钮**（带 dry-run 预览）？建议**要**，方便你确认效果。

---

## 7. 实现记录（2026-10-07）

### 7.1 用户拍板的 4 个点

| 点 | 结论 |
|---|---|
| 保留天数默认值 | **默认 0（不自动清理）**；打开后建议 90 天。设置页可随时改。 |
| 是否连带清理 `logs\*.log` | **暂不做**（本次只清消息库，日志清理留作后续） |
| 要不要 VACUUM | **默认关**；页面「立即清理一次」旁给 `顺带 VACUUM` 勾选框 |
| 要不要"立即清理一次"按钮 | **要**，并且在旁边先给「预演一次（只报数）」 |

### 7.2 配置项（`setup.py` 生成 + `config.example.yaml` 示例）

```yaml
storage:
  retention_days: 0          # 0 = 不自动清理（默认）；1~6 视为非法 → 拒绝执行
  auto_cleanup: true         # 开关（还需 retention_days >= 7）
  cleanup_time: '00:00'      # 用户指定：每晚 0 点后跑一次（可改成 04:30 等）
  cleanup_vacuum: false      # 只影响"自动清理"；页面手动清理有独立勾选框
```

### 7.3 代码落点

| 文件 | 内容 |
|---|---|
| `consumer/cleanup.py` | **新模块**：`retention_cutoff()`（保留 N 个自然日，门槛=本地零点）、`cleanup_messages()`（分批 2000 + 批间 sleep + 可中断 + dry-run + checkpoint + 可选 VACUUM）、`should_run()`（到点/开关/下限/一天一次）、状态文件 `data/cleanup_state.json`、CLI `python -m consumer.cleanup` |
| `consumer/main.py` | 新增 `_cleanup_loop()` 常驻线程（60 秒一跳，与归档循环同构）；启动时按配置打印"已启用/未启用" |
| `web/app.py` | `FIELDS` 新增「数据清理」区块（保留天数 / 启用自动清理 / 每天几点清理，`kind: time` 校验 HH:MM）；`/settings` 增加「🗄 数据清理」区块（当前生效 / 上次清理 / dry-run 预览 / 预演按钮 / 立即清理按钮）；新增 `POST /settings/cleanup`（`mode=dry_run|run`） |
| `web/templates/settings.html` | 数据清理区块（表单在大表单**之外**，避免 form 嵌套） |
| `tests/regression/wft_retention_check.py` | **新增回归套件**，覆盖 A1~A9 + CLI |

### 7.4 关键实现口径（容易误解的地方）

* **"保留 N 天"= 保留最近 N 个自然日（含今天）**：门槛 = 今天零点 -（N-1）天，
  `received_at < 门槛` 才删。所以 `retention_days: 30` 删的是"第 30 天往前"的记录，
  **当天数据永不参与**（门槛最小也是今天零点）。
* **下限 7 天**：`0` = 不清理；`1~6` = 保留但**拒绝执行**（页面与日志都会说明原因）。
  这是防手滑的保护，不阻断保存（只提示）。
* **静默**：只写 `logs/consumer.log` 一行与状态文件；**不调用** 通知（回归里用 spy 断言
  `Notifier.push` 没被调用）。
* **可中断**：`stop_event` 置位后当前批删完即退出，`interrupted=True`，状态文件如实记录已删条数。
* **磁盘不会立刻变小**：默认只 `wal_checkpoint(TRUNCATE)`；要缩文件得 `VACUUM`（默认关）。
* **不影响归档产物**：`reports/*.md`、`labels.json`、`wechat_contacts.json` 都不参与清理；
  但**已被删的消息**重跑旧日期归档会得到 0 条（页面给可读提示，不 500）。
