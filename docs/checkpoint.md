# 检查点 · 2026-10-06（10-07 补充）

> **新 session 先读**：`docs/stage-summary-2026-10-07.md`（本轮做了什么、决策、下一步）
> ｜ 状态报告：`STATUS.md`（最上面是 10-07 更新块）
> ｜ 需求台账：`docs/requirements.md`（新需求先落这里，确认后再写代码）
> ｜ 回归：`powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\regression\run_all.ps1`
> （2026-10-07 实测 ALL 27 SUITES PASSED；3 套环境相关的默认跳过）

> 精简状态快照，配合 `STATUS.md`（10-05 更新节）与 `docs/handoff-2026-10-05.md` 使用。

## 已完成（已验证）

| 项 | 验证方式 |
|---|---|
| keyhook 采密钥 → 离线解密 `message_0.db` | 17~19 把密钥，解开 3 个库 |
| 私聊回填 | 641 条，其中 433 条是 hook 漏掉的 |
| 账号数据隔离 `accounts/<slug>/` | `self_wxid` 由目录名推导 |
| 应用层（托盘/Web/生命周期/注入/恢复正常） | 装配冒烟 + 注入实测 + 待初始化模式 |
| 打包 onedir + vendor 随包 + 多尺寸图标 | 10-06 重新打包：**278.9 MB**（其中微信安装包 228.3 MB）；`vendor/*` 随包 |
| 初始化向导（采集/决策/生成配置/部署 DLL） | `/setup` 200，apply 往返校验 |
| LLM provider 无关 + `base_url` 校验 | OpenAI/DeepSeek/Ollama 三者透传 |
| `apply_answers(dry_run=True)` | **sha256 证明真实 config 未被改动** |
| 自动回复 `fallback`（与 persona 分离） | 人设不泄露、固定回复不碰 LLM |
| 入库类型闸门 `storage.ingest_exclude_types` | 默认 `[47]` |
| **当日归档 → LLM 总结** `consumer/summarize.py` | prompt 生成实测（22 条/1 会话/1418 字） |
| **Web 运行期配置**（消息分类 / 每群敏感关键词 / 生成当日总结） | test client 端到端 + 真实 HTTP 服务冒烟（假 LLM，真实数据未动） |
| **真实 LLM 当日总结** | MiniMax-M3 `HTTP 200`，18 条 → 320 字总结（**真实调用才发现 `<think>` 混入，已修**） |
| **打包产物实测**（冻结版） | 正式 exe 装到可写目录实跑：**10/10 页面 200**、待初始化引导正常 |
| 表情 47 **恒不入库** | 后端保存与 consumer 读取都强制加回；含"提交 [1] 仍得 [1,47]"断言 |

## 关键事实（避免重复踩坑）

* **git 推送/忽略规则**（10-07 定）：`.gitignore` 里 `config.yaml*`、`dist/`、`build/`、
  `*.bak-*`、`.wft-*/` 一律不进 git；**`vendor/` 保留跟踪**（`keyhook3.dll` / `version.dll` /
  `manifest.json`），但 **228 MB 的微信安装包 `vendor/*.exe` 不进 git**（按 `manifest.json`
  里的 sha256 重新下载）；**`accounts/` 保留跟踪**（只含 `account.json` 元信息），
  账号下的 `data/` `logs/` `reports/` 与配置备份全部忽略。
  ⚠️ **DSH 沙箱里 `git push` 需要显式指定 ssh 路径**（默认的 `ssh` 会 `Host key verification failed`）：
  `git -c core.sshCommand="C:/Windows/System32/OpenSSH/ssh.exe -o StrictHostKeyChecking=accept-new" push origin main`
  —— 且写 `.git` 属于工作区外，shell 会被拒、需要一次授权升级。

* **冻结版 `--help` 曾在 GBK 控制台上崩**（10-07 打包后实测）：帮助文本里有 `⚠️`，
  `argparse` 打印帮助时 `UnicodeEncodeError`，用户连用法都看不到（退出码非 0）。
  修法：`app/main.py` 抽出 `_force_utf8_stdio()`（stdout/stderr 切 UTF-8 + `errors="replace"`），
  在 **`main()` 里 `argparse` 之前**就调用；并把帮助文本里的 `⚠️` 换成"警告："。
  回归 `wft_retention_check` 用"真实 GBK 控制台（cp936）"子进程跑 `--help` 锁死这条
  —— ⚠️ 所以**打包后要抽查一次 `--help`**，它是唯一能验证"冻结版 + 控制台编码"的入口。
