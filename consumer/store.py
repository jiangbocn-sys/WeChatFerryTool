"""SQLite 落库模块。

所有命中过滤规则的消息都会进入 messages 表；
评分结果会更新到 score 字段。
"""
import sqlite3
import threading
from pathlib import Path


_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id      TEXT UNIQUE,             -- 微信消息 ID，用于去重
    group_name  TEXT NOT NULL,
    sender      TEXT NOT NULL,
    sender_id   TEXT,
    content     TEXT NOT NULL,
    msg_type    INTEGER DEFAULT 1,       -- 1=文本 其它见 WechatFerry 协议
    received_at INTEGER NOT NULL,        -- unix timestamp
    score       INTEGER,                 -- LLM 评分 1~5
    score_reason TEXT,                   -- LLM 给的简短理由
    pushed      INTEGER DEFAULT 0,       -- 是否已推送 (0/1)
    priority    INTEGER DEFAULT 0,       -- 0=普通，1=重要（标定群/联系人），2=紧急
    direction   TEXT DEFAULT NULL,       -- 'in'=收到 / 'out'=我们发的 / NULL=未确定
    transcript  TEXT DEFAULT NULL        -- 语音消息转写文本（ASR）
);
"""

# 注意：priority 索引放在 ALTER TABLE 迁移之后，老库升级时不会因为缺列而失败
_INDEXES_BASE = """
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_name);
CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_score ON messages(score);
"""
_INDEX_PRIORITY = """
CREATE INDEX IF NOT EXISTS idx_messages_priority ON messages(priority);
"""


class Store:
    """线程安全的 SQLite 封装。

    设计要点：
    - 单连接复用：避免每次操作都 connect/commit/close，高频消息场景下 IO 节省明显
    - WAL 模式：允许读不阻塞写，避免 "database is locked"
    - 锁保护：所有写操作串行化（SQLite 写本来就串行，锁主要防并发连接交错）
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            check_same_thread=False,  # 允许从 worker 线程使用
        )
        self._conn.row_factory = sqlite3.Row
        # WAL + 平衡 synchronous，提升并发性能
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_TABLE)
            self._conn.executescript(_INDEXES_BASE)
            # 给老库做 schema 迁移：补 priority / direction 列
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "priority" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN priority INTEGER DEFAULT 0")
            if "direction" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN direction TEXT DEFAULT NULL")
            if "transcript" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN transcript TEXT DEFAULT NULL")
            self._conn.executescript(_INDEX_PRIORITY)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_message(
        self,
        *,
        msg_id: str,
        group_name: str,
        sender: str,
        sender_id: str,
        content: str,
        msg_type: int,
        received_at: int,
        priority: int = 0,
        direction: str | None = None,
    ) -> int | None:
        """插入一条消息。已存在（同 msg_id）则跳过，返回 None。"""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages
                  (msg_id, group_name, sender, sender_id, content, msg_type, received_at, priority, direction)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (msg_id, group_name, sender, sender_id, content, msg_type, received_at, priority, direction),
            )
            self._conn.commit()
            return cur.lastrowid if cur.rowcount > 0 else None

    def update_score(self, msg_id: str, score: int, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET score = ?, score_reason = ? WHERE msg_id = ?",
                (score, reason, msg_id),
            )
            self._conn.commit()

    def update_direction(self, msg_id: str, direction: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET direction = ? WHERE msg_id = ?",
                (direction, msg_id),
            )
            self._conn.commit()

    def update_transcript(self, msg_id: str, transcript: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET transcript = ? WHERE msg_id = ?",
                (transcript, msg_id),
            )
            self._conn.commit()

    def get_transcript(self, msg_id: str) -> str:
        """读某条消息当前的 transcript（空/不存在返回空串）。

        用途：语音**延迟重试**前先看一眼是不是已经被别处补上了，避免重复解码/重复调 ASR。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT transcript FROM messages WHERE msg_id = ?", (msg_id,)
            ).fetchone()
        return str(row[0] or "") if row else ""

    def mark_pushed(self, msg_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET pushed = 1 WHERE msg_id = ?",
                (msg_id,),
            )
            self._conn.commit()

    def recent(self, limit: int = 50, group: str | None = None) -> list[dict]:
        with self._lock:
            if group:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE group_name = ? ORDER BY received_at DESC LIMIT ?",
                    (group, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM messages ORDER BY received_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]