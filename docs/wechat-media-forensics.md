# 微信 PC 端媒体取证笔记（2026-10-07 实测）

> 本文记录**实测验证过**的事实与方法，用于后续直接复用。
> 环境：Windows + 微信 4.1.10.27（数据目录 `D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e`）。
> ⚠️ 所有路径/长度/结论都在本机验证过；换微信版本请重新验证。
> 所有工具都**只读**（读内存、读快照），不改微信数据、不改配置。

---

## 0. 一句话结论表

| 目标 | 能否直接拿到 | 关键结论 |
|---|---|---|
| 文本消息 | ✅ 能 | hook 回调 + 自有库；也可离线读 `message_0.db` |
| **图片** | ⚠️ 半能 | 本地 `.dat` 是 **XOR 流加密**（非 AES）；内存里是明文 JPEG 但**不长期驻留** |
| **语音** | ❌ 目前不能 | PC 微信**不把语音落盘**（`silklength` 恒 0）；播放也不生成文件；"转文字"结果**不保存** |
| 消息里的媒体元数据 | ✅ 能 | `aeskey` / `voiceurl` / `md5` 等都在消息 XML 里 |
| 微信自带库（SQLCipher） | ✅ 能 | 有钥匙就能读；**每个会话一张 `Msg_<md5(会话id)>` 表** |

---

## 1. 目录结构与命名规则

```
xwechat_files\<账号>\
├── msg\
│   ├── attach\<会话md5>\<年-月>\Img\<stem>.dat        # 图片（加密）
│   │                              \Rec\<子目录>\Img\  # 转发/引用的图
│   ├── video\<年-月>\                                  # 视频（.jpg 缩略图）
│   └── file\                                          # 文件消息
├── cache\<年-月>\
│   ├── Message\<会话md5>\
│   │   ├── Bubble\<stem>_b.dat     # 气泡版图（加密，比 attach 版大）
│   │   ├── ImageTemp\              # 图片转换中间产物（明文！）
│   │   ├── Thumb\<n>_<ts>_thumb.jpg
│   │   ├── VideoTemp\  VoiceTemp\  # 临时目录（**语音只在这里出现**）
│   │   └── RecTmp\
│   └── Emoticon\<xx>\<md5>         # 表情
├── db_storage\
│   ├── message\message_0.db  message_fts.db  message_resource.db  media_0.db
│   ├── contact\contact.db    session\session.db
│   └── general\general.db
└── temp\head_image\<md5>           # 头像
```

**命名规律（重要）**

| 名字 | 含义 |
|---|---|
| `<会话md5>` = `md5(conv_id)` | 群/好友 id 的 md5（如 `49366798260@chatroom` → `dd12d75e…`） |
| `<stem>` | 图片主名（`md5` 或 16 字节 hex），`_t` 缩略图 / `_b` 气泡 / `_h` 高清 |
| VoiceTemp 里 `<序号>_<秒级时间戳>` | **微信语音文件名**，**无扩展名**，10~30 KB |
| `<n>_<ts>_thumb.jpg` | 图片/视频缩略图 |

**⚠️ 噪音陷阱**：百度网盘会在同一目录留 `<同样前缀>.baiduyun.uploading.cfg`
（数字前缀与语音文件名一模一样！判断语音时必须**排除带扩展名的文件**）。

---

## 2. 消息数据库（`db_storage`）

* 每个会话一张表：`Msg_<md5(conv_id)>`
* 列（17 个）：`local_id, server_id, local_type, sort_seq, real_sender_id, create_time,
  status, upload_status, download_status, server_seq, origin_source, source,
  message_content, compress_content, packed_info_data, WCDB_CT_message_content, WCDB_CT_source`
* `message_content` / `source` 是 **zstd 压缩**（魔数 `28 b5 2f fd`，`WCDB_CT_*=4`）
  → **直接 `LIKE '%中文%'` 搜不到**，必须先解压
* `local_type` 与 hook 回调的 `type` **编号体系不同**（本机观察：1 文本、3 图片、34 语音、
  47 表情，另有 `244813135921`、`219043332145` 这类大数）

### 读库要点（踩过的坑）

1. **钥匙**：`D:\JiangBo\Documents\wechat-keys.jsonl`（keyhook 采集），字段 `db/key/len/src/ts`。
   `db` 字段是**句柄 ID 而非文件名** → key↔库 的对应关系要靠顺序试。
   **实测：59 个 key 候选能打开全部 6 个库**（`tools/probe_keys_health.py` 验证）。