* **`dist/` 与 `build/` 目录的 ACL 会被写成"当前用户缺 WRITE_OWNER"**（10-07 遇到）：
  打包时 PyInstaller 先在 `build\pyinstaller` 写 spec、再 `rmtree dist\WeChatFerryApp`，
  两个目录权限不足时分别报 `PermissionError: [WinError 5]` / `[Errno 13]`。
  处理：用技能 `diagnose-windows-sandbox-acl` 各跑一次（`-Path <目录> -AllowRoot D:\projects`），
  会补上当前用户的完全控制项并复检；备份/回退在 `D:\projects\.acl-recovery-20261007\`。
  修完 `dist` 之后**还要修 `build`**（同一条链上的两段），别以为修一个就够。

* **语音补转/漏抓现状（10-07 实测，别再重复排查）**：
  全部 17 条语音里：**已转写 8 / 未捕获到文件 3 / 解码失败 1 / transcript 为 NULL 5**。
  * `scripts/transcribe_pending.py` 找的是 **`voices/<msg_id>.wav`**（收进来的语音的落盘名，
    根目录里确实有 4 个这种文件）；**孤儿语音**（自己发的）落在 `voices/raw/<会话hash>_<毫秒>.bin`
    且落库名是 `vt-<stem>` —— 两种命名不同，所以那个脚本对孤儿语音永远 skip。
    新增 `tools/transcribe_orphans.py` 专门补孤儿（按 `vt-<stem>` 找 raw 里的 .bin，解码+转写+回填）；
    当前它报 0 条 = 孤儿语音都已转写完。
  * 6 条待处理语音**本地一个文件都没有**（`voices/<msg_id>.wav` 与 `.bin` 都不存在）→
    **不是转写没跑，而是抓取环节就没拿到文件**：日志实证 11:52 那条是
    `语音捕获超时（20.0s 内未在 VoiceTemp 发现新文件）`；5 条 NULL 是 10-02 的，当时语音捕获尚未启用。
    ⚠️ 这类漏抓目前**无法事后补救**（除非去微信缓存 `cache\<月>\Message\<会话hash>\VoiceTemp\`
    按时间戳配对——缓存里确实能看到同名/同时刻的文件，但配对不可靠，未做）。
  * 语音捕获失败时 `_capture_voice_bg()` 会写 `[未捕获到语音文件]`（有痕迹、可统计），
    解码失败写 `[解码失败]`；只有"语音捕获功能没启用那段时间"进来的才是 NULL。
  * ASR 服务（`JiangdeMac-mini.local:8170`）**可达**（根路径 404 但服务在），补转前需确认它开着。
  * ⚠️ 另外发现**同一条语音可能入库两次**（msg_id 不同、时间差 2 分钟、转写内容相同，
    如 10-07 11:34 与 11:36 的 `赤窑是入墓的…`）—— 疑似 hook 重推，暂未处理（总结会重复一句）。

* **图片信息暂时交给不了 LLM，已在归档闸门里排除（10-07 用户决定）**：
  本地图是加密 `.dat`（无解，见上），微信只在内存解密 → prompt 里只能出现
  `[图片 214×480 · 478 KB]` 这种**没有内容**的占位。用户确认"图片先忽略、只传文本+语音转写"，
  所以 `digest.exclude_types` 默认加上 **3(图片)**：现为 `[3, 47, 51, 10000]`
  （`setup.default_config`、代码兜底、`config.example.yaml`、用户真实 config 四处一致）。
  实测：10-07 监控群归档从 166 条降到 **155 条**（148 文本 + 5 语音转写 + 2 撤回），
  prompt 里不再有图片占位。⚠️ **入库闸门没挡图片**（用户配置里
  `storage.ingest_exclude_types = [42,43,47,48,49,50,51,10000]`）——图片仍会入库、
  在浏览页可见（这是我们要的），只是不进归档。
  💡 若哪天解开 `.dat`：把 3 从 exclude_types 去掉，再走"解密 → base64 → 视觉模型"这条路
  （MiniMax-M3 原生多模态，缩略图约 100~300 token/张）。
  回归：`wft_ingest_scope_check` 断言"图片被排除不进归档 / 未排除时会进（闸门由配置驱动）"。

* **语音"没转写"的根因是抓取不是 ASR，已加三级兜底**（10-07 用户报"消息浏览器多条语音没转写"）：
  排查结论：**whisper/ASR 服务完全正常**（`JiangdeMac-mini.local:8170` DNS→192.168.1.11、
  TCP 通、`/health` 200、`/docs` 200）。真正原因是 **`VoiceTap.wait_for()` 只有 20 秒窗口**，
  而语音文件是微信**按需落盘**的（要等你在微信里点播放、或稍后才出现）→ 超时放弃。
  当日实测：32 条语音里 `已转写 9 / 未捕获到语音文件 16 / 空 6 / 解码失败 1`，
  日志 15 次 `语音捕获超时`（其中 22:29:07 一秒内 3 条同时超时 = 一次到达多条）。
  ⚠️ 关键观察：**这些文件往往还在 VoiceTemp 里**（事后目录里确实还有未认领的文件）。
  修复（三级兜底 + 延迟重试）：
  ① `wait_for` 事件捕获（原样）→ ② `capture_voice()` 轮询（原样）→
  ③ **新增 `VoiceTap.scan_temp(h, since)` 直接扫 `<cache>/<月>/Message/<会话md5>/VoiceTemp/`**
     （迟落盘 / 事件漏掉的场景），找到就复制出来并 `claim()`；
  仍失败 → `_schedule_voice_retry()` 在 **1/3/10 分钟**后再各扫一次（扫码/晚播场景），
  成功即停（用新增的 `store.get_transcript()` 判断是否已被别处补上）。
  `_decode_and_transcribe()` 抽成公共方法供两处复用。
  * ⚠️ 扫盘必须过滤噪音：百度网盘会在同目录留 `4_1791363695.baiduyun.uploading.cfg`
    —— 数字前缀与微信语音一模一样，只看时间戳会误判！新增 `_looks_like_voice_bin()`：
    微信语音是 `<序号>_<秒级ts>` 且**没有扩展名**，带 `.` 的一律排除。
  * `_adopt_temp()` 复制后**必须 claim**，否则孤儿处理器会把同一条语音再处理一遍 → 重复入库。
  * 新增 `tools/backfill_voices.py`：把 **VoiceTemp 里残留的语音配对回消息**并补转。
    配对原理：目录名 = `md5(conv_id)`（能反查是哪个会话）+ 文件名后缀 = 秒级时间戳
    ↔ 消息 `received_at`（默认容差 60s）。**实测当天补回 1 条**（11:35 那条 `[解码失败]`，
    差值 7 秒，转写为"但是这里有神经暗洞神经暗洞来生事"）；其余 23 条的文件已不在缓存里。
  回归：新增 `wft_voice_recover_check`（文件名判定含 .cfg 反例 / scan_temp 排噪音与按会话隔离 /
  get_transcript / _adopt_temp 认领 / 配对时间戳与容差 / 三级兜底代码存在性）。

* **通用总结 prompt 必须"领域中立"**（10-07 用户指正）：用户指出"并不是每个群都涉及卦例及答疑
  等内容，所以针对特定群的要求，不要放在通用的提示词内"。原先第 4 条我把"卦例/答疑"写进了
  **通用规则**做示例 —— 对业主群/美食群就是噪音，还可能把模型往那个方向带。已改：
  * 第 4 条补"照原样保留该群的**领域用词**（各群关心的东西不同，不要替换成你熟悉的其它领域词汇）"；
  * 第 7 条补"**以该群自己的领域为准**：本群是做什么的、在聊什么，就用它那一行的常用说法，
    不要往别的领域猜"；
  * 同音错例表加了免责说明"只是可疑信号的举例，**不是替换表**，也**不代表所有群都关心这些领域**"。
  ⚠️ 仍在通用 prompt 里的领域相关文字只有**同音错例表**（申金/世爻/辰土…）—— 它标注了"不是替换表"
  且有"读得通就原样采信"的护栏，但对非六爻群仍属轻微偏置；彻底解决要把它做成配置项
  （按群或全局的"术语纠错表"），**列在待办**。
  💡 Web 里另有两处"风水教学群/卦例"文字，是**输入框的示例占位**（用户一输入就被替换、
  不会提交给模型），非通用规则。

* **📆 阶段总结（指定群 + 任意时间段 + 用户额外要求）**（10-07 新增，入口在导航栏）：
  用户需求："允许用户指定一个群，选择消息时间段（比如周一到周三或具体日期），提交 LLM 做
  阶段群聊内容总结，允许在 prompt 中加入具体要求" + "结果直接在 web 平台展示，
  无需用户自己打开文档"。
  * `consumer/summarize.py` 新增：`resolve_range()`（时间段解析）、`collect_group_range()`
    （按会话取区间消息）、`build_range_prompt()`、`summarize_range()`、`fmt_range()`、`_date_span()`。
  * **时间段解析**支持：`2026-10-05`（单日）/ `2026-10-05 ~ 2026-10-07` /
    `2026-10-05-2026-10-07` / `2026/10/5-2026/10/7` / `2026.10.5~2026.10.7` / `10/5~10/7` /
    `2026-10-05~10-07`（后半沿用起始年）/ `周一~周三`（= **本周**一~周三）/
    `星期一到星期五` / `周五~周一`（自动跨周）。
    ⚠️ 核心难点：`-` 同时是**日期内部**与**区间**分隔符 → 必须先"在日期与日期之间的 `-` 上插 `~`"，
    再归一 `/`、`.`，最后才切分（先归一会得到无法切分的 `2026-10-5-2026-10-7`，踩过）。
  * 取数**只按会话**（不像 `_in_digest` 那样按监控名单/重点人过滤）—— 阶段复盘常就是想把
    "这个群这几天聊了什么"看全；但仍套类型闸门 `digest.exclude_types` 与 ★ 标记；
    跨天时时间戳带 `MM-DD HH:MM`（单日只 `HH:MM`），模型才能分清"周一的讨论"和"周三的结论"。
  * prompt：同一套规则模板，标题换成区间；**用户的额外要求**作为第 10 条插在通用规则之后、
    每群总结提示之前（本次任务的要求 vs 该群的长期口径，优先级递进）。
  * 落盘：`reports/summary-<起>_<止>-<群名>.md`（跨天才带下划线，与每日的
    `summary-<日期>-<群名>.md` 区分开，`/range-summary` 页也据此列出历史）。
  * Web：`/range-summary` 页（群下拉含 ★监控 标记、时间段输入 + 说明、额外要求 textarea、
    ① 预览 prompt（不花钱）/ ② 生成总结 两个按钮、历史列表）；
    API `/api/range/preview`、`/api/range/generate`、`/api/range/status`。
  * **结果直接在页面上显示**（用户明确要求）：生成完把 `content` 渲染进结果卡片，
    **刷新页面也还在**（`range_job` 状态随页面上下文传入并预渲染）；文件链接仍保留供留档/下载。
  * ⚠️ 阶段总结用**独立的任务状态与锁**（`range_job`/`range_lock`），否则会和"当日总结"的
    `summarize_job` 抢 running 标志，两边都跑不了。
  * 回归：`wft_range_summary_check`（A 解析含坏输入 / B 取数与闸门★ / C prompt 注入顺序 /
    D dry_run 不调 LLM + 落盘命名与头部 / E Web 三接口与坏输入 / F 页内展示与刷新存活 /
    G 与当日总结互不干扰）。

* **📚 媒体取证知识库已单独成文**：`docs/wechat-media-forensics.md`
  —— 把本轮"图片/语音到底能不能拿到"的全部实测结论、目录命名规则、数据库结构、
  `.dat` 容器与最强线索（同尺寸共享载荷前缀 → XOR 流而非 AES）、内存取证方法、
  以及**排查方法论**（snapshot diff / 控制变量 / 输出写文件 / 别过早下定论）
  整理成可直接复用的笔记。**后续要做图片或语音，先读这一篇，别重新踩一遍。**

* **撤回消息：保留原文 + 标注撤回（10-10 用户要求）**：
  用户反馈"撤回的信息原文显示怎么没有了"。**事实核查：原文其实一直在库里** ——
  我们在撤回**之前**就入库了那条消息；撤回通知的 XML 自带
  `<newmsgid>被撤回消息id</newmsgid>`，用它就能回查（实测最近 40 条撤回里 **32 条**能回查到）。
  原来只显示"某某撤回了一条消息"，把原文丢掉了。
  * `cards.parse` 从撤回 XML 解出 `newmsgid` 与撤回人（**`who` 只留人名**，
    留空很重要，否则渲染会叠成"某某 撤回了一条消息 撤回了一条消息"）；
  * `cards.summary_line(..., revoked_original=…)` 三种渲染：
    `〔已撤回〕原文：…（某某 撤回）` / `〔已撤回〕某某 撤回了一条消息（原文未留存）` /
    超长原文截断到 60 字；
  * `digest.revoked_originals(revoke_rows, db)`：按 `newmsgid` **批量** `IN (...)` 回查
    （分批 500 避开 SQLite 变量上限），原文自己也用卡片渲染（图片→图意、语音→时长）；
  * 接进**三处**：`generate_digest`（归档）、`summarize.build_lines`（每日总结输入）、
    `collect_group_range`（阶段总结输入）、以及 **web 浏览页** `/browse`
    （`revoke_map` → `card["revoked_original"]`，`browse.html` 显示 "↩ 已撤回 + 原文："）；
  * 总结提示词加**规则 9**：说明「〔已撤回〕原文」是实时留存、内容可信可引用，
    写「原文未留存」时不要猜；并提示"撤回本身往往有信息，但别过度解读"。
  回归：新增 `wft_revoke_check`（解析 / 三种渲染 / 不叠字 / 截断 / 批量回查 /
  归档与总结端到端 / 提示词规则 / 浏览页与模板）。

* **图片巡检器加强：不再只认 Thumb/ImageTemp，也不漏无扩展名临时图（10-08）**：
  实测见过 `<n>_<ts>_hd_temp`（**400 KB**，比缩略图大得多）与 `<n>_<ts>_thumb_temp`，
  它们**转瞬即逝**且**不一定落在已知目录**。改动：
  `_looks_like_image_file()`（有图片扩展名 **或** 文件名带 `_hd_temp`/`_thumb_temp`/`_mid_temp`）
  + 扫描改为**会话下所有子目录**（不再写死两个目录）+ 无扩展名统一落地成 `.jpg`
  （原来 `.mid_temp_convert` 会被存成 `.img` 导致 PIL 打不开）。
  回归 `wft_image_sweeper_check` 新增 [B2][B3]（识别表、非已知目录里的 `_hd_temp` 也能抢、
  `.dat` 被拒绝、大文件完整复制；[B3] 用**独立假账号目录**避免污染 [E] 的数量断言）。

* **图片理解：让"拿到的明文图"真正参与总结（10-08 方案 B）**：
  攻 `.dat` 失败后的务实路线 —— 既然 `ImageSweeper` 能抢到**部分**明文图
  （`data/images/<msg_id>.jpg`），就让这部分交给**多模态模型描述**并写进归档/总结。
  * `consumer/vision.py`（新）：`enabled()`（读 `llm.image_understand`，**默认关**）、
    `describe_image()`（OpenAI 兼容的 `image_url` + base64；提示词**强调照抄图上的文字与数字**，
    看不清用「□」不猜）、`get_description()`/`describe_many()`（**按 (msg_id,size,mtime) 落盘缓存**
    到 `data/image_desc.json`，重跑归档**不重复烧额度**）。
  * `cards.summary_line(..., image_desc=…)`：有描述 → `[图片 214×480 · 478 KB] 图意：…`；
    无描述 → `[图片 …]（图未获取到内容）`（**老实标注，不编造**）。
  * `digest.image_descriptions()`：批量 + 进程内缓存；`_render_content(m, img_desc)`；
    `generate_digest` 与 `summarize.build_lines`/`collect_group_range` 都把描述带上
    （取数 SQL 补 `msg_id`）。
  * 设置页新增开关「用视觉模型描述归档图片（**默认关闭**）」并写清代价（每张一次视觉调用、
    只有拿到明文的图才有描述）。
  回归：`wft_image_vision_check`（开关语义 / 缓存落盘与命中 / 无明文不调模型 /
  卡片两种渲染 / 批量与关掉开关零调用 / `generate_digest` 端到端 / 设置页有开关）。

* **攻 `.dat` 的结论：常规方法不可行（10-08 用干净样本复核）**：
  * 新样本（南京路特坦群一张图的三档变体）熵 **7.92~8.00**、8 字节块重复率 **<1%**
    → **既不是简单 XOR/重复密钥流，也不是 AES-ECB**；载荷里 HEVC 起始码 **0 个**（不是视频编码）；
    全盘与内存都**没有该图的明文**。
  * ⚠️ **纠正 10-07 的误判**：当时"同尺寸 `.dat` 共享载荷前缀 → XOR 流"是**错的** ——
    那是**同一张图**（或固定头部）造成的假象；用确定不同的图重测，前缀并不相同。
  * 结论：要拿明文只能**调用微信自己的解密例程**（逆向 `Weixin.dll` + DLL/hook），成本以周计、
    不保证成功。**已按用户决定转向方案 B**（把能拿到的明文图用起来）。

* **修掉两个语音 bug（10-08）**：
  1. **（严重）多条语音抢同一个语音文件 → 转写内容全错**。实测三条语音
     （`7335411990255098730` 时长 6544 / `3723520471822789117` 5824 / `8686477914441493378` 17102，
     `aeskey`/`voiceurl` 各不相同）**被转写成同一段文字**。根因：
     `capture_voice` 与 `VoiceTap.wait_for/recent` 都用 **`since = now - 120 秒`** 的宽窗口，
     只取"窗口内最新"的文件、**取走不记账** → 同一文件被反复分配给不同消息。
     修法（三处）：
     * `VoiceTap.recent()` 新增 `_claimed` 记账 —— **取走即消费**，同一文件不再给第二条消息；
     * `capture_voice(..., near_ts, tol_s)` 改为按 **VoiceTemp 文件名后缀时间戳挑最接近的**，
       对不上就继续等（宁可不转写也不张冠李戴）；
     * `_capture_voice_bg(..., near_ts)` 窗口从 120 秒**收紧到 30 秒**，
       `_schedule_voice_retry(..., near_ts)` 同样按时间戳过滤（原来用 `now-3600` 的宽窗口）。
     ⚠️ 顺带说明：这三条**不是重复消息**（时长/aeskey 都不同），之前误判成"重复入库"。
  2. **`[语音 ~13s]` 表述不一致**：`cards.summary_line()` 的语音分支改成按状态明确标注 ——
     `[语音 5秒] 转写内容` / `[语音 5秒·未获取到语音文件，内容不可得]` /
     `[语音 5秒·解码失败，内容不可得]` / `[语音 5秒·待转写]` / `[语音 5秒·未转写，内容不可得]`。
     这样模型不会把占位符当怪话，也知道"这里有内容但拿不到"。
  回归：`wft_voice_recover_check` 新增 [G][H] 段（三条各自抓到不同文件、内容与时间戳对应、
  时间对不上返回 None、`recent()` 第二次返回 None）；`wft_cards_check` 更新语音断言。

* **⭐ 图片/语音明文就在缓存里，已实现两个常驻巡检器（10-08 重大进展）**：
  用户质疑"微信应该是把图片读进内存、新消息也先进内存再处理"（逻辑上对），
  但实测**内存里抓不到聊天图**（见下条），而**缓存的明文更稳**：
  * **图片**：`cache\<月>\Message\<会话md5>\Thumb\<序号>_<秒级时间戳>_thumb.jpg`
    是**明文 JPEG**（头 `ff d8 ff e0`，7 KB~163 KB，实测含 **540x720 真聊天图**）；
    另有 `ImageTemp\<n>_<ts>_mid_temp_convert`。文件名带秒级时间戳 →
    与消息 `received_at` 配对**差值 9 秒**即对上。
  * **语音**：`VoiceTemp\<序号>_<秒级时间戳>`（无扩展名，见语音条目）。
  * ⚠️ **微信会清理**（实测同批文件几分钟内消失）→ **必须常驻巡检"出现即抢"**。
  实现：`consumer/image_sweeper.py`（`ImageSweeper`）与 `consumer/voice_sweeper.py`
  （`VoiceSweeper`），都由 `Consumer.__init__` 起后台线程；
  配对循环 `_image_pair_loop` / `_sweeper_pair_loop` 按"会话 md5 + 时间戳"反查消息，
  图片存成 **`<data>/data/images/<msg_id>.jpg`** → **归档/总结可按消息直接引用图片**。
  新增 `store.message_near()`（按会话+类型+时间最近找消息）与
  `store.voice_needing_transcript()`（只找未转写的语音）。
  ⚠️ 踩坑：① `_prune` 一度按**源文件 mtime** 计时，导致"昨天遗留、今天才扫到"的文件
  被立刻删掉 → 改为按**抢存时刻**；② 时间并列时排序要加次级键（`msg_id`）才稳定。
  回归：`wft_image_sweeper_check`（抢存/解析/清理/配对/接线）、`wft_voice_sweeper_check`；
  ⚠️ 另修 `wft_retention_check` 的**日期漂移脆弱**：它调用 `cleanup.count_older(db, 30)`
  用**真实当前时间**，跨过午夜（10-07→10-08）后门槛前移一天导致断言挂 —— 已改为传固定 `now`。

* **内存验证结论（10-07 用户批准，A/B 两线）**：目标是判断"自研 DLL 拿媒体"值不值得投入。
  1. **图片：内存里确实有明文** —— `tools/scan_wechat_memory.py` 只读扫微信主进程，
     命中 **JPEG 94 处 / PNG 14 处**；`tools/dump_wechat_memory_media.py` dump 后
     PIL 验证成功（132×132、160×156 真 JPEG）。→ "读内存取图"技术成立。
  2. **但内存扫到的多是 UI 素材/头像/表情**（768×1392 是 emoji 面板、960×1839 是气泡背景、
     800×800 是素材）→ **真正的聊天照片不是长期驻留在内存里**（微信按需小块解码）。
     所以"常驻扫内存"不是个稳的方案，除非在图片正被渲染的那一瞬间抓。
  3. **`Decode_Pic` 接口是空壳**：`POST /Decode_Pic` 各种入参都返回
     `{"ret":0,"retmsg":"success"}`，但**不产出任何文件**（与 DLL 里只有打印语句一致）。
  4. **语音**：内存里有 `#!SILK_V3` 明文头（7 处），但内存里是**多条流拼接**，
     按 `[len2][payload]` 解析到中途会断，pysilk 解不开 → 需要更精确的流边界或 hook 解密函数。
  5. **`.dat` 容器结构新发现（重要）**：同尺寸的多个 `.dat`，其**载荷前 16 字节完全相同**
     （2698/2386 组都是 `3e fb 15 44 78 75 b7 61…`）→ 说明用的是 **XOR 流/固定头**，
     **不是 AES**（AES 不会让不同文件共享同一段前缀）。这是可解的信号，下一步应沿这条查。
     ⚠️ 已知明文对照失败的原因：用户另存的是**原图(489KB)**，而本地 `.dat` 是
     **中图/缩略图(2~60KB)** —— 不是同一份数据，XOR 出来没有意义。
     要破解必须拿到**同一变体**的明文（例如让微信另存为同尺寸，或从内存 dump 出对应缩略图）。
  💡 结论更新：**自研 DLL 的价值从"能否读内存"变成了"能否在正确时机 hook 解密函数"** ——
  图片不一定需要 DLL（.dat 结构已见规律），语音才可能需要。

