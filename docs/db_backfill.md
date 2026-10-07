# 离线解密回填私聊（db_backfill.py）

> 目标：补上 hook 漏掉的**私聊**历史。aixed DLL 只能看到微信 PC 端**已处理**的消息，
> 所以群聊稳定、私聊时有时无；本地 SQLCipher 库里是完整的。

## 全流程

```
微信启动 ──► keyhook3.dll 注入（必须早于开库）
             └─ MinHook 挂 sqlcipher_api_routines->key
                └─ 每次开加密库时拿到密钥 → wechat-keys.jsonl
                                │
                                ▼
        db_backfill.py --probe / --inspect / --run [--write]
            复制 message_*.db + -wal → 用密钥解开 → 抽私聊 → 按 msg_id 去重 → 入 messages.db
```

## 已验证的微信 4.x 库结构（4.1.10.27，2026-10-05 实测）

账号目录：`<文档>\xwechat_files\<账号>_<后缀>\db_storage\message\`

| 文件 | 说明 |
|---|---|
| `message_0.db` | 主消息库；实测只有 `message_0.db`，**没有** `message_1.db` |
| `message_0.db-wal` | **4 MB，和主库一样大 —— 最近的消息都在这里，必须一起复制** |
| `message_fts.db` / `message_resource.db` | 全文索引 / 资源，同样加密 |

`message_0.db` 内部：

* **每个会话一张表**：`Msg_<md5(会话id)>`（实测 62 张），另加 `Name2Id`
* `Name2Id(user_name, is_session)` —— **`rowid` 就是消息表里 `real_sender_id` 指向的 id**
  （实测 `ruibo_jiang` = rowid 2，即"自己"）
* 表名后缀 = `md5(会话id)`，与 `consumer/voice.py` 的 `conv_hash()` 同一套约定

消息表列：

| 列 | 含义 |
|---|---|
| `local_id` / `server_id` | 本地序号 / **MsgSvrID**（与 hook 的 `msg_id` 同源，可跨来源去重） |
| `create_time` | 秒级时间戳（`sort_seq` = 它 ×1000） |
| `local_type` | 消息类型（1=文本，3=图片，43=视频，50=通话…） |
| `real_sender_id` | 发言人 → `Name2Id.rowid`；**等于自己的 rowid 就是发出去的** |
| `message_content` | 内容。**列压缩**：见下 |
| `WCDB_CT_message_content` | 压缩标记：`0` = 明文 str，`4` = **zstd 帧**（魔数 `28 B5 2F FD`） |
| `source` / `WCDB_CT_source` | 消息来源 XML，同样的压缩规则 |

> ⚠️ 压缩标记是**按列**的。`ct=4` 时值可能是「zstd 帧 + 尾部附加数据」，
> 流式解压（`decompressobj`）比一次性 `decompress` 更稳。

密文密钥形态：微信把密钥以 **SQLCipher `x'<hex>'` 文本形式**传给 `sqlite3_key`，
所以解密时应当**把这段文本原样当口令**（`PRAGMA key = 'x''…'''`），
而不是把它再 hex 编码一次。实测 `len=99`（48 字节）、`len=67`（32 字节）两种。

## 命令

```powershell
cd D:\projects\WeChatFerryTool

# 1) 试出「哪把密钥开哪个库」，结果缓存到 data/db_key_map.json（之后不用再试错）
.venv\Scripts\python.exe scripts\db_backfill.py --probe

# 2) 看结构 + 试提取（换微信版本后先跑这个）
.venv\Scripts\python.exe scripts\db_backfill.py --inspect

# 3) 预览：只统计，不落库
.venv\Scripts\python.exe scripts\db_backfill.py --run --since-hours 720

# 4) 落库（--since-hours 决定回溯多久；8760≈一年）
.venv\Scripts\python.exe scripts\db_backfill.py --run --since-hours 720 --write
```

## 踩过的坑（都已修）

| 现象 | 原因 | 处理 |
|---|---|---|
| 钩子装上了但密钥文件是空的 | `codec_get_key` 只是 getter，且 aixed 自己注释说"可以废弃不用" | 改挂 `sqlcipher_api_routines->key`（每次开加密库必调） |
| 同上 | `codec_get_key` 只在**开库时**被调用一次，微信先启动、后注入就永远错过 | 先跑 `inject_early.ps1` 再启动微信，抢在开库之前 |
| 注入成功却没有任何输出文件 | `%USERPROFILE%\Documents` 不存在（「文档」被重定向到 `D:\JiangBo\Documents`），`ofstream` 静默失败 | 按注册表解析真实「文档」目录，并**逐个实际试写**后才采用 |
| 解密报 `file is not a database` | `PRAGMA key='x{hex}'` **缺收尾单引号**，被当口令走了 KDF | 正确写法 `PRAGMA key = "x'<hex>'"`；或用「口令文本」模式原样透传 |
| 最新消息缺失 | 只复制了主库，没复制 4 MB 的 `-wal` | `snapshot()` 连 `-wal`/`-shm` 一起复制 |
| 内容读出来是空的 | `ct=4` 是 zstd 压缩；且 `ct=0` 时 SQLite 返回的是 `str` 而不是 `bytes` | 按标记解压；类型判断两种都处理 |
| 脚本退出码 1 | 提取后没关连接，临时目录清理失败 | 提取完立即 `close()` |
| 消息不再入库 | **微信重启会清空 aixed 的回调注册** | `POST http://127.0.0.1:30001/set_callback {"url":"http://127.0.0.1:8888/hook/callback"}`，或重启 consumer |

> 顺带：`/set_callback` 的**空 body 会被当成"清空回调"**，探测它时不要发 `{}`。

## 依赖

`sqlcipher3-binary`、`zstandard`（缺 zstandard 时 `ct=4` 的内容会解析为空）。
两者都已加入 `requirements.txt`。

## 边界

* 默认**只回填私聊**。群消息的发言人在 `real_sender_id` 里（可反查 `Name2Id`），
  但 hook 已能稳定拿到群消息，回填意义不大且容易造成归属混乱。
* 微信升级后 `Weixin.dll` 的偏移会变，keyhook 需要重新定位
  （`WECHAT_CODEC_OFFSET` 可临时覆盖，结构体偏移 `0x8BB1570` 需重新确认）。
* 回填进来的行 `score` 为空 —— 可按需跑评分，或让归档流程忽略。
