"""消息库按期清理（需求 R-001）。

设计要点（与 docs/requirements.md 的规格一致）：

* **只删老消息**：阈值取「本地零点」而不是滚动 24 小时 —— `retention_days: 30`
  表示保留最近 30 个自然日（含今天），更早的行删掉。**当天数据永不参与**。
* **分批 + 可中断**：每批 2000 行，批间 `sleep(interval_s)`，`stop_event` 一置位
  当前批跑完就收工并如实回报 `interrupted=True`。绝不长时间持锁，抓取不受影响。
* **静默**：只写日志（`logs/consumer.log`）与状态文件，不弹窗、不通知。
* **删完 checkpoint**：`PRAGMA wal_checkpoint(TRUNCATE)` 收缩 -wal；
  `vacuum=True` 时才 `VACUUM`（会持锁较久，默认关）。
* **dry-run**：只统计不删，供页面/CLI 预览。

CLI：

    python -m consumer.cleanup --dry-run --days 90
    python -m consumer.cleanup --days 90
    python -m consumer.cleanup --days 90 --vacuum
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

from paths import data_root

log = logging.getLogger("consumer.cleanup")

#: 配置里允许的最小保留天数。低于它会拒绝执行 —— 清理是不可逆的，
#: 一个手滑写成 0/3 就把历史全删了，所以给一条硬下限。
MIN_RETENTION_DAYS = 7

DEFAULT_BATCH_SIZE = 2000
DEFAULT_SLEEP_S = 0.2


# ---------------------------------------------------------------------------
# 路径 / 状态
# ---------------------------------------------------------------------------
def resolve_db_path(raw: str | Path) -> Path:
    """把配置里的 sqlite_path 解析成绝对路径（相对值按数据根解释）。"""
    p = Path(raw)
    if p.is_absolute():
        return p
    return data_root() / p


def state_path() -> Path:
    return data_root() / "data" / "cleanup_state.json"


def load_state(path: Path | None = None) -> dict:
    p = Path(path) if path is not None else state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    """写清理状态。写不了只告警 —— 状态文件丢了顶多今天多跑一次，不该影响主循环。"""
    p = Path(path) if path is not None else state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        log.warning("清理状态写不了（%s）：%s", p, e)


# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------
def retention_cutoff(retention_days: int, now: datetime | None = None) -> int:
    """返回"保留门槛"的 unix 时间戳（本地零点）。

    `retention_days = N` → 保留最近 N 个自然日（含今天）：
    门槛 = 今天零点 - (N-1) 天。`received_at < 门槛` 的行才删。

    ⚠️ 门槛最小也是"昨天零点"（N=1 时），**当天数据永不参与清理**。
    """
    now = now or datetime.now()
    start_today = datetime.combine(now.date(), dtime(0, 0))
    cutoff = start_today - timedelta(days=max(1, int(retention_days)) - 1)
    return int(cutoff.timestamp())


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------
@dataclass
class CleanupResult:
    days: int
    cutoff_ts: int
    matched: int = 0                 # 命中（早于门槛）的行数
    deleted: int = 0
    total: int = 0                   # 清理后库里总行数
    batches: int = 0
    dry_run: bool = False
    interrupted: bool = False
    vacuumed: bool = False
    checkpointed: bool = False
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def log_line(self) -> str:
        action = "预演" if self.dry_run else "删除"
        return (f"数据清理：{action} {self.deleted} 条（保留 {self.days} 天），"
                f"耗时 {self.elapsed_ms} ms，当前共 {self.total} 条"
                + ("，已中断（剩余下次继续）" if self.interrupted else ""))


def cleanup_messages(
    db_path: str | Path,
    *,
    retention_days: int,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sleep_s: float = DEFAULT_SLEEP_S,
    stop_event: Any = None,
    dry_run: bool = False,
    vacuum: bool = False,
    state_file: str | Path | None = None,
    write_state: bool = True,
    time_fn: Callable[[], float] = time.time,
) -> CleanupResult:
    """删除超过 `retention_days` 天的消息。返回 `CleanupResult`。

    `stop_event`：任何带 `is_set()` 的对象（`threading.Event` 即可）。
    `dry_run=True` 只统计不删。库里没有 messages 表（全新库）时安全返回 0。
    """
    db = Path(db_path)
    days = int(retention_days)
    start = time_fn()
    res = CleanupResult(days=days, cutoff_ts=retention_cutoff(days, now), dry_run=dry_run)

    if days < MIN_RETENTION_DAYS:
        res.errors.append(f"保留天数 {days} 小于下限 {MIN_RETENTION_DAYS}，已跳过（拒绝执行）")
        log.warning("数据清理未执行：%s", res.errors[-1])
        return res
    if not db.exists():
        res.errors.append(f"消息库不存在: {db}")
        log.info("数据清理跳过：消息库不存在 %s", db)
        return res

    def stopped() -> bool:
        # 兼容两种"停止信号"：threading.Event（is_set()）与 consumer 里的布尔 _stop
        if stop_event is None:
            return False
        fn = getattr(stop_event, "is_set", None)
        if callable(fn):
            return bool(fn())
        return bool(stop_event)

    conn: sqlite3.Connection | None = None
    try:
        # timeout 让清理自动给抓取让路（拿不到写锁就等，不抛 database is locked）
        conn = sqlite3.connect(str(db), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as e:
            res.errors.append(f"读库结构失败: {e}")
            return res
        if "messages" not in tables:
            res.errors.append("库里没有 messages 表（全新库？）")
            res.total = 0
            return res

        res.matched = int(conn.execute(
            "SELECT COUNT(*) FROM messages WHERE received_at < ?", (res.cutoff_ts,)
        ).fetchone()[0])

        if not dry_run and res.matched:
            while True:
                if stopped():
                    res.interrupted = True
                    break
                cur = conn.execute(
                    "DELETE FROM messages WHERE id IN "
                    "(SELECT id FROM messages WHERE received_at < ? LIMIT ?)",
                    (res.cutoff_ts, int(batch_size)),
                )
                n = cur.rowcount or 0
                conn.commit()
                res.deleted += n
                res.batches += 1
                if n < int(batch_size):
                    break                      # 删完了
                if sleep_s and not stopped():
                    time.sleep(sleep_s)
        res.total = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

        if not dry_run:
            if res.deleted and not stopped():
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                    res.checkpointed = True
                except sqlite3.Error as e:
                    res.errors.append(f"wal_checkpoint 失败: {e}")
                if vacuum:
                    try:
                        # VACUUM 不能在事务里，且会持锁较久
                        conn.isolation_level = None
                        conn.execute("VACUUM")
                        res.vacuumed = True
                    except sqlite3.Error as e:
                        res.errors.append(f"VACUUM 失败: {e}")
    except sqlite3.Error as e:
        res.errors.append(f"打不开/清理消息库 {db}: {e}")
        log.warning("数据清理失败（不影响抓取）: %s", e)
        return res
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    res.elapsed_ms = int((time_fn() - start) * 1000)

    if not dry_run and write_state and res.deleted:
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        save_state({
            "last_date": today,                       # 每天最多跑一次的依据
            "at": int(time.time()),
            "deleted": res.deleted,
            "days": days,
            "total": res.total,
            "cutoff_ts": res.cutoff_ts,
            "vacuumed": res.vacuumed,
            "elapsed_ms": res.elapsed_ms,
        }, state_file)
    if res.errors:
        for e in res.errors:
            log.warning("数据清理告警: %s", e)
    log.info(res.log_line())
    return res


# ---------------------------------------------------------------------------
# 调度（挂在 consumer 的常驻线程里；也供 Web「立即清理」复用）
# ---------------------------------------------------------------------------
def read_storage_cfg(cfg: dict) -> dict:
    st = (cfg.get("storage") or {}) if isinstance(cfg, dict) else {}
    return {
        "path": st.get("sqlite_path") or "data/messages.db",
        "retention_days": int(st.get("retention_days") or 0),
        "auto_cleanup": bool(st.get("auto_cleanup", False)),
        "cleanup_time": str(st.get("cleanup_time") or "00:00"),
        "vacuum": bool(st.get("cleanup_vacuum", False)),
    }


def should_run(cfg: dict, *, now: datetime | None = None,
               state: dict | None = None) -> tuple[bool, str]:
    """该不该跑自动清理。返回 (是否跑, 原因说明)。原因会写进日志，便于排查。"""
    sc = read_storage_cfg(cfg)
    now = now or datetime.now()
    if sc["retention_days"] <= 0:
        return False, "未配置保留天数（retention_days=0 → 不自动清理）"
    if sc["retention_days"] < MIN_RETENTION_DAYS:
        return False, f"保留天数 {sc['retention_days']} 小于下限 {MIN_RETENTION_DAYS}，不执行"
    if not sc["auto_cleanup"]:
        return False, "自动清理开关关闭（auto_cleanup=false）"
    try:
        hh, mm = (int(x) for x in str(sc["cleanup_time"]).split(":")[:2])
    except (ValueError, TypeError):
        hh, mm = 0, 0
    if (now.hour, now.minute) < (hh, mm):
        return False, f"还没到清理时间 {sc['cleanup_time']}"
    st = state if state is not None else load_state()
    today = now.strftime("%Y-%m-%d")
    if str(st.get("last_date") or "") == today:
        return False, f"今天（{today}）已经清理过"
    return True, "到点且今天未清理"


def run_scheduled(cfg: dict, *, now: datetime | None = None,
                  dry_run: bool = False, stop_event: Any = None) -> CleanupResult | None:
    """按配置跑一次（含"今天跑过没有"的判断）。不满足条件返回 None。"""
    ok, why = should_run(cfg, now=now)
    if not ok:
        log.debug("自动清理跳过：%s", why)
        return None
    sc = read_storage_cfg(cfg)
    log.info("开始自动清理：保留 %d 天（阈值 %s），%s",
             sc["retention_days"],
             datetime.fromtimestamp(retention_cutoff(sc["retention_days"], now))
             .strftime("%Y-%m-%d %H:%M"),
             "VACUUM 开启" if sc["vacuum"] else "只 checkpoint")
    return cleanup_messages(
        resolve_db_path(sc["path"]),
        retention_days=sc["retention_days"],
        now=now,
        dry_run=dry_run,
        vacuum=sc["vacuum"],
        stop_event=stop_event,
    )


def count_older(db_path: str | Path, retention_days: int,
                now: datetime | None = None) -> int:
    """只数不删（页面预览、测试断言用）。异常或读不到表时返回 -1。

    这里**不套用清理的"最小天数"限制**（那是给真删用的保险）：
    预演任何时候都该能报数 —— 顺手也保证"数一数"绝不会有副作用。
    """
    db = Path(db_path)
    if not db.exists():
        return -1
    try:
        conn = sqlite3.connect(str(db), timeout=10.0)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            cutoff = retention_cutoff(int(retention_days), now)
            return int(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE received_at < ?", (cutoff,)).fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.debug("计数失败 %s: %s", db, e)
        return -1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="清理 app 消息库里超过 N 天的记录（默认只 checkpoint，不 VACUUM）")
    ap.add_argument("--days", type=int, required=True,
                    help=f"保留天数（含今天）；小于 {MIN_RETENTION_DAYS} 会拒绝执行")
    ap.add_argument("--db", default=None, help="消息库路径（默认取配置 storage.sqlite_path）")
    ap.add_argument("--dry-run", action="store_true", help="只报数，不删")
    ap.add_argument("--vacuum", action="store_true", help="删完顺带 VACUUM 回收磁盘（较慢）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    db = args.db
    if not db:
        db = resolve_db_path("data/messages.db")
        try:
            import yaml
            from paths import config_path
            cp = config_path()
            if cp.exists():
                cfg = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
                sc = read_storage_cfg(cfg)
                db = resolve_db_path(sc["path"])
        except Exception as e:  # noqa: BLE001
            print(f"（读配置失败，用默认路径 {db}：{e}）")

    r = cleanup_messages(db, retention_days=args.days, dry_run=args.dry_run,
                         vacuum=args.vacuum)
    print(f"库: {db}")
    print(f"命中（早于 {datetime.fromtimestamp(r.cutoff_ts):%Y-%m-%d %H:%M}）: {r.matched} 条")
    if args.dry_run:
        print("dry-run：未删除任何数据")
    else:
        print(f"已删除: {r.deleted} 条；当前共 {r.total} 条；"
              f"checkpoint={r.checkpointed} vacuum={r.vacuumed}")
    for e in r.errors:
        print(f"⚠️ {e}")
    return 1 if (r.errors and not r.dry_run) else 0


if __name__ == "__main__":
    raise SystemExit(main())