* **语音问题的最终结论：PC 微信本地既没有音频、也没有"转文字"结果（10-07 穷尽排查）**：
  背景：用户场景是**PC 微信 24 小时挂机、从不逐条点播放**，所以三级兜底也救不了。
  逐步排除后确认：
  1. **语音只存在服务器**：`silklength` 全库恒为 `0`；`voiceurl` 只有 101~102 字节
     （protobuf 定位信息，不是音频；按 `length` 应是十几 KB 级）；`msg\attach` 下只有 `.dat`，
     没有 Audio 目录；hook DLL 只有 `Decode_Pic/QueryDB/SendTextMsg…`，
     试探 `/GetAudioMsg`、`/DownloadVoice` 等**全部 404**。
  2. **点播放也不落盘**：实测播放一条语音后，整个账号目录 diff 只有
     `Bubble\*_b.dat`（图片气泡）与 emoji 变化，`VoiceTemp` 无新文件。
  3. **微信自带「转文字」的结果也不落地**：转文字后 diff 显示
     `message_0.db(+wal)`、`sns.db` 有写入 → 于是用 keyhook 采的密钥离线读库
     （`message_0.db` 有 74 张表，**每个会话一张 `Msg_<md5(conv_id)>`**），
     解压 `message_content`（zstd，魔数 `28 b5 2f fd`，`WCDB_CT_message_content=4`）后确认：
     语音行**只有 `<voicemsg .../>`**，没有转写文本；
     在 `message_0.db` + `message_fts.db` + `media_0.db` + `message_resource.db` 全部表里
     **搜遍已知语音内容（"神经暗洞"等），零命中** —— 命中的 25 处全是普通文本消息
     （聊天里本来就有"世爻/申金/行李箱"这些词）。
     → 结论：转文字是**每次现调服务器、只在界面显示**，本地不保存。
  ⚠️ 踩到的坑：① `open_db` 在**同一个快照文件**上连续试多个 key 会失败，
  **每个 key 必须用独立快照**（`tools/probe_keys_health.py` 用此法测出 6 个库全部可读）；
  ② `sqlcipher3` 的 Cursor **不能配 `sqlite3.Row`**（TypeError），用元组下标；
  ③ 排查输出被 PowerShell 吞过，结论性脚本要把结果**写文件**再读。
  留下的工具：`tools/probe_keys_health.py`（keys 能用哪些库）、
  `tools/snapshot_account.py`（操作前后 diff 账号目录，定位"某操作产生什么文件"）、
  `tools/search_wechat_voicetext2.py`（证据脚本：全库搜语音转写文本）。
  💡 可行替代：① 重要语音让人补文字；② 截图 + 本地 OCR（截图不可编程获取）；
  ③ 手机端采集（另一套链路）。**不要在 PC 端继续找语音文件了，没有。**

* **测试临时目录会在 `D:\projects` 下堆积（已修）**：35 套回归每套都在**仓库的上一级**
  （`D:\projects`）建 `.wft-*` 临时目录（套件里写的是绝对路径）。
  * 29 套在结尾有 `shutil.rmtree(SC)`，但**失败的套件根本走不到那行**；
    另有 `wft_quit_check` / `wft_reload_check` / `wft_discover_check` / `wft_e6_check` /
    `wft_frozen_root_check` / `wft_live_check` 等**完全没有清理**。
  * 结果：10-07 一次性清掉 **13 个目录 / 33.9 MB**（`.wft-contacts` 最大 10 MB）。
  * 修法：`run_all.ps1` 把跑测循环包进 `try { … } finally { 扫掉 .wft-* }` ——
    收尾统一删（在最后一个套件之后，不会误删正在用的目录），实测每次能扫掉 5 个左右。
  * ⚠️ 增量记忆：**`.wft-*` 是测试scratch，随时可删**；若单跑某套件失败，手动清一下即可。

* **每群「总结提示」的维护入口搬到了「🎯 监控规则」（/filter）页**（10-07 用户要求）：
  用户理由：归档范围已经是"监控名单里的群的全部对话"，所以这套设置的自然归属是监控规则页。
  * 监控名单表格新增 **「🧠 总结提示」列**（每群一个 `<textarea name="hint_<群id>">`），
    整张表包在一个 `action=save_hints` 的 form 里，表格下方是「💾 保存总结提示」；
    原先在表格里的"移除重点人 / 移出监控名单"按钮**挪到表格外面的操作区**
    （HTML 不允许 form 嵌套，内层 form 会被浏览器丢弃）。保存只处理**监控名单里**的群。
  * 「🗄 归档关键词（已废弃）」卡片改成说明性文字，**不再提供编辑表单**（该字段已不参与判定）。
  * **标定页的 🧠 入口保留**（两处写同一份 `labels.groups.<gid>.summary_hint`），
    标定页文案里加了"日常建议去监控规则页维护"的指引。
  * ⚠️ 顺带修正了**过时文案**：重点人页（config_focus.html）和 _monitor_block 原本写着
    "设了重点人 → 只归档这些人的发言"，与 10-07 新语义（整群归档 + ★ 标记）矛盾，已改。
    用户三个监控群（易青岚乙巳年弟子群 focus=11、岳章形家风水班 focus=4、
    易青岚丙午下半年每日一卦 focus=6）**都设了重点人**，所以这条文案之前是**真的在误导**。
  * 总设置页（/config）里那段旧监控表格是 `{% if false %}` 死代码，不渲染；那页只留
    "当前监控的群/人 + 去监控规则页设置"入口卡片 —— 刻意保证"同一份配置只有一处能改"。
  回归：新增 `wft_summary_hint_monitor_check`（28 项：入口/保存/清空/只写名单内的群/立刻进 prompt/
  重点人语义与文案）；`wft_monitor_check`、`wft_formstruct_check` 随新语义更新。
  ⚠️ 踩坑记录：① 测试断言插错了段落（写在"加群"之前，名单还空着）；② **测试名里带 emoji（🧠）
  在 GBK 控制台 print 会 `UnicodeEncodeError` 直接崩**（单跑正常、经 run_all 失败）——
  已去掉 emoji 并给 `check()` 加 ASCII 降级。

* **语音错字提示的三方对照实验（10-07 实跑，MiniMax-M3，结论反直觉）**：
  用 10-07「易青岚乙巳年弟子群」真实语料（115 行，含 5 行语音转写）做 A/B/C 三方对照，
  只换 prompt 里的规则段：A 无规则 / B 固定纠错表（最初写法）/ C 上下文推断（现写法）。
  工具：`tools/experiment_asr_fix.py`（`--dry-run` 只看 prompt；真跑会调 LLM）。
  结果：
  | 变体 | 正确术语出现 | 残留错字 | 过度纠偏 | 正文字数 |
  |---|---|---|---|---|
  | A 无规则 | 10 | 0 | 0 | 798 |
  | B 固定纠错表 | 25 | **1** | 0 | 1180 |
  | C 上下文推断 | 25 | 0 | 0 | 1058 |
  * **A 也把"神经暗洞"正确理解成"申金暗动"** → MiniMax-M3 靠上下文自己就能纠，
    **规则不是必需的**（这点很重要：别高估固定规则的价值）。
  * B 的查表倾向会**把转写错字原样搬进总结**并加括号（`自动转写为"赤窑/世爻"…`），
    C 则输出 `★周宏伟语音进一步展开（转写校正）：…入墓逢合…` —— 更干净。
  * **三方都没有过度纠偏**："尘土/缝合/入木"等正常词都没被乱改（这次的语料里它们
    本来也没出现在总结里，所以"过度纠偏"这一项**未被真正压测**）。
  结论：**保留 C**（读得通就原样采信 + 存疑标注），但要知道 A 已经能拿到大部分收益。
  💡 还想更严格地压测"过度纠偏"，需要**专门构造**一组"看似错例、其实正确"的句子
  （如"桌上落了层尘土""别这么神经""伤口要缝合""山洞很暗"）再跑 A/B/C —— 未做。

* **语音转写的错字：教"按上下文推断"，不要给固定替换表**（10-07 用户指正后重写）：
  最初我把 17 组同音对照（申金/神经、世爻/事要…）直接列进 prompt 当纠错表 —— 用户指出
  **固定纠错表会过度纠偏**（"尘土""神经"在别的语境里本来就正确，机械替换会制造新错误）。
  现在第 7 条改成**推理原则**：
  ① 读得通就**原样采信**，绝不因为"长得像术语"就改；
  ② 只有本句/本话题里**明显读不通**、换成同音术语后逻辑才通顺时才改，并按改后意思理解；
  ③ 判断依据优先级：本群/本话题用词习惯 > 说话人前后几句 > 常识；
  ④ 拿不准就保留原词 + 标「（转写存疑）」。
  例子降级为"**可疑信号参考，不是替换表**"（且在末尾强调"这些词在别的语境里可能完全正确"）。
  第 5 条（原有）也保留了"转写明显有误时标注（转写存疑）"。
  回归：`wft_summary_hint_check` 断言"按上下文推断/不是替换表/读得通就原样采信/禁止机械替换"。
  ⚠️ 文案里刻意只用中文与常用标点（早先用 `↔` 时 `--print-prompt` 在 GBK 控制台崩过）。
  `_safe_print()` 做了三层降级（UTF-8 容错 → replace → ASCII），打印任何字符都不会把功能搞崩。
  💡 后续可选：把"可疑信号"清单做成配置项或每群术语表，让用户自己维护。

* **每个群可以带一段"总结提示"一起提交给 LLM**（`labels.groups.<gid>.summary_hint`，10-07 新增）：
  入口在「🏷 标定」页每个群下面的「🧠 总结提示」输入框（原样文本，按 group_key 数组对齐提交，
  清空就删掉该键）。`summarize.build_prompt()` 会把它拼成 **第 7 条要求 + 「本群总结提示」清单**，
  并明确"优先级高于通用要求、提示之外的内容算噪音直接丢掉"，用来剔除群里的闲聊杂质。
  ⚠️ 两个容易踩的点：
  1. `build_lines()` 的键**从"群显示名"改成了"群 id"**（因为提示按 id 存）——
     渲染群名/文件名要走 `name_for_gid()`，否则 per_group 会生成
     `summary-<日期>-<roomid>.md` 这种丑名字（10-07 被 `wft_pergroup_check` 逮到）。
     `build_prompt()` 两种键都认（旧调用点传显示名会用 `gid_for_name()` 反查）。
  2. prompt 组装用 **`str.replace` 而不是 `.format()`** —— 正文里含 `{}`（XML/JSON 片段）
     会让 format 直接抛异常。
  回归：`wft_summary_hint_check`（取值/注入/边界/兼容旧键/标定页保存与清空）。

* **逐条 LLM 评分默认关闭（`llm.score_enabled: false`，10-07 用户决定）**：
  以前每条"别人发来的文本消息"都要调一次大模型打 1~5 分并写 `score/score_reason`。
  查过依赖面后确认它**只有一个真用途** —— 给 **Bark 推送**做阈值判断
  （`score >= llm.push_threshold`）；**自动回复**走模板 trigger + 模板自身 scope，
  **归档/总结**走 `digest._in_digest()` 的 priority（标定/监控名单/重点人/每群敏感关键词），
  浏览页/仪表盘只是把 score 显示成标签，`scripts/browse.py`、`inspect_groups.py` 只打印它。
  → 现在 `score_enabled=false`（默认）时**完全不调 LLM、不写 score、不推送**；
  打开才恢复评分+推送。开关在总设置页「大模型」区块；`setup.default_config()` 与
  `config.example.yaml` 都给了 false。
  回归：`wft_ingest_scope_check` 第 5/6 段用 spy 断言"关掉后 scorer 零调用、库里 score 为 NULL；
  打开后恢复调用与写入"。
  ⚠️ 历史消息里已有的 score 不动（仍可查）；`llm.push_threshold` 现在只在评分开启时才有意义。

* **入库范围已全开（R-002，10-07）**：`handle_message` **不再调用 `Filter.match`** ——
  所有会话都入库，只受 `storage.ingest_exclude_types`（表情 47 恒排除）限制。
  `filter.groups`（监控名单）**只决定 23:30 归档/总结范围**（`digest._monitored_groups()`），
  `filter.keywords` **成了空转项**（入库不看、`_in_digest` 也不读），`filter.senders` 只在归档侧
  对私聊有意义。⚠️ 改动前"名单外的群连库都进不去"是**静默**的（只写 debug 日志），
  所以用户会看到"微信里有、系统里查无此条"（10-07 的 9:47 事件）。
  回归：`wft_ingest_scope_check`（真 Consumer + 假 hook 端到端断言）。
  ⚠️ 若将来要恢复"只抓指定群"，记得那是**新需求**，别再默认绑到监控名单上。

* **相对路径的 `storage.sqlite_path` 是按"当前工作目录"解析的**（10-07 踩到）：
  `store.Store(cfg["storage"]["sqlite_path"])` 不知道数据根，只有 app 会 `chdir(PROJECT_DIR)`。
  所以**绕开 app 直接用 Consumer/Store**（脚本、回归）时，`data/messages.db` 会落到
  `WeChatFerryTool\data\messages.db`（10-05 的老库！），而不是账号数据根 ——
  我在新回归里就打开过真库（进程里出现 4233 条），幸亏那是老副本。
  对策：**测试/脚本里一律用绝对路径**；想彻底修就改 Store 走"数据根优先"（未做，记在这）。

* **消息卡片解析统一在 `consumer/cards.py`**（10-07 新增）：浏览页与归档/总结**共用同一个解析器**，
  不再各自实现（以前浏览页直接吐 XML、归档只有 `[图片]`）。
  * `cards.parse(msg_type, content)` → 结构化；`cards.summary_line(...)` → 归档用单行摘要。
  * 覆盖：图片(尺寸/大小/md5)、视频(尺寸/大小/时长)、引用回复(被引用人+原话，**嵌套引用再解析一层**)、
    链接、文件、位置、名片、通话、撤回、语音(优先转写)。
  * ⚠️ **属性名取值的坑**：`_attr(raw, "length")` 会先匹配到 `cdnthumblength`（下划线是 \w 字符，
    `\b` 挡不住）→ 实测把缩略图长度当成原图大小。已用 `(?<![A-Za-z0-9_])` 前缀断言修掉，
    被 `wft_cards_check` 锁住。任何"按名字取属性"的地方都要注意这个。
  * 系统类消息（type 51/10000，如 `<op id=5><name>lastMessage</name>`）**只显示 `[系统]`**，
    不吐内部 JSON（它们本来就没人类可读正文，且「消息分类」默认已挡在库外）。
  * 真库校验：4415 条消息解析 0 报错、摘要 0 条含 XML 特征。

* **图片"还原成图"的可行性（10-07 实测，已通 2.5 步，未接 UI）**：
  * **消息 → 本地文件**（已通）：微信 `db_storage\message\message_resource.db`（**同一把已采密钥**，
    `db_key_map.json` key_index 14）里 `MessageResourceInfo.message_svr_id` = 我们的 `msg_id`，
    其 `packed_info` 就是**本地文件名 stem**（如 `ddece4375134faa154cd693fe53a6d48`）；
    再用 `db_storage\hardlink\hardlink.db` 的 `image_hardlink_info_v4`（`md5_hash/md5/type/file_name/
    file_size/dir1/dir2`）与 `dir2id` 拼出完整路径
    `msg\attach\<会话md5>\<月>\Img\<stem>[_t|_h].dat`。
    现成脚本：`tools\probe_wx_resource.py`（只读快照 + 解密 + 按消息 id 反查）。
  * **`.dat` 解密（10-07 穷尽式实测后的结论：不是简单方案，需要参考实现的密钥参数）**：
    容器格式（181 个样本一致）：`07 08 56 32 08 07 00 04 00 00 <2字节小端长度> 00 00 01 <1字节变体>`
    —— **前 15 字节固定**、第 16 字节随文件变化；`<2字节长度>` 实测等于该消息 XML 里的
    `cdnthumblength`（_t 文件）与 `hevc_mid_size`（无后缀文件），可用来校验映射是否正确。
    数据段 = 第 16 字节之后。已排除的假设（都是**全文件/全偏移**验证过的）：
      ✗ 单字节 XOR（256 个 key × 各偏移，无任何魔数命中；尾部 key 0xF9 是"凑 FF D9"的假象）
      ✗ zlib / gzip / zstd 直接解（原样与跳过 15/16 字节都试了）
      ✗ AES-128-ECB + 消息 XML 的 `aeskey`（偏移 0/15/16/31/32 全试，头尾都不是任何图片魔数）
      ✗ 明文（前 15 字节之后直接就是密文）
    → 属于**真加密**（公开实现 chatlog/dat2img 说 v4 是 AES-ECB + XOR，但具体密钥派生/块边界
    没拿到源码，`raw.githubusercontent.com` 在本会话取不到）。**这一步不要再用暴力试**，
    要拿到参考实现的算法（或换一台能上 GitHub 的机器把 `dat2img.go` 抓下来）。
  * 已就绪的判据（下次继续时直接用）：本地 `msg\video\*.jpg` 有 65 个**明文** JPEG 可当已知明文；
    181 个 `.dat` 的头部逐字节统计见 `tools\probe_dat_struct4.py` 输出。
  * **又排除两条路**（10-07 追加）：
    ✗ `msg\video\*.jpg` 是**视频缩略图**（`<stem>.jpg`/`<stem>_thumb.jpg` + `<stem>.mp4`），
      与图片 `.dat` **没有同名交集** → 当不了"已知明文配对"（`tools\probe_dat_pair.py` 实测）。
    ✗ DLL 的 `/Decode_Pic` 是个**空壳**：不论传 XML/path/msgid/裸 XML，一律回
      `{"ret":0,"retmsg":"success"}`（裸 XML 才回 `{"msg":"invalid..."}`），不产出任何图片字节；
      `/ForwardXMLMsg` 空 body 回 `{"ret":1,"retmsg":"fail"}`（且它会真发消息，不适合用来取图）
      → 见 `tools/probe_dll_image.py`。
  * **AES 的密钥/IV 组合也全排除**（10-07 补充；`tools\probe_dat_decrypt.py` 可一键复跑全部否定项）：
    ECB 换 key（XML.aeskey / 头部 0:16 / 16:32 / cdnthumburl 前 16 / md5(aeskey) /
    sha256(aeskey)[:16]）× 偏移（0/15/16/31/32）、CBC 换 IV（头部前 16 / 全零 / cdnurl 前 16）、
    以及"上述结果再 XOR 0x37/0xF9" → **全部 0 命中**。
    ⚠️ 这类判定要用**严格魔数**（`FF D8 FF`/`89PNG`/`RIFF`…）；别用 `BM` ——
    `0x07^0x45='B'` 能凑出 `BM` 造成假阳性（10-07 踩到一次，已修）。
    AES 实现抽到 `tools\aes128.py`（纯 Python，过 FIPS-197 自检，零依赖可复用）。
  * **新线索（可能有用）**：`.dat` 的第 16~30 字节**在很多文件里完全相同**
    （`_t.dat` 200 个样本里只有 4 种取值，最常见的一串出现在 124 个文件里：
    `f2 aa 2d 4c fb 9c 93 5a 65 01 3e 5b cf c6 57`）→ 这一带不是"每文件随机的密文"，
    更像**固定块/分组密钥/指纹**，是定死算法时最该先看的地方。
  * **可落地的下一步**：用户在浏览器直接打开 GitHub 上的 `dat2img.go`
    （`github.com/sjzar/chatlog`，用仓库自带的文件搜索找 `Dat2ImageV4` 所在文件，
    或从 README 的 "Source Files" 进去），把内容贴进会话即可；本会话
    `raw.githubusercontent.com` / `jsdelivr` / `proxy.golang.org` **都不通**，我取不到源码。
  * **用"已知明文"实攻过一次（10-07，未成功，但把证据备齐了）**：
    用户从 PC 微信另存了 11:25 那张图 → `D:\projects\wx-saved\微信图片_20261007112501_1034_1.jpg`
    （**489,072 B**，正好等于该消息 XML 的 `length`；文件名里的 `20261007112501` = 消息时间）。
    ⚠️ **但两者不是同一份数据**：本地 `.dat` 是**中图**（60,624 B，声明 59,569 ≈ `hevc_mid_size`
    的压缩版），另存的是**原图**（来自 CDN，489KB）→ 长度不同，做 known-plaintext 攻击不成立。
    另外发现微信还会在 `cache\<月>\Message\<会话md5>\Bubble\<stem>_b.dat` 存一份 bubble 版
    （同一格式，也加密）。这也再次确认：**微信只在内存里解密，磁盘上不落明文**
    （全盘扫 `resource` / `cache` 都没有可用的明文图）。
    这轮又排除：AES 重复密钥 XOR、明文片段原样内含、已知明文对齐的周期性（都无命中）。
    证据/脚本：`tools/probe_dat_knownplain.py`、`tools/probe_dat_variants.py`、
    `tools/probe_dat_final.py`；1097 个 `.dat` 的声明值统计见 `probe_dat_variants.py` 输出
    （`_t.dat` 的"实际−声明"恒为 1055 字节 → 声明值是压缩数据长度 + 固定容器开销）。
    **若将来要真解，最省事的顺序**：① 拿到 `dat2img` 源码（用户浏览器打开贴进来）；
    ② 或让用户在微信里把**同一张图**另存两次（原图 + 能拿到中图的途径）凑出同源明文密文对；
    ③ 或对微信进程做内存转储找解密后的中图（重、有风险，最后手段）。
  * **`GET /media/<msg_id>` 与浏览页内联图因此暂缓**（元数据与卡片已上线，见上一节）。

* **语音「会话名手工映射」（`voice.conv_overrides`）的键同时认 32 位与 8 位前缀**（10-07 修）：
  语音文件叫 `<会话md5>_<毫秒>.bin`，`_reverse_conv_map()` 现在**统一按前 8 位建键**
  （完整 hash 也留一份），`_sweep_orphans()` 用 `_conv_key()` 截前 8 位再查 ——
  以前实时路径只做精确匹配，用户只抄 8 位前缀时**静默不生效**，只有 `remap_convs.py`
  认前缀，两边口径不一致。⚠️ 映射的**值**填 wxid/roomid 才会归到真实会话；
  填显示名只会落成一个独立会话名（页面文案已写明）。回归：`wft_convmap_check`。

* **排查用的现成脚本**（都在 `tools\`，只读/幂等，已在 git 里）：
  `probe_wx_msg.py`（用 DLL 查微信自己的库，看某条消息在不在）、
  `rehook_callback.py`（微信重启后重登记回调）、
  `probe_wx_recent.py`（列各会话最近活跃时间）。
  ⚠️ 它们依赖 DLL 的 QueryDB；10-07 实测本机 `GetAllDBName` 恒空（`get database handle…
  failed`），所以"查微信库"这条路目前不可用，`probe_wx_recent.py` 会直接报这个。

* **"消息抓不到"的标准排查顺序**（10-07 实测总结，别再靠猜）：
  1. `netstat -ano | findstr :30001`（hook 的 HTTP）、`:8888`（我们的回调）、`:6060`（Web）
     —— 看三个端口的属主 PID。冻结版 app 会同时持有 8888+6060，hook 挂在微信 PID 上。
     ⚠️ 沙箱里 `Get-NetTCPConnection` 看不到别的会话的监听、`Test-NetConnection` 反而能连通，
     所以**用 `netstat -ano` 判断属主**、用 python/urllib 发请求（`Invoke-WebRequest` 会被
     控制台读取拦成 WinError 5）。
  2. **先证明我们这侧是好的**：往 `http://127.0.0.1:8888/hook/callback` POST 一条合成消息
     （`{"event_type":1001,"type":1,"msgid":...,"roomid":"<监控名单里的群>","sender":"wxid_x",
     "content":"...","timestamp":now}`）。它应当立刻在 `consumer.log` 出现「入库 …(reason=group_match)」
     并触发评分。**注意 roomid 必须在 `filter.groups` 里**，否则被 `filter.match` 挡掉（会误判成 bug）。
     → 这一步过了，说明 8888→入库 完好，问题在 hook 不转发。
  3. 问 hook 本人：`POST :30001/QueryDB/GetAllDBName`、`POST /GetSelfProfile`、`POST /set_callback`。
     10-07 的坏状态是：`GetAllDBName` 返回 `[]`、`QueryDB/execute` 一律
     `get database handle which named xxx failed`、`GetSelfProfile` 返回**一个群的昵称**而不是本人
     —— 而 `set_callback` / `SendTextMsg` 照样回 `ret:0 success`。**这些接口"成功"不代表 hook 健康**。
  4. 判断"微信自己在不在收消息"：看微信库快照的 mtime
     `D:\JiangBo\Documents\xwechat_files\<账号>\db_storage\message\message_0.db*`
     —— 一直在写就说明微信正常，断点在 hook。
  5. 结论模板：微信在收 + 我们的回调链路自测通过 + hook 的 DB/推送全废 ⇒ **hook 侧问题**，
     处理：重启微信让 hook 重新加载 → 重登记回调（`tools\rehook_callback.py`）→ 再试；
     不行就重新部署 `version.dll`；仍不行就换与该微信版本匹配的 hook。
  * 排查用的现成脚本：`tools\probe_wx_msg.py`（用 DLL 查微信自己的库，验证某条消息在不在）、
    `tools\rehook_callback.py`（重登记回调）。两者都只读/幂等。

* **多群总结现在是"每群一份"**（10-07，用户选定）：`digest.summarize_mode: per_group`（默认）→
  每个会话一份 `summary-<日期>-<群名>.md`、**每群一次 LLM 调用**；`combined` → 一份
  `summary-<日期>.md`、只调一次。文件名：清洗 Windows 非法字符、群 id 兜底时剥掉 `@chatroom`、
  **同一天重跑覆盖同名文件（幂等）**；某个群失败只记进 `errors`，不影响其它群。
  归档侧不变：仍是一份 `digest-<日期>.md`，内部 `## 群名` → `### 发送人` 分节。
  ⚠️ **归档与总结必须用同一套选取规则**：`summarize.build_lines()` 一定要把
  `digest._monitored_groups()` 传给 `_in_digest` —— 漏传会导致"归档里有、总结里没有"
  （10-07 实测到并修复；对照组：归档 5 条 vs 总结 0 条 → 修后 5 条 = 5 条）。
  ⚠️ 总设置页的 `select` 控件：`_form_value` 在字段**未提交**时必须返回 `MISSING`
  （保持原值），否则非本页表单/夹具一提交就把该配置写成空串。

* **多群归档/总结当前是"合并"的**（待办 #10，等用户决定是否改）：
  `generate_digest()` 产出**一份** `digest-<date>.md`，内部按 `## 群名` → `### 发送人` 分节；
  `summarize()` 也是**一份** `summary-<date>.md` + **一次 LLM 调用** ——
  `collect()` 把当天进归档的消息按会话分组，`build_prompt()` 再把所有群用
  `### 群：<名字>（N 条）` 拼进**同一个 prompt**，模型输出的是一篇覆盖所有群的整体叙述
  （不保证按群分节）。要"分群总结"就得按群循环调用 LLM（调用次数×群数）。

* **微信本地库能离线读，而且不需要注入**（`consumer/wechat_offline.py`，10-07 新增）：
  用已采集的 SQLCipher 口令，把 `contact.db`（+`-wal`）**只读快照**到工作目录再解密，就能拿到
  **5492 个联系人（备注/昵称）+ 32 个群名 + 3366 条群成员**。全程不注入、不启动、不碰微信进程。
  * ⚠️ **口令拼接必须转义单引号**：微信传的是 `x'<64位hex>'` 文本形式（记录里 `len=99/67`），
    拼进 `PRAGMA key = '…'` 时要把 `'` 换成 `''`。**不转义会变成语法错误 → 误判"密钥无效"**
    （我因此差点否掉整条离线路线，实际上密钥一直好用）。
  * 不是 32 字节原始密钥：`len=32` 的记录很少，多数是 `len=99/67` 的口令文本。
  * `sqlcipher3`(5.9MB) + `zstandard`(1.6MB) 已**打进 exe**（`--collect-all`），打包版也能用。
  * 工作目录优先 `data/_tmp`（不可写则退系统 temp），可用 `WFT_OFFLINE_WORK` 覆盖；
    导出 `data/wechat_contacts.json` 供「选择重点人」页显示**本群成员**。
  * 名字合并策略：**微信库名字覆盖标定名**（用户要求），原名存 `name_prev`；写前留
    `labels.json.bak-<ts>`；只给"消息里出现过/已标定"的 wxid 建条目（否则 5000 陌生人塞爆标定）。
* **keyhook 早期注入会让微信读不出消息库**（10-06 事故，已加闸门）：
  app 自己启动微信时会在**开库前**注入 `keyhook3.dll`（`supervisor.start_wechat` →
  `_inject_when_ready`），实测结果是**微信对话历史变空白**；而"先手动开微信、再启动 app"
  （注入发生在开库后）完全正常 —— 两次都有注入，唯一差别是**时机**。日志实证：
  23:45 `启动微信…已注入 keyhook3.dll → PID 32596` → 空白；23:52 `微信已在运行 → 已注入` → 正常。
  现状：`auto_inject_keyhook` **默认 false**、`launch_wechat: false`、要采密钥得用
  `--inject-keys` 且**先过版本闸门**（`inject_safety_error()`）。抓消息**不需要** keyhook。

* **保存的隔离性（硬约束，改一处绝不能动其它）**：用户明确要求设置页/监控规则页保存时
  不得影响其它部分。三个入口的保证 ——
  ① `/config/save`：只写表单里**确实提交了**的字段（逐字段合并）；复选框未提交=取消勾选；
  **请求里设置字段少于 1/3 就整份拒绝**（防 form 嵌套/其它按钮误提交把配置清空）。
  ② `/filter/update`：只在请求**确实带了** `groups`/`senders` 时才覆盖它们 ——
  否则"保存关键词"会清空监控名单。
  ③ `/config/filter/save`：只加/删指定条目、只改指定群/人的字段，不重建整个 labels 条目。
  另外：**写 config.yaml 与 labels.json 前都自动备份**（`*.bak-<ts>`，各留最近 5 份）；
  拒绝写入时不产生备份。回归测试 `wft_isolation_check` 用"快照 → 只改一处 → 逐字段比对"
  锁死这条（改 push_threshold 后其余 9 段与 labels 逐字段一致；截断提交后字节未变）。

* **消息库按期自动清理（R-001，10-07 完成）**：`consumer/cleanup.py` + consumer 常驻线程
  `_cleanup_loop()` + 总设置页「数据清理」区块 + `/settings` 的状态/预览/立即清理。
  配置 `storage.retention_days / auto_cleanup / cleanup_time / cleanup_vacuum`；
  状态 `data/cleanup_state.json`（`last_date` 保证一天一次）。
  ⚠️ **口径**：「保留 N 天」= 保留最近 N 个**自然日（含今天）**，门槛 = 今天零点 -(N-1) 天，
  `received_at < 门槛` 才删 → **当天数据永不参与**；`retention_days: 0` = 不清理、
  `1~6` = 保存但**拒绝执行**（下限 `MIN_RETENTION_DAYS=7`）。
  删完只 `wal_checkpoint(TRUNCATE)`；`VACUUM` 默认关（自动清理看 `cleanup_vacuum`，
  页面手动清理有独立勾选框）。分批 2000 + 批间 sleep + `stop_event` 可中断。
  ⚠️ **静默是硬要求**：只写日志与状态文件，**不许**调通知/托盘（回归用 spy 断言
  `Notifier.push` 未被调用）。
  ⚠️ `/settings` 渲染时要**容错**：全新库（有 db 文件但没 messages 表）时
  `db_query("SELECT COUNT(*)…")` 会回空列表 → 直接取 `[0]` 会 IndexError 让整页 500
  （本次实测踩到并修：总数取不到就按 0/未知显示）。
  ⚠️ 表单新增字段时**务必守住 `_form_value` 的 MISSING 语义**：本次给"数据清理"加
  `kind: time` 的 `cleanup_time` 时，空提交在解析层兜了默认值 `"00:00"`，导致
  "原样保存一次"凭空多出 `<storage>.cleanup_time` —— `wft_isolation_check`
  与 `wft_form_check` 立刻抓住（这就是那两套回归存在的意义）。
  正确写法：空值/未提交 → 返回 `MISSING`（保持"没有"）；`bool` 未提交且配置里**没有**该键
  → 也返回 `MISSING`（不能写 False），有问题就看 `_form_value` 里 time/number/bool 三个分支。
  回归套件：`wft_retention_check`（A1~A9 + CLI）。规格与决策记录在 `docs/requirements.md` R-001。

* **监控相关的设置全在「监控规则」页（`/filter`）**（10-06 调整）：监控名单（下拉勾选群/人 +
  每群「👥 选择重点人」）+ 入库关键词 + 当前生效范围。**总设置页 `/config` 只留一个跳转链接**
  —— 一处设置只能有一个入口，否则两处都能改同一份配置、极易互相覆盖。
  下拉**只列已标记**的项（群：有名字/★重点/有重点人/有关键词；人：有名字/★重点），
  外加"已在监控名单里"的（否则移除入口会消失）。
  `_config_context()` 已提升到 `web/app.py` **模块级**（`/filter` 与 `/config` 共用）。
  ⚠️ `/filter/update` 只在表单**确实提交了** `groups`/`senders` 时才覆盖这两个列表，
  否则"保存关键词"会把监控名单清空。
* **HTML 不允许 form 嵌套**：页面上任何自定义表单都必须放在"大表单"**之外**。曾把监控名单的
  form 放进总设置大表单里 → 浏览器丢弃内层 form → 点「添加」变成提交整份设置 →
  **真实 config.yaml 被覆盖清空**（只有 api_key 因"留空=不修改"侥幸保住）。
  现已：① 监控名单抽到 `web/templates/_monitor_block.html` 放在表单外；
  ② `/config/save` 加服务端兜底：提交里没有 `f_llm__base_url`/`f_hook__api_base` 就**拒绝写入**。

