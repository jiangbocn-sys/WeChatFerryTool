# 回归测试（regression suites）

这些脚本是开发过程中逐步攒下来的**固定回归测试**，每个都是独立可跑的 Python 脚本：
自建临时数据根（`WCF_DATA_DIR` 指向 `D:\projects\.wft-*`），不碰真实账号数据，跑完自己清理。

## 怎么跑

```powershell
# 全部跑一遍（推荐；会打印每套的 PASS/FAIL 汇总）
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\regression\run_all.ps1

# 单跑某一套
.\.venv\Scripts\python.exe -B .\tests\regression\wft_pergroup_check.py
```

`run_all.ps1` 会输出形如：

```
[wft_pergroup_check] OK  每群单独一份总结 验证通过
[wft_isolation_check] OK  保存隔离性（改一处不动其它）验证通过
...
=== 全部通过: True ===
```

## 各套覆盖什么

| 脚本 | 覆盖 |
|---|---|
| `wft_isolation_check` | **保存隔离性**：三个保存入口都"只动该动的"，改一处后其余配置段/labels 逐字段一致；截断提交整份拒绝 |
| `wft_retention_check` | **消息库按期清理（R-001）**：阈值口径（保留 N 个自然日、当天永不删）、dry-run 报数=真删条数、0 天不删 / 1~6 天拒绝、一天最多一次、可中断、只有 `--vacuum` 才 VACUUM、静默（不调通知）、清理后各页面仍 200、CLI |
| `wft_ingest_scope_check` | **抓取范围全开（R-002）**：名单外群/私聊照样入库、类型闸门仍有效、`_in_digest` 仍按监控名单筛选、入库日志不再有旧 `reason=`（真 Consumer + 假 hook 端到端） |
| `wft_convmap_check` | **会话名手工映射**：`voice.conv_overrides` 的键同时认完整 32 位与 8 位前缀，且 8 位前缀在**实时孤儿语音归档**里真生效；填显示名只作标记（落成独立会话） |
| `wft_cards_check` | **消息卡片解析与渲染**（`consumer/cards.py`）：图片带尺寸/大小、引用回复带被引用人+原话（含嵌套引用再解析一层）、链接带标题/描述、文件/位置/名片/通话/撤回；归档摘要与浏览页**都不再吐原始 XML**（真库校验：4415 条 0 报错） |
| `wft_summary_hint_check` | **每群总结提示**：`labels.groups.<gid>.summary_hint` 随当日总结一起提交给 LLM（含第 7 条优先级规则）；`build_lines()` 的键是群 id、文件名/标题仍用显示名；无提示时规则不出现；正文含 `{}` 不炸；标定页可存/可清空且不破坏其它字段 |
| `wft_monitor_check` | 监控名单增删 + 每群重点关注人 + 归档范围真值表（整群归档 + 重点人打 ★ / 私聊只有★才进）+ 监控规则页的每群总结提示入口 |
| `wft_range_summary_check` | **📆 阶段总结**：时间段解析（具体日期/区间/斜杠点/`周一~周三`/跨周/坏输入）、按会话取数 + 类型闸门 + ★、用户额外要求注入 prompt 的位置、dry_run 不调 LLM、落盘命名 `summary-<起>_<止>-<群>.md`、Web 三接口、**结果页内直接显示**且刷新存活、与当日总结互不干扰 |
| `wft_pergroup_check` | **每群单独一份总结**（调用次数=群数、文件命名清洗与幂等、失败隔离、合并模式、Web 预览） |
| `wft_offline_import_check` | 离线导入昵称/群成员（口令转义、备注优先、群成员解析、覆盖/不覆盖、`name_prev`、本群成员展示） |
| `wft_inject_guard_check` | keyhook 注入安全闸门（默认关闭、版本不符拒绝、`--inject-keys` 才开） |
| `wft_browse_dropdown_check` | 消息浏览页下拉：已标定全列出、未标定公众号排除、分组展示 |
| `wft_null_check` | 消息表含 NULL 时各页面不 500（priority/score/sender/content） |
| `wft_dberr_check` | 消息库打不开时给可读提示（503）而非 500 白页 |
| `wft_rules_page_check` | 「监控规则」页整合（监控名单 + 入库关键词 + 保存关键词不清空名单） |
| `wft_labeled_check` | 下拉只列"已标记"项（有名字/★重点/有重点人/有关键词） |
| `wft_form_check` / `wft_formstruct_check` | 总设置页表单化 + 表单结构（主表单唯一、无 form 嵌套） |
| `wft_guard_check` | 配置保护（误提交不写盘）+ 真实配置完整性 |
| `wft_monitor_check` / `wft_harvest_check` / `wft_reload_check` | 监控名单 / 从消息挖昵称（含只填空白）/ 标定热重载 |
| `wft_config_page_check` / `wft_llm_page_check` | 高级 JSON 模式 / 大模型设置页与测试连接 |
| `wft_contacts2_check` | 走 DLL 的昵称动态发现（假 DLL 模拟契约；本机 DLL 不可用，保留作将来） |
| `wft_final_check` / `wft_final2_check` / `wft_web3_check` | 端到端：页面渲染、消息分类闸门、关键词→归档、当日总结链路、`<think>` 剥离 |
| `wft_emptycfg_check` / `wft_e6_check` / `wft_wizard2_check` / `wft_quit_check` | 空配置待初始化、E3/E4 语义、向导回显与保 key、托盘退出语义 |
| `wft_discover_check` / `wft_discover_check2` / `wft_frozen_root_check` | 冻结版数据根发现、向导回显、同账号多目录不被误认 |
| `wft_live_check` / `wft_tray_check` | 真实数据/托盘相关的在线探测（需要环境，可能跳过） |

## 注意

* 脚本里写死了 `D:\projects\WeChatFerryTool` 作为项目根（`PROJ`），换机器要改这一行。
* 依赖 `sqlcipher3` 的只有 `wft_offline_import_check` 的一部分（它用**假连接**测逻辑，
  所以没有 `sqlcipher3` 也能跑）。
* 这些脚本**不写真实账号数据**；如果你看到 `accounts\...` 被改动，那一定是 bug，请报给我。