2. ⚠️ **同一个快照文件上连续试多个 key 会失败** —— 失败的连接会留下状态。
   **必须每个 key 用一份全新的快照**（`wo.snapshot()` + `wo.open_db()`）。
3. ⚠️ `sqlcipher3` 的 Cursor **不能配 `sqlite3.Row`**（`TypeError: Row() argument 1 must be
   sqlite3.Cursor`）→ 用元组下标。
4. ⚠️ 带 `ORDER BY create_time DESC` 的查询曾出现"返回 0 行"的怪现象；
   排查时**先去掉 ORDER BY** 确认数据在不在。

---

## 3. 媒体消息的 XML 里有什么

**图片**
```xml
<img aeskey="c8bd2599…" encryver="1" cdnthumbaeskey="c8bd2599…" cdnthumburl="305f"
     cdnthumblength="4700" cdnthumbheight="480" cdnthumbwidth="214"
     length="489072" hevc_mid_size="60593" md5="c98dc358…" />
```

**语音**
```xml
<voicemsg voiceformat="4" voicelength="35987" length="63280" aeskey="7760ba90…"
          voiceurl="7f0c000502207c9f…（约 101~102 字节的十六进制 blob）"
          silklength="0" />
```

* `aeskey` 是 32 hex 字符（16 字节）；
* ⚠️ `voiceurl` **不是 URL**，是 101~102 字节的 blob（protobuf 风格但首字节 `0x7f` 非法
  字段号 → 不是标准 protobuf）。**用 aeskey 解它、ECB/CBC 各种组合都解不出音频**。
* ⚠️ `silklength` **全库恒为 0** → 本地从未保存 silk 数据。

---

## 4. `.dat` 容器（图片/媒体加密文件）

**头部（所有 `.dat` 一致）**
```
07 08 56 32 08 07 00 04 00 00 <u16le 声明大小> 00 00 01 <1 字节变体>
```
* `_t.dat`（缩略）：实际大小 − 声明大小 ≈ **1055**（常量）
* `_h.dat`（高清）：可能 ≥ 10 MB

**已排除的算法**（穷尽试过）：单字节 XOR（全部 256 个 key）、zlib/gzip/zstd、
AES-128-ECB（key 取 XML aeskey / head[0:16] / head[16:32] / cdnurl[0:16] /
md5·sha256(aeskey) × 偏移 0/15/16/31/32）、AES-128-CBC（IV 取 head[0:16]/全零/cdnurl[0:16]）、
AES+XOR 0x37/0xF9、重复 key XOR、明文碎片。

**🔑 最强线索（2026-10-07 发现）**：**同尺寸的多个 `.dat`，其"载荷前 16 字节"完全相同**：
```
尺寸 2698 的 3 个文件 → 3e fb 15 44 78 75 b7 61 ee 43 8a 88 f2 1d aa 8b
尺寸 2386 的 3 个文件 → 3e fb 15 44 78 75 b7 61 …（一模一样）
尺寸 9617 的 2 个文件 → 29 b3 a5 26 25 d4 f4 8a 43 90 d4 36 94 2a fa 54
```
→ **这是 XOR 流 / 固定头部的特征，不是 AES**（AES 不可能让不同文件共享同一段前缀）。
**下一步方向**：拿"同一变体"的明文（原图 ≠ 本地 `.dat`！）做 XOR，直接暴露密钥流。

⚠️ **已知明文对照失败的教训**：用户另存的是**原图**（489 KB），本地 `.dat` 是
**中图/缩略图**（2~60 KB）→ **不是同一份数据**，XOR 无意义。
**必须同变体**才能做已知明文攻击。

---

## 5. 语音：为什么拿不到（穷尽排查记录）

| 假设 | 验证结果 |
|---|---|
| 语音文件在某个目录 | ❌ 全盘只有 8 个 `VoiceTemp`；`msg\attach` 下**没有 Audio 目录** |
| 打开窗口会拉语音 | ❌ 打开窗口只新增图片 `.dat`，`VoiceTemp` 无新文件 |
| 播放会落盘 | ❌ 播放一条后 diff 全账号目录，**`VoiceTemp` 无新文件**（只多了图片气泡） |
| hook 有语音下载接口 | ❌ DLL 只有 `Decode_Pic/QueryDB/GetAllDBName/GetSelfProfile/SendTextMsg/SendImgMsg/ForwardXMLMsg`；试探 `/GetAudioMsg`、`/DownloadVoice` 等**全 404** |
| 消息体里有音频密文 | ❌ `silklength=0`、`voiceurl` 仅 101 字节（定位信息） |
| 微信"转文字"会存本地 | ❌ 转文字后 db 有写入，但**全库搜不到转写文本**（搜遍 message_0/fts/media/resource 全部表，解压后搜，零命中） |
| 内存里有明文音频 | ⚠️ **有** `#!SILK_V3` 头（7 处），但内存里是**多条流拼接**，按 `[len2][payload]` 解析到中途会断，pysilk 解不开 |

**结论**：PC 微信的语音是**服务器端按需拉流**，本地不落地。
想拿到只能靠：① 内存/hook 截获（需解决流边界或 hook 解密函数）；② 手机端采集（另一套链路）。

---

## 6. 内存取证（本项目已验证可行）

**关键事实：微信主进程内存里有解密后的媒体数据**

| 特征 | 命中数（主进程 36480） | 含义 |
|---|---|---|
| `\x02#!SILK` | 7 | 明文语音（silk） |
| `\xff\xd8\xff\xe0` (JPEG) | 94 | 明文图片 |
| `\x89PNG` | 14 | 明文 PNG |
| `\x28\xb5\x2f\xfd` (zstd) | 110 | 明文消息内容 |

* 工具：`tools/scan_wechat_memory.py`（扫描）、`tools/dump_wechat_memory_media.py`（dump）、
  `tools/dump_wechat_images.py`（提取并 PIL 校验）
* ✅ 已验证 dump 出的 JPEG 能正常打开（132×132、160×156）
* ⚠️ 但**绝大多数不是聊天图**：768×1392 是 emoji 面板、960×1839 是气泡背景、
  64×64/128×128 是头像、800×800 是素材 → **聊天照片不长期驻留内存**
* API 要点：`OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ)` +
  `VirtualQueryEx` 枚举可读区 + `ReadProcessMemory`；**只读，不写进程**
* 🔑 **普通权限也可能读得到**（本机未提权即成功；失败时提示需管理员）
* 技巧：JPEG 按 `FFD9`、PNG 按 `IEND\xaeB\x82` 截断 + **PIL 逐个校验**，可过滤截断/拼接的假样本

---

## 7. 排查方法论（比结论更值钱）

1. **`tools/snapshot_account.py`**：记录整个账号目录的 (路径,大小,mtime)，
   操作前后 `diff` → **精确定位"某操作产生了什么文件"**。
   本次靠它证明了"播放语音不落盘"、"打开窗口会缓存图片"、"转文字改了哪些库"。
2. **结论性排查的输出要写文件**（PowerShell 管道会吞输出，中文/emoji 还可能在 GBK 控制台
   `UnicodeEncodeError` 崩溃）。
3. **控制变量**：一次只改一个动作（打开窗口 ≠ 播放语音），否则归因会错。
4. **穷尽式排除要写下来**：本项目两次"以为无解"，后来都是靠**同尺寸文件对比**
   或**换 key 试法**找到新线索 —— 别在"试过几种"之后轻易下"不可能"的结论。
5. **试密钥/试解密的循环里，失败会污染状态** → 每次用全新副本。

---

## 8. 待办与下一步（按性价比）

| 优先级 | 事项 | 说明 |
|---|---|---|
高 | **同变体明文 → 破 `.dat`** | 让微信另存为**与本地 `.dat` 同尺寸**的图（或从内存 dump 对应缩略图），XOR 即暴露密钥流。成功后图片问题彻底解决 |
高 | **时机化内存抓取** | 用户"打开图片/播放语音"的**瞬间**扫内存 → 验证"渲染瞬间能抓到明文"，作为兜底能力 |
中 | **语音流边界** | 分析内存里的拼接缓冲，找出**完整单条流**的起止（`#!SILK` 出现位置序列是线索） |
中 | 自研 DLL | hook 解密函数，图片+语音一起解决；成本以周计，需先确认目标函数 |
低 | `Decode_Pic` 逆向 | 现接口是空壳，但 DLL 里有该符号，值得看看它内部调了什么 |

**已确认无效、不要重复投入**：
* 从 VoiceTemp 轮询补语音（文件根本不出现）
* 让用户"点播放"来触发落盘
* 读微信"转文字"结果（不保存）
* 用 XML aeskey 解 `.dat` / 解 `voiceurl`
* hook 的语音下载接口（不存在）