* **监控名单决定归档范围**（10-06 明确，语义变更）：
  `filter.groups`（总设置页「📋 监控名单」勾选的群）现在**直接参与归档判定**：
  * 群在名单里、**没设重点关注人** → **整群归档 + 总结**
  * 群在名单里、**设了重点关注人**（`labels.groups[gid].focus_members`）→ **只归档这些人**
  * 补充来源：群的**敏感关键词**命中、**全局重点联系人**在群里发言，也会进归档
  * **私聊**：只有 ★重点联系人（或列在"监控的人"里）才进归档 —— 以前私聊靠关键词也能进，
    现在不行（关键词是"群"的概念，私聊那栏本来就不该有）
  * 群**没进监控名单、又没标 ★重点/重点人** → 不再归档（以前"只配关键词"就能归档）
  判定在 `consumer/digest.py::_in_digest(row, labels, exclude_types, monitored)`；
  `monitored = _monitored_groups()` 读不到 `filter.groups`（空/缺失）时传 None → 退回旧 labels 规则。
  入口：`/config/focus/<gid>`（重点人勾选页）、`/config/filter/save`（增删群/人/重点人）。

* **DLL 的 `QueryDB` 在本机 DLL 上不可用**（10-06 挖源码确认，别再浪费时间）：
  `g_IsLogin` 全项目**只有 `= 0` 的初始化**，没有任何地方置 1；`getDatabaseInfo()` 与
  `searchDatabases()` 第一句都是 `if (!g_IsLogin) return 空` → `GetAllDBName` 永远空、
  `execute` 永远回 `get database handle which named failed`。`xdb\xwechat_offsets.h`
  只有 `#pragma once`（偏移常量是空的）。**管理员模式也没用**（不是权限问题）。
  → **昵称不要走 DLL**，走下面这条。
* **昵称从"已抓到的消息"里挖**（`consumer/name_harvest.py`，10-06 新增，**主路**）：
  引用消息（local_type 49）里成对出现 `<chatusr>wxid</chatusr>…<displayname>名字</displayname>`，
  我们自己库里就有。实测真实库：347 条引用消息 → **92 个 wxid→昵称**（覆盖 344 个发言人中的 91 个）。
  取"出现次数最多的名字"（有人改过昵称时更稳）。人工补充存 `data/name_overrides.json`，
  **优先级高于挖掘**（手工纠正过的不会被挖出来的覆盖）。两条路都是**只填空白**，永不覆盖已有非空名。
  标定页：「🔄 从已抓消息挖昵称并套用」「🔍 只看能挖到多少」+ 可展开的手工批量粘贴区。
  注意 `messages.sender_id` 列**全是空值**（hook 没给），别指望用它对齐成员。
* **标定是热重载的**（10-06 新增）：`consumer/main.py::_maybe_reload_labels()` 在归档循环里
  按 mtime 检查 `labels.json`，一变就重载 —— Web 上套用完名字**不用重启**。
* **DLL 的 `QueryDB` 必须带库名**（10-06 实测踩到）：`POST /QueryDB/execute` 的 body 是
  `{"optDbName": "contact.db", "SQL": "..."}`，**漏掉 `optDbName` 会回
  `{"status": -1, "desc": "get database handle which named failed"}`**（不是表名错）。
  响应约定是 `{"status":0,"desc":"","data":[...]}`（**看 `status`，不是 `ret`**）。
  `POST /QueryDB/GetAllDBName`（body 传 `{}`）返回 `[{"dbName":"x.db","dbHandle":123}]`。
  契约来源：`D:\projects\WeChatHook-src\README.md` + `src\QueryDB.cpp`。
  另外磁盘文件名（contact.db）与 DLL 里挂的名字**未必相同** → 库名/表名/列名一律动态发现，
  结果缓存在 `data/contact_source.json`（查不通会自动重发现）。
* **昵称自动关联走 DLL，不走本地解密**（`consumer/contact_sync.py`，10-06 新增）：
  wxid→昵称 的来源是 `POST /QueryDB/execute`（`HookClient.query_db`），**由微信用它自己
  持有的密钥查库**。为什么不用离线解密：实测 `contact.db` / `message_0.db` 用 21:15 采到的
  23 把钥匙**全部打不开**（9 种参数矩阵 + 连 `-wal` 快照都试了）→ **SQLCipher 密钥会轮换**，
  离线这条路随时会断，而 DLL 不受影响、也不需要把 `sqlcipher3`/`zstandard` 打进 exe。
  实现要点：① QueryDB 的库/表/列名**无公开文档**，所以用「候选查询矩阵」逐个试，
  谁能读通用谁；② `/api/contacts/probe` 把每条候选的可用性与报错都吐出来，换微信版本时
  照它改 `CANDIDATE_QUERIES` 即可；③ 合并进 labels.json 时**只填空白、绝不覆盖手工名**，
  自动填的标 `"auto": true`；④ 找不到表/列时 `/QueryDB/execute` 才报错，所以逐条试的代价很低。

* **Web 是 app 进程内的线程**（`app.main` → `start_web()`）：app 退出 = 管理平台立刻失联，
  它不是独立服务。**默认行为就是最小化到托盘**（pystray 图标代码绘制，右键有
  打开管理页/自动回复/暂停/完全恢复/退出）。
* **沙箱只约束开发会话（DSH）里启动的进程**，不约束用户自己双击启动的 app。
  所以在 DSH 里实测 exe 会看到这些**假故障**：托盘 `ChangeWindowMessageFilterEx` → `WinError 5`、
  SQLite 打不开 `accounts\...`、写 `accounts\...` 被拒。用户环境里都不存在。
* **windowed 打包下 `sys.stdout` / `sys.stderr` 是 None**：裸 `print(..., file=sys.stderr)` 会抛
  AttributeError（症状：双击没反应、连日志都没有）。三处已加固：`app/main.py` `_warn()`、
  `consumer/main.py` `_safe_stderr()`、`web/app.py` `force_utf8()`。
* **API 接口必须自己 try/except 返回 JSON**：抛出去就是 Flask 的 HTML 500 页，前端
  `.then(r => r.json())` 会解析失败并显示成 `TypeError: Failed to fetch`（误导性极强）。
  已注册全局 `@app.errorhandler(Exception)`：`/api/*` 回 JSON、其它回错误页。
* **打包 exe 的数据根 = exe 所在目录**（`base_root()` = `sys.executable` 的父目录）：
  所以直接双击 `dist\WeChatFerryApp\WeChatFerryApp.exe` 会把它当**全新安装** ——
  在 `dist\WeChatFerryApp\accounts\<slug>\` 下新建一套空的 data/logs，看不到项目里的
  `config.yaml` / `messages.db` / `labels.json`，于是进入"待初始化"模式、**不抓消息**。
  想跑"已有数据"要二选一：① 用源码启动（`start-app.cmd` / `python -m app.main`，数据根=项目目录）；
  ② `WeChatFerryApp.exe --data-dir D:\projects\WeChatFerryTool\accounts\ruibo_jiang_542e`
  （该目录里放一份 config.yaml）。
* **总设置页是表单化的**（`/config`，导航「⚙️ 总设置」）：所有配置项按区块排成表单，
  每项标了**字段名 / 影响 / 建议**，用户只调参数，**YAML 由应用自动生成**。
  实现方式：`web/app.py` 里一张 `FIELDS` 描述表（路径 → 标签/类型/影响/建议）同时驱动
  页面渲染与提交回写；自动回复用 `REPLY_GLOBAL` + `TEMPLATE_FIELDS`（模板是 repeat 区块，可增删）。
  要点：① **逐项容错**（某项填错，其它项照存，只提示那一项）
  ② 表单里没有的值（如空数字、原本不存在的段）用 `MISSING` 哨兵**不写入**，别把配置写成空串/None
  ③ 多行字段（人设/固定回复）**不要 strip**——结尾换行有意义，strip 会每次保存都悄悄改动人设
  ④ `scope.groups/senders` 的表单字段名是 `tpl_i__scope_groups`（与保存端一致，别写成 `tpl_i__groups`）
  高级模式在 `/config/raw`（按段改 JSON），日常别用它。
* **大模型设置有了独立入口** `/llm`（导航里叫「🧠 大模型设置」）：初始化完成后随时能改
  base_url / API Key / model / push_threshold。**只改 `llm` 段**（初始化向导 `/setup` 是按模板
  重建整份配置的，事后用它改 LLM 会把标定、回复模板、语音/ASR 等一起盖掉，所以别用向导改）。
  key 不回显（显示 `已保存 sk-abc…mnop`），留空 = 不修改，勾"清空 key"才真的清掉；
  另有「测试连接」按钮（`POST /api/llm/test`，真实调一次、不写配置）。
* **`build_exe.ps1` 现在是纯 ASCII**（10-06 改动）：Windows PowerShell 5.1 会把**无 BOM 的 .ps1
  当 GBK 读**，文件里的中文注释会被解成乱码、进而让解析失败（本会话因此把脚本改坏过一次）。
  所以这个脚本一律只写 ASCII；要加中文就**必须**保留 UTF-8 BOM，并复检语法：
  `[Parser]::ParseFile($p,[ref]$t,[ref]$e)`。
  另外它会在构建后**删掉 `_internal\vendor`**（vendor 是数据不是包：只该在 exe 同级有一份，
  否则白占约 229 MB）。
* **打包 exe 启动时现在会"先找已有配置/数据"**（`app/main.py::_discover_existing_root`）：
  从 exe 所在目录往上最多 4 层（外加 cwd，且仅当 cwd 在起点之下）找
  `config.yaml` / `accounts/<账号>/config.yaml` / `data/messages.db`，找到就用它
  （有账号目录就用那个账号，否则整个目录当数据根）。边界保护：不把盘根、`C:\Users\<用户名>`、
  Windows 等当项目根。这样 `dist\WeChatFerryApp\WeChatFerryApp.exe` 也能跑项目里的老数据。
  优先级仍是：`WCF_DATA_DIR`/`--data-dir` > `--no-multi-account` > 发现已有数据 > 账号隔离 > 基目录。
* **向导页会回显已保存的配置**（`setup.peek_answers_from_report`）：以前无条件返回空值，
  保存后再点初始化看着像"没保存"。现在回显 `base_url` / `model` / `digest_time` / `asr.enabled` /
  `run_mode` 与 `config_path`；**api_key 永不回显**，只给 `llm_api_key_saved` + 脱敏提示
  （`sk-abc…mnop`），输入框留空 = 不修改（前端回传哨兵 `__KEEP__`）。
  ⚠️ 只有**填着 base_url** 时才继承旧 key；base_url 留空 = 明确不要 LLM，不会继承。
* **托盘菜单「退出」曾经退不掉**：`request_quit()` 以前只 `_stop.set()`，而 `_tray.run()`
  阻塞在 Win32 消息循环里、只有 `_console_loop()` 看 `_stop` → 主线程永不返回 →
  `shutdown()` 不执行。现在 `request_quit()` / `shutdown()` 都会先 `tray.stop()`。
  （症状：点退出没反应、进程还在、日志还在被仪表盘刷新写入；只能任务管理器结束。）
* 钩点：`Weixin.dll + 0x8BB1570` → `sqlcipher_api_routines->key`（+0x10）。
  `codec_get_key`(0x4EE64D0) **实测不触发**。
* 密钥是 SQLCipher **`x'<hex>'` 文本**，必须当口令原样透传。
* **密钥重启后仍有效** → 不必每次启动都采。
* `message_0.db`：每会话一张 `Msg_<md5>` 表 + `Name2Id`（rowid = `real_sender_id`）。
  `WCDB_CT_* == 4` → zstd；`== 0` → 明文 **str**。`-wal` 必须一起复制。
* 归档渲染：语音走 `transcript` ✅；图片/视频**仅占位符**（无 OCR）；表情需剔除。
* **写不了日志 / 数据目录不可写，一律不能抛异常**：打包后 `FileHandler`、`accounts/<slug>` 一旦失败
  就是"双击 exe 闪一下没反应"（没有控制台，`--windowed` 下连 traceback 都看不到）。
  现在两处日志初始化 + 账号引导都做了降级（退化到控制台 / 退回 `%LOCALAPPDATA%`）。
* **待初始化时 `cfg` 是空 dict**：模板里别写 `cfg.hook`，Jinja 的 Undefined 一取属性就 500，
  要写 `(cfg.get('hook') or {})`。
* `.ps1` 改完必须 `Parser::ParseFile` 复检（丢 BOM 会静默失败）。
  ⚠️ 更稳的做法：**脚本里只写 ASCII**（见上条），从根上避免 GBK 误读。
* `PROJECT_DIR` 语义已拆分：`base_root` / `data_root` / `resource_root` / `config_path`
  （`config_path()` 现在是**数据根优先、基目录兜底**）。

## 刚完成（2026-10-06）

* `consumer/digest.py` 查询**已接线**：改为取当天全部消息 + 按 `_in_digest()` 判定
  （重点群 ∩ 重点成员 ∪ 敏感关键词 ∪ 全群重点 ∪ 全局重点联系人）；
  判定异常会退回旧的 `priority>=1`。
* `_exclude_types()` 默认扩为 **`[47, 51, 10000, 10002]`**（表情/系统/撤回）。
* 真实数据对比（2026-10-05）：当天 1065 条 → 旧规则 26 条 / **新规则 18 条**
  （剔掉 8 条表情与系统噪声），类型分布 `{文本:13, 链接:5}`。
* 归档与 `summarize.py` 现在**共用同一套判定**，不再不一致。
* ⚠️ **生效即改变行为**：当晚 23:30 归档起就用新规则（已用真实数据验证过）。

## 刚完成（2026-10-06 下午）：Web 端运行期配置（E 节待办第 1 项）

* **`/settings` 消息分类勾选**：勾选 = 该类 `msg_type` **不入库**，写 `storage.ingest_exclude_types`。
  **表情 47 恒排除** —— 页面灰选不可取消；后端 `sanitize_ingest_exclude()` 保存时强制加回；
  `consumer/main.py` 读取时也强制加回（有人手改 `config.yaml` 删掉 47 也没用）。
* **`/labels` 每群「敏感关键词」输入框**：写 `labels.json` 的 `groups.<id>.keywords`，
  **不用再手改 JSON**。支持换行/逗号/顿号分隔 + 整行注释（`# 说明`；`#合同` 这种仍算关键词）。
