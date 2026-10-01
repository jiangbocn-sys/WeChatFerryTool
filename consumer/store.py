"""SQLite 落库模块。

所有命中过滤规则的消息都会进入 messages 表；
评分结果会更新到 score 字段。
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


_SCHEMA = """
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
    pushed      INTEGER DEFAULT 0        -- 是否已推送 (0/1)
);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_name);
CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_score ON messages(score);
"""


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

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
    ) -> int | None:
        """插入一条消息。已存在（同 msg_id）则跳过，返回 None。"""
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                  (msg_id, group_name, sender, sender_id, content, msg_type, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (msg_id, group_name, sender, sender_id, content, msg_type, received_at),
            )
            return cur.lastrowid if cur.rowcount > 0 else None

    def update_score(self, msg_id: str, score: int, reason: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE messages SET score = ?, score_reason = ? WHERE msg_id = ?",
                (score, reason, msg_id),
            )

    def mark_pushed(self, msg_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE messages SET pushed = 1 WHERE msg_id = ?",
                (msg_id,),
            )

    def recent(self, limit: int = 50, group: str | None = None) -> list[dict]:
        with self._lock, self._conn() as conn:
            if group:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE group_name = ? ORDER BY received_at DESC LIMIT ?",
                    (group, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages ORDER BY received_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]