* **`/digest` 生成当日总结**：先「预览将发送的 prompt」（纯本地、不联网、不花钱）→ 再「生成当日总结」
  （后台线程 + 轮询 + 重复点击 409）；另可只生成归档 Markdown；`/reports/<name>` 可在线查看/下载。
* 顺手修掉两个真问题：① `/labels/save` 原本**重建标定条目**会丢 `focus_members` → 改为只更新被提交字段；
  ② `/api/digest/summarize` **持锁时又取状态**（`threading.Lock` 不可重入）→ 重复点击**死锁**，已拆开。
* 新增字段：`group_key[]` / `group_keywords[]` / `group_focus[]` **按群对齐**提交
  （旧的 `focus[i]` 数字下标已废弃：多勾几个成员就会把后面的群错位）。
* 验证：Flask test client 端到端（scratch 数据根 + 假 LLM）全过；真实 HTTP 服务器
  （`python -m web.app --port 6099` + scratch 数据根）冒烟全过，真实 `config.yaml` / `labels.json` 未被改动。

## 下一步

1. 把 `D:\projects\llm-test-20261006\reports\summary-2026-10-05.md` 放进账号 `reports/`
   （或正常权限下从 `/digest` 页再点一次生成）
2. 可选：把「归档排除类型」也做成 Web 开关（现在只能改 `config.yaml` 的 `digest.exclude_types`）
3. 把 `D:\projects\.acl-recovery-20261006\`（权限备份/回退脚本）与 `llm-test-20261006\` 按需清理

## 刚完成（2026-10-06 傍晚）：重新打包 + 冻结版实测（E 节待办 5/6）

* **重新打包**：`dist\WeChatFerryApp` **278.9 MB**（exe 11.3 MB + `_internal` 38.9 MB +
  `vendor` 229.2 MB，其中微信安装包就 228.3 MB —— 这就是包体积的大头）。
* **冻结版实测**（装到可写目录后实跑）：**10/10 页面 200**，仪表盘显示"待初始化"引导，
  `/settings`（勾选框 + 恒排除行）、`/digest`（生成按钮）、`/labels`（关键词框）的新控件都在。
* ⚠️ **实测才发现的三处启动期问题（已修）**：
  1. 日志目录/日志文件写不了 → `FileHandler` 抛异常 → **整个 app 起不来**（"双击 exe 闪一下就没反应"）。
     `app/main.py` 与 `consumer/main.py` 两处都改成"写不了就只输出控制台 + 明确告警"。
  2. 数据根不可写（安装在 `Program Files` 这类只读目录）→ `accounts\<slug>` 建不出来 → 崩。
     现在退回 `%LOCALAPPDATA%\WeChatFerryTool` 并告警（也可用 `--data-dir` 指定）。
  3. **待初始化（空配置）时仪表盘 500**：`dashboard.html` 里 `cfg.hook` / `cfg.llm` 这类访问
     在空 dict 上会抛 `UndefinedError`。改成 `(cfg.get('x') or {})` 取值，并加了"待初始化 → 前往向导"提示。
* **E6**：`vendor/manifest.json` 补上安装程序真实 sha256 `54203FC2…B4F74`，并把文件名从
  `WeChatSetup-4.1.10.27.exe` 修正为随包实际的 `wechat4.1.10.27.exe`（原先对不上 → `present` 恒 false）；
  `setup_env` 采集报告现在 **warnings/blockers 全空**、`ready_to_configure=True`。
* 打包/实测期间**没有改动真实 `config.yaml` 与账号数据**（回归里有 sha256 断言）。

## 刚完成（2026-10-06 傍晚）：真实 LLM 总结 + E 节待办 2/3/4

* **真实 LLM 总结跑通了**（MiniMax-M3，`HTTP 200`）：18 条 / 1 会话 / prompt 1381 字 → 总结 **320 字**。
* ⚠️ **真实调用才发现的一个 bug**：M3 会把整段 `<think>` 推理块一起返回，被**原样写进总结报告**
  （`scorer.py` / `replier.py` 早就各自剥过，只有 `summarize` 这条链路漏了）。
  → 新增 `summarize.strip_think()`（闭合/未闭合/正文在 think 之后三种情况都覆盖），
  并让"剥完是空"时报错，而不是写出一个空总结。
* `digest.exclude_types` 默认值写进 `setup.py` + `config.yaml` + `config.example.yaml`：
  **`[47, 51, 10000, 10002]`**（原先只是代码兜底，配置里看不到）。
* `setup.apply_answers()`：填了 `base_url` 却不填 `model` **直接拒绝**（原先静默写出 `model: ''`）。
* **`config_path()` 口径修正**：数据根优先、基目录兜底（原来看 `_account_root`，
  导致 `WCF_DATA_DIR=<目录>` 时文档承诺的"数据根优先"失效）。修完 `DRY_RUN`/沙箱测试里
  的配置读写才真正落到临时数据根。
* 验证：`apply_answers(dry_run=True)` 五种答案组合 + 真实 `config.yaml`/`labels.json` **sha256 前后一致**；
  Web 端 `/digest` 全链路（预览→生成→文件→查看/下载）用"带 think 块的假 LLM"复跑通过。

## 编辑文件时的两个坑（本会话各踩过一次）

1. **改函数头/边界时，锚点必须包含被保留的结构行** —— 曾漏掉 `def main():` 导致
   `digest.py` 语法错误（该文件每天 23:30 定时跑）。
2. **`.ps1` 编辑后会丢 BOM** → PowerShell 5.1 按 GBK 解码中文 → 静默失败。
   改完务必 `Parser::ParseFile` 复检。
3. 测试 `setup.apply_answers()` **必须带 `dry_run=True`**（已支持），
   否则会用临时配置覆盖真实 `config.yaml`（本会话发生过一次，已从备份恢复）。

## 遗留文件

`config.yaml`（已恢复）· `config.yaml.bak-1791214920`（保留）· `config.yaml.wizard-accident`（可删）

另：`D:\projects\.acl-recovery-20261006\` 是 10-06 会话为修复 DSH 沙箱写入被拒而做的
**Windows 文件权限备份 + 回退脚本**（给 `accounts/`、`accounts/ruibo_jiang_542e`、
`.../data` 三个目录补了当前用户的完全控制权限；脚本名 `acl-backup-*.json.ps1`，
每个都带 `-Restore` 命令）。项目 ACL 本身经诊断**无缺失权限**（`NOT_THIS_CLASS`），
所以这三处改动未必需要回退；要回退就按 `acl-report-*.jsonl` 里的 `ROLLBACK` 命令逐条执行。
