"""主入口（HTTP server 版，适配 aixed/WeChat-Hook）。

启动顺序：
1. 读 + 校验 config.yaml
2. 启 HTTP server 在 callback_port（默认 8888），准备接收 DLL 推送
3. 调 DLL /set_callback 把本机地址告诉 DLL
4. DLL 收到消息 → POST 到我们这里 → filter → store → score → push

协议说明（基于 aixed/WeChat-Hook DLL 实际推送的 payload）：
- event_type: 1001 表示新消息
- type: 1 = 文本，49 = 链接/卡片，51 = 系统消息，其它见 WeChatFerry 协议
- 群消息：wxid == roomid == "@chatroom" 后缀；sender 是群成员 wxid
- 私聊：wxid 是对方 wxid，roomid 为空，sender 是对方 wxid
"""
import json
import logging
import shutil
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from consumer.asr import ASRClient
from consumer.hook_client import HookClient, HookConfig, HookError
from consumer.notifier import BarkConfig, Notifier
from consumer.replier import Replier
from consumer.scorer import Scorer, ScorerConfig
from consumer.sent_tracker import SentTracker
from consumer.store import Store
from consumer.voice import capture_voice, decode_to_wav, find_wechat_files_dir
from consumer.voice_tap import VoiceTap, conv_hash

from paths import config_path, data_root

PROJECT_DIR = data_root()
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"
REPLY_STATE_PATH = PROJECT_DIR / "data" / "reply_state.json"
SENT_LOG_PATH = PROJECT_DIR / "data" / "sent_log.json"


# ------- logging --------
# 日志目录/日志文件写不了**不能**让程序起不来：打包成 exe 后用户可能把它放在
# 只读目录、或日志被别的进程占着。写不了就退回"只打控制台"，并明确警告一句。
LOG_DIR = PROJECT_DIR / "logs"


def _safe_stderr(msg: str) -> None:
    """`--windowed` 打包时 sys.stderr 是 None，直接 print(file=sys.stderr) 会崩。"""
    try:
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _build_log_handlers() -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:          # 打包成 --windowed 时没有控制台
        handlers.append(logging.StreamHandler(sys.stdout))
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_DIR / "consumer.log", encoding="utf-8"))
    except OSError as e:
        _safe_stderr(f"[warn] 无法写日志文件 {LOG_DIR / 'consumer.log'}（{e}），本次只输出到控制台")
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_build_log_handlers(),
)
log = logging.getLogger("consumer.main")


# ------- 配置校验 --------
class ConfigError(Exception):
    pass


def _need(d: dict, keys: list[str], where: str) -> None:
    for k in keys:
        if k not in d or d[k] in (None, ""):
            raise ConfigError(f"配置缺失: {where}.{k}")


def load_config(path: str = "config.yaml") -> dict:
    # config.yaml 是**全局**的：账号目录优先，其次基目录（见 paths.config_path）。
    # 注意不能直接拼 PROJECT_DIR —— 账号隔离后 PROJECT_DIR 是账号数据目录。
    if path == "config.yaml":
        p = config_path()
    else:
        p = Path(path)
        if not p.is_absolute():
            p = PROJECT_DIR / p
    if not p.exists():
        raise ConfigError(f"找不到配置文件: {p}（请把 config.example.yaml 复制为 config.yaml 再填值）")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    """启动期一次性校验。错误直接抛 ConfigError，main() 捕获并退出。"""
    _need(cfg, ["hook", "filter", "llm", "storage"], "<root>")
    _need(cfg["hook"], ["api_base", "callback_host", "callback_port"], "hook")
    _need(cfg["llm"], ["base_url", "api_key", "model"], "llm")
    _need(cfg["storage"], ["sqlite_path"], "storage")

    bark = cfg.get("bark", {}) or {}
    if bark.get("enabled"):
        _need(bark, ["server", "key"], "bark")

    f = cfg["filter"]
    for k in ("groups", "senders", "keywords"):
        if k in f and not isinstance(f[k], list):
            raise ConfigError(f"filter.{k} 必须是列表")
    for k in ("groups", "senders", "keywords"):
        f.setdefault(k, [])
    f.setdefault("case_insensitive", True)

    if cfg["llm"]["api_key"] in ("REPLACE_ME", ""):
        log.warning("llm.api_key 未配置，消息会入库但不会 LLM 评分")

    if bark.get("enabled") and bark.get("key") in ("REPLACE_ME", ""):
        log.warning("bark.key 未配置，达到阈值也不会推送")

    hook = cfg["hook"]
    if not isinstance(hook.get("callback_port"), int):
        raise ConfigError("hook.callback_port 必须是整数")
    hook.setdefault("self_wxid", "")


# ------- 主循环 --------
class Consumer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._stop = False  # 必须最早初始化（后台线程会引用）
        self.store = Store(cfg["storage"]["sqlite_path"])
        # 注意：**入库不再用 Filter**（需求 R-002，2026-10-07）—— 所有会话都入库，
        # 只受"入库类型闸门"限制。`consumer/filter.py` 保留（独立可用/有回归），
        # 但入库路径不再引用它；归档范围由 digest.py 读 filter.groups 决定。
        self.scorer = Scorer(ScorerConfig(
            base_url=cfg["llm"]["base_url"],
            api_key=cfg["llm"]["api_key"],
            model=cfg["llm"]["model"],
        ))
        bark_cfg = cfg.get("bark") or {}
        self.notifier = Notifier(
            BarkConfig(server=bark_cfg["server"], key=bark_cfg["key"])
            if bark_cfg.get("enabled") else None
        )
        self.push_threshold = int(cfg["llm"].get("push_threshold", 4))

        hook_cfg = cfg["hook"]
        self.hook = HookClient(HookConfig(
            api_base=hook_cfg["api_base"],
            timeout_s=float(hook_cfg.get("api_timeout_s", 5.0)),
        ))
        self.callback_host = hook_cfg["callback_host"]
        self.callback_port = int(hook_cfg["callback_port"])
        self.self_wxid = (hook_cfg.get("self_wxid") or "").strip()

        # 标定文件（重要的群/联系人），用于给消息打 priority 标签
        self._labels_mtime: float | None = None      # 热重载用（见 _maybe_reload_labels）
        self.labels = self._load_labels()
        n_g_imp = sum(1 for v in self.labels.get("groups", {}).values() if v.get("important"))
        n_s_imp = sum(1 for v in self.labels.get("senders", {}).values() if v.get("important"))
        log.info(
            "已加载标定: %d 群 / %d 联系人；重点关注 %d 群 / %d 人",
            len(self.labels.get("groups", {})), len(self.labels.get("senders", {})),
            n_g_imp, n_s_imp,
        )

        # 跟踪我们发出去的消息（用于标记 direction=out）
        self.sent_tracker = SentTracker(SENT_LOG_PATH)

        # 语音捕获（收到语音时立刻从微信 VoiceTemp 抓临时文件）
        voice_cfg = cfg.get("voice") or {}
        self.voice_enabled = bool(voice_cfg.get("enabled", True))

        # 入库类型闸门：默认排除**表情**(type=47)。用户可在 Web 端「消息分类」里勾选。
        # 注意「入库」与「归档」是两件事：这里只决定存不存，
        # 归档的筛选（重点群 ∩ 重点成员/关键词）在 digest.py 里。
        #
        # **47 是硬闸门**：表情在任何情况下都不入库（Web 端也把它显示为不可取消）。
        # 即使有人手工把 47 从 config.yaml 里删掉，这里也会加回来 —— 否则表情会混进归档。
        storage_cfg = cfg.get("storage") or {}
        excl = storage_cfg.get("ingest_exclude_types")
        if excl is None:
            excl = [47]
        excl_types = {int(x) for x in excl}
        if 47 not in excl_types:
            log.warning("storage.ingest_exclude_types 里没有 47（表情）——已强制加回：表情不进库")
            excl_types.add(47)
        self.ingest_exclude_types = excl_types
        log.info("入库类型闸门：排除类型 %s（表情 47 恒排除）",
                 sorted(self.ingest_exclude_types))
        self.voice_dir = PROJECT_DIR / (voice_cfg.get("dir") or "data/voices")
        self.wechat_dir = None
        self.voice_tap: VoiceTap | None = None
        if self.voice_enabled:
            self.wechat_dir = find_wechat_files_dir(self.self_wxid)
            if self.wechat_dir:
                log.info("语音捕获已启用，微信数据目录: %s", self.wechat_dir)
                # 事件驱动全局监听（毫秒级捕获，覆盖收/发两个方向）
                self.voice_tap = VoiceTap(self.wechat_dir, self.voice_dir / "raw")
                self.voice_tap.start()
            else:
                log.warning("语音捕获已启用但找不到微信数据目录（xwechat_files），功能不可用")

        # 语音转写（Mac mini whisper 服务）
        asr_cfg = cfg.get("asr") or {}
        self.asr = ASRClient(
            url=str(asr_cfg.get("url") or ""),
            timeout_s=float(asr_cfg.get("timeout_s", 60)),
            enabled=bool(asr_cfg.get("enabled", True)),
        )
        if self.asr.ready:
            log.info("语音转写已启用: %s", self.asr.url)
        else:
            log.info("语音转写未配置（asr.url 为空）——语音会存档为 [待转写]，之后可批量补转")

        # 孤儿语音处理（自己发出的语音没有消息事件 → 定时扫描转写归档）
        if self.voice_tap is not None and self.asr.ready:
            threading.Thread(target=self._orphan_sweeper_loop, name="voice-orphan", daemon=True).start()
            log.info("孤儿语音处理已启用（自己发出的语音也会转写归档）")

        # 每日重点发言归档
        digest_cfg = cfg.get("digest") or {}
        self.digest_enabled = bool(digest_cfg.get("enabled", True))
        self.digest_time = str(digest_cfg.get("time") or "23:30")
        self.digest_out_dir = PROJECT_DIR / (digest_cfg.get("dir") or "reports")
        # 归档后是否顺带让 LLM 总结（默认开；没配 LLM 会自动跳过；digest.summarize: false 可关）
        self.digest_summarize = bool(digest_cfg.get("summarize", True))
        if self.digest_enabled:
            threading.Thread(target=self._digest_loop, name="digest", daemon=True).start()
            log.info("每日归档已启用：每天 %s 生成（目录 %s，归档后%s）",
                     self.digest_time, self.digest_out_dir,
                     "自动总结" if self.digest_summarize else "不自动总结")

        # 消息库按期清理（需求 R-001）：后台静默、一天最多一次、分批可中断
        from consumer.cleanup import MIN_RETENTION_DAYS, read_storage_cfg
        _cl = read_storage_cfg(cfg)
        self.cleanup_cfg = _cl
        if _cl["retention_days"] > 0 and _cl["auto_cleanup"]:
            threading.Thread(target=self._cleanup_loop, name="cleanup", daemon=True).start()
            if _cl["retention_days"] < MIN_RETENTION_DAYS:
                log.warning("数据清理：保留天数 %d 小于下限 %d，不会执行"
                            "（请在设置页改成 ≥ %d）",
                            _cl["retention_days"], MIN_RETENTION_DAYS, MIN_RETENTION_DAYS)
            else:
                log.info("数据清理已启用：每天 %s 后清理超过 %d 天的消息（%s）",
                         _cl["cleanup_time"], _cl["retention_days"],
                         "顺带 VACUUM" if _cl["vacuum"] else "只 checkpoint 收缩 WAL")
        else:
            log.info("数据清理：未启用（保留天数 %d，自动清理 %s）",
                     _cl["retention_days"], _cl["auto_cleanup"])

        # 自动回复模块
        llm_cfg = cfg.get("llm") or {}
        self.replier = Replier.from_yaml(
            cfg.get("replies") or {},
            self.hook,
            REPLY_STATE_PATH,
            llm_base_url=str(llm_cfg.get("base_url") or ""),
            llm_api_key=str(llm_cfg.get("api_key") or ""),
            llm_model=str(llm_cfg.get("model") or ""),
            labels_path=LABELS_PATH,
            sent_tracker=self.sent_tracker,
            history_fn=lambda g, n: self.store.recent(limit=n, group=g),
        )
        if self.replier.cfg.enabled:
            n_test = sum(1 for t in self.replier.cfg.templates if t.test_only)
            n_real = len(self.replier.cfg.templates) - n_test
            log.warning(
                "⚠️ 自动回复已启用: %d 模板（%d 测试模式 / %d 真实发送）— 谨慎使用，封号风险高",
                len(self.replier.cfg.templates), n_test, n_real,
            )
        else:
            log.info("自动回复：禁用（配置 replies.enabled: true 开启）")

        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    def _load_labels(self) -> dict:
        """读取 data/labels.json；文件不存在或解析失败返回空标定。"""
        try:
            self._labels_mtime = LABELS_PATH.stat().st_mtime
        except OSError:
            self._labels_mtime = None
        if not LABELS_PATH.exists():
            return {"groups": {}, "senders": {}}
        try:
            with LABELS_PATH.open(encoding="utf-8") as f:
                data = json.load(f)
            # 防御性：补全结构
            data.setdefault("groups", {})
            data.setdefault("senders", {})
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("读取 labels.json 失败: %s（按无标定处理）", e)
            return {"groups": {}, "senders": {}}

    def _maybe_reload_labels(self) -> bool:
        """labels.json 被改过（Web 标定页 / 昵称自动关联）就重新加载。

        以前标定只在启动时读一次 —— 页面上补完名字还得重启才生效，太别扭。
        这里只在 mtime 变化时重读，代价可忽略。
        """
        try:
            mt = LABELS_PATH.stat().st_mtime
        except OSError:
            return False
        if self._labels_mtime == mt:
            return False
        old_g, old_s = len(self.labels.get("groups", {})), len(self.labels.get("senders", {}))
        self.labels = self._load_labels()
        new_g, new_s = len(self.labels.get("groups", {})), len(self.labels.get("senders", {}))
        log.info("标定已热重载：群 %d→%d，联系人 %d→%d", old_g, new_g, old_s, new_s)
        return True

    def _digest_loop(self) -> None:
        """后台线程：每天到点生成重点发言归档（状态持久化，防重启重复生成）。"""
        state_path = PROJECT_DIR / "data" / "digest_state.json"
        while not self._stop:
            try:
                # 标定被改过就热重载（Web 上点完"挖昵称/套用"立刻生效，不用重启）
                try:
                    self._maybe_reload_labels()
                except Exception as e:  # noqa: BLE001
                    log.debug("标定热重载检查失败: %s", e)
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                hhmm = now.strftime("%H:%M")
                last_date = ""
                if state_path.exists():
                    try:
                        last_date = json.loads(state_path.read_text(encoding="utf-8")).get("last_date", "")
                    except Exception:  # noqa: BLE001
                        pass
                if hhmm >= self.digest_time and last_date != today:
                    from consumer.digest import generate_digest
                    p = generate_digest(today, out_dir=self.digest_out_dir)
                    state_path.write_text(
                        json.dumps({"last_date": today, "last_file": str(p)}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    log.info("每日归档已生成: %s", p)
                    # 归档之后顺带让 LLM 总结（可用 digest.summarize=false 关掉）。
                    # 单独 try：总结失败绝不能影响归档与状态文件。
                    if self.digest_summarize:
                        try:
                            from consumer import summarize as summarize_mod
                            if not summarize_mod.llm_ready():
                                log.info("已跳过自动总结：未配置 LLM（base_url / model 为空）")
                            else:
                                res = summarize_mod.summarize(today, out_dir=self.digest_out_dir)
                                if res is None:
                                    log.info("已跳过自动总结：当天没有进入归档的消息")
                                else:
                                    log.info("自动总结已生成（方式=%s，%d 份文件）: %s",
                                             res.mode, len(res.results),
                                             "、".join(r.path.name for r in res.results))
                                    for e in res.errors:
                                        log.warning("部分会话总结失败：%s", e)
                        except Exception:  # noqa: BLE001
                            log.exception("自动总结失败（归档文件已正常生成，不影响后续运行）")
            except Exception:  # noqa: BLE001
                log.exception("每日归档任务异常")
            # 每 60 秒检查一次
            for _ in range(60):
                if self._stop:
                    return
                time.sleep(1)

    def _cleanup_loop(self) -> None:
        """后台线程：到点清理超过保留天数的消息（R-001）。

        与归档循环同构：60 秒一跳、状态文件防重复。差别是**必须静默**：
        只写日志，不弹窗/不通知；失败也绝不能影响抓取（每轮整体 try 包住）。
        """
        from consumer.cleanup import cleanup_messages, resolve_db_path, should_run

        while not self._stop:
            try:
                ok, why = should_run(self.cfg)
                if ok:
                    sc = self.cleanup_cfg
                    log.info("开始自动清理：保留 %d 天（每天 %s 后一次）",
                             sc["retention_days"], sc["cleanup_time"])
                    res = cleanup_messages(
                        resolve_db_path(sc["path"]),
                        retention_days=sc["retention_days"],
                        vacuum=sc["vacuum"],
                        stop_event=self._stop,
                    )
                    if res.errors and not res.deleted:
                        log.warning("自动清理未完成：%s", "；".join(res.errors))
                else:
                    log.debug("自动清理跳过：%s", why)
            except Exception:  # noqa: BLE001
                log.exception("自动清理任务异常（不影响抓取）")
            for _ in range(60):
                if self._stop:
                    return
                time.sleep(1)

    def _orphan_sweeper_loop(self) -> None:
        """定时扫描未被消息事件认领的语音（多数是自己发出的），转写并归档。"""
        while not self._stop:
            try:
                self._sweep_orphans()
            except Exception:  # noqa: BLE001
                log.exception("孤儿语音处理异常")
            for _ in range(60):
                if self._stop:
                    return
                time.sleep(1)

    def _reverse_conv_map(self) -> dict[str, str]:
        """8 位 hash 前缀 -> 会话 id/名称（标定 + 数据库已知 id + 手工映射）。

        键统一用**前 8 位**，因为语音文件名叫 `<hash>_<ms>.bin` 时 hash 段可能是
        完整 32 位（VoiceTap 抓的）也可能是 8 位（老文件），调用方会先截前 8 位再查
        （见 `_sweep_orphans`）。

        手工映射 `voice.conv_overrides` 同时接受**完整 32 位**和**8 位前缀**两种键
        —— 用户抄文件名时两种都可能，之前只认 32 位、抄 8 位就静默不生效（10-07 修）。
        """
        ids: set[str] = set()
        ids |= set((self.labels.get("groups") or {}).keys())
        ids |= set((self.labels.get("senders") or {}).keys())
        try:
            conn = sqlite3.connect(self.cfg["storage"]["sqlite_path"])
            ids |= {r[0] for r in conn.execute("SELECT DISTINCT group_name FROM messages")}
            ids |= {r[0] for r in conn.execute("SELECT DISTINCT sender FROM messages")}
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        m: dict[str, str] = {}
        for i in ids:
            if not i:
                continue
            h = conv_hash(i)
            m.setdefault(h[:8], i)
            m.setdefault(h, i)          # 完整 hash 也留一份，方便直接对照
        # 手工映射（config: voice.conv_overrides: {hash 或前 8 位: 会话名/wxid}）
        overrides = (self.cfg.get("voice") or {}).get("conv_overrides") or {}
        for h, name in overrides.items():
            k = str(h).strip()
            if not k:
                continue
            m.setdefault(k, str(name))          # 原样（32 位或 8 位都能直接命中）
            m.setdefault(k[:8], str(name))      # 8 位前缀（32 位键也能被前缀查到）
        return m

    @staticmethod
    def _conv_key(h: str) -> str:
        """文件名里的 hash 段 → 查表用的键（统一前 8 位）。"""
        return (h or "").strip()[:8]

    def _sweep_orphans(self) -> None:
        """把孤儿语音解码+转写，落库为合成消息行（direction=out）。"""
        if self.voice_tap is None:
            return
        files = self.voice_tap.unclaimed_files(older_than_s=90)
        if not files:
            return
        rev = self._reverse_conv_map()
        for f in files:
            try:
                h, ms = f.stem.rsplit("_", 1)
                ts = int(ms) // 1000
            except ValueError:
                h, ts = f.stem, int(time.time())
            key = self._conv_key(h)          # 统一按前 8 位查（兼容 32 位/8 位文件名）
            conv_id = rev.get(key) or rev.get(h)
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            wav = f.with_suffix(".wav")
            text = None
            if decode_to_wav(f, wav):
                text = self.asr.transcribe(wav)
            fake_id = f"vt-{f.stem}"
            inserted = self.store.insert_message(
                msg_id=fake_id,
                group_name=conv_id or f"(未知会话:{key})",
                sender=self.self_wxid or "(self)",
                sender_id="",
                content="[语音]",
                msg_type=34,
                received_at=ts,
                priority=0,
                direction="out",
            )
            if inserted is not None and text:
                self.store.update_transcript(fake_id, text)
            self.voice_tap.claim(f)
            log.info("孤儿语音归档: 会话=%s 文件=%d bytes 文字=%s",
                     conv_id or key, size, (text or "(未转写)")[:60])

    def _capture_voice_bg(self, conv_id: str, msg_id: str) -> None:
        """后台线程：抓取语音文件 → silk 解码 → ASR 转写 → 落库。

        优先用 VoiceTap 事件驱动捕获（毫秒级、覆盖收发双向）；
        万一不可用，回退到老的 VoiceTemp 轮询。
        """
        try:
            path = None
            raw_path = None
            if self.voice_tap is not None:
                h = conv_hash(conv_id)
                path = self.voice_tap.wait_for(h, since=time.time() - 120, timeout_s=20)
            if path is None:
                path = capture_voice(self.wechat_dir, conv_id, msg_id, self.voice_dir)
            if not path:
                self.store.update_transcript(msg_id, "[未捕获到语音文件]")
                return
            # 统一复制为 <msg_id>.bin（保持补转脚本的命名约定）
            if path.parent.name == "raw":
                raw_path = path
                dest_bin = self.voice_dir / f"{msg_id}{path.suffix or '.bin'}"
                shutil.copy(path, dest_bin)
                path = dest_bin
            # 认领（孤儿处理器不再重复处理）
            if raw_path is not None and self.voice_tap is not None:
                self.voice_tap.claim(raw_path)
            wav = path.with_suffix(".wav")
            if not decode_to_wav(path, wav):
                self.store.update_transcript(msg_id, "[解码失败]")
                return
            text = self.asr.transcribe(wav)
            if text:
                self.store.update_transcript(msg_id, text)
                log.info("语音转写成功 msg=%s: %s", msg_id, text[:60])
            else:
                self.store.update_transcript(msg_id, "[待转写]")
        except Exception:  # noqa: BLE001
            log.exception("语音捕获线程异常: conv=%s msg=%s", conv_id, msg_id)

    def _compute_priority(self, group_name: str, sender: str) -> tuple[int, str]:
        """返回 (priority, reason)。priority=1 表示命中重点关注。

        规则（互斥，按顺序）：
        1. 群配置了 focus_members（重点成员）→ 只有这些成员在该群的发言算重点
        2. 群标记 important 且无 focus_members → 群内全部发言算重点
        3. 发送人全局标记 important → 其任何群的发言都算重点（叠加）
        """
        reasons: list[str] = []
        g_entry = self.labels.get("groups", {}).get(group_name) or {}
        focus = [m for m in (g_entry.get("focus_members") or []) if m]
        if focus:
            if sender in focus:
                reasons.append(f"重点成员@{g_entry.get('name', group_name)}")
        elif g_entry.get("important"):
            reasons.append(f"群={g_entry.get('name', group_name)}")

        s_entry = self.labels.get("senders", {}).get(sender) or {}
        if s_entry.get("important"):
            reasons.append(f"人={s_entry.get('name', sender)}")

        if reasons:
            return 1, "+".join(reasons)
        return 0, ""

    def stop(self, *_) -> None:
        log.info("收到停止信号，退出中...")
        self._stop = True
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.replier.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.voice_tap is not None:
                self.voice_tap.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---- HTTP server ----

    def _make_handler(self):
        """生成一个知道 self（consumer 实例）的 RequestHandler 子类。"""
        outer = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: A003
                pass  # 关掉默认 access log，我们自己打

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception as e:  # noqa: BLE001
                    log.warning("非 JSON 回调: %s (err=%s)", raw[:200], e)
                    self._reply(400)
                    return
                try:
                    outer.handle_message(payload)
                except Exception:  # noqa: BLE001
                    log.exception("处理回调异常: %r", payload)
                self._reply(200)

            def do_GET(self):  # noqa: N802
                # 健康检查
                self._reply(200, b"consumer ok\n")

            def _reply(self, code: int, body: bytes = b'{"ok":true}\n') -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return CallbackHandler

    def start_server(self) -> None:
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(
            (self.callback_host, self.callback_port), handler
        )
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="consumer-http",
            daemon=True,
        )
        self._server_thread.start()
        log.info(
            "回调 server 已启动: http://%s:%d  (日志: %s)",
            self.callback_host, self.callback_port, LOG_DIR / "consumer.log",
        )

    def register_callback(self) -> None:
        """告诉 DLL 把消息推到哪里。失败抛出 HookError，main() 退出。"""
        url = f"http://{self.callback_host}:{self.callback_port}/hook/callback"
        result = self.hook.set_callback(url)
        log.info("已注册回调: %s -> %s", url, result)

    # ---- 业务 ----

    def handle_message(self, msg: dict) -> None:
        """处理一条 DLL 推送的消息事件。"""
        try:
            # DLL 用 event_type 区分事件类型，1001 是新消息
            # 其他类型（系统消息、撤回等）暂时不处理
            if msg.get("event_type") not in (1001, None):
                return

            msg_type = int(msg.get("type") or 1)
            # 1=文本, 6=文件, 49=链接/卡片, 51=系统消息等
            # 文本入库 + LLM 评分；非文本只入库元信息（不调 LLM）
            is_text = (msg_type == 1)

            sender = msg.get("sender") or ""
            wxid = msg.get("wxid") or ""
            roomid = msg.get("roomid") or ""

            content = (msg.get("content") or "").strip()
            msg_id = str(msg.get("msgid") or "")
            ts = int(msg.get("timestamp") or 0) or None

            if not msg_id:
                return
            # 非文本消息：用占位符填充 content，方便管理平台看到"有这条消息"
            if not is_text and not content:
                content = f"[非文本 type={msg_type}]"
            if not content:
                return

            # 群 vs 私聊
            if roomid:
                # 群消息：DLL 不返回群显示名，先用 roomid 占位
                group_name = roomid
            else:
                # 私聊：用对方 wxid 当 group_name（兼容老 filter）
                group_name = wxid or "(私聊)"

            # 语音消息：立刻启动捕获线程（微信 VoiceTemp 临时文件会被清理，要抢时间）
            if msg_type == 34 and self.voice_enabled and self.wechat_dir:
                threading.Thread(
                    target=self._capture_voice_bg,
                    args=(group_name, msg_id),
                    daemon=True,
                    name="voice-capture",
                ).start()

            # 计算 is_self（是不是自己发的）
            # DLL 字段语义（最终确认的"作者模型"）：
            #   sender 字段 = 消息作者的 wxid（群、私聊都一样）
            #   - 自己发的消息：sender == self_wxid（config.yaml 里配置的本人 wxid）
            #   - 他人发的消息：sender == 对方 wxid
            # 辅以 sent_tracker（记录 consumer 自动回复发出去的消息）
            is_self = bool(self.self_wxid) and sender == self.self_wxid
            if not is_self and self.sent_tracker.is_self_sent(group_name, content, ts=ts or 0):
                is_self = True
            direction = "out" if is_self else "in"

            # 私聊自发消息的归属修正：
            # DLL 对私聊只报"消息作者"，不报"发给了谁"（group_name 也会填成自己）。
            # 若这条是我们（replier）发出去的，sent_tracker 里有目标会话记录 → 修正归属，
            # 这样 store.recent(group=对方wxid) 就能拼出双向私聊历史。
            if is_self and not roomid:
                target = self.sent_tracker.match_any(content, ts=ts or 0)
                if target:
                    log.debug("私聊归属修正: %s -> %s", group_name, target)
                    group_name = target
                else:
                    # 临时诊断：手动发送的私聊消息，观察原始 payload 是否携带会话标识
                    log.info("[RAW-OUT-PRIVATE] %s", json.dumps(msg, ensure_ascii=False)[:500])

            # 1. 入库不再做"白名单过滤"（需求 R-002，2026-10-07 用户确认）：
            #    **所有会话的消息都入库**，只受"入库类型闸门"（表情/系统/撤回等）限制。
            #    监控名单（filter.groups）与关键词（filter.keywords）**不再是入库条件**，
            #    它们只用于归档/总结的范围（见 consumer/digest.py::_monitored_groups 与
            #    _in_digest 的敏感关键词命中）。老行为是"名单外的群连库都进不去"，
            #    导致 9:47 那种"明明在微信里有、系统里查无此条"的困惑（10-07 踩到）。

            # 1.5 自动回复检查（只对"别人发来的"消息触发；自己发的不触发）
            #     注意：回复的生效范围由**模板自己的 scope** 决定（replier._match_scope），
            #     与上面的入库范围无关 —— 入库放开不会让回复变宽。
            if not is_self:
                try:
                    self.replier.maybe_reply(
                        group_name=group_name, sender=sender, content=content,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("replier 处理异常")

            # 2. 计算 priority（命中重点关注群/联系人 → priority=1）
            priority, prio_reason = self._compute_priority(group_name, sender)

            # 3. 入库类型闸门（Web 端可配）：只挡入库，不影响回复与自发追踪
            if msg_type in self.ingest_exclude_types:
                log.debug("入库闸门：类型 %d 已按配置排除，不入库 msg=%s", msg_type, msg_id)
                return

            # 4. 入库
            row_id = self.store.insert_message(
                msg_id=msg_id,
                group_name=group_name,
                sender=sender,
                sender_id="",  # DLL 没单独给 senderId（与 wxid 同义）
                content=content,
                msg_type=msg_type,
                received_at=ts or 0,  # 0 表示"未知时间"，store 会用当前时间
                priority=priority,
                direction=direction,
            )
            if row_id is None:
                log.debug("重复消息，跳过: %s", msg_id)
                return
            if priority >= 1:
                log.info("★入库 [%s] %s: %s (★priority=%d %s)",
                         group_name, sender, content[:50], priority, prio_reason)
            else:
                log.info("入库 [%s] %s: %s", group_name, sender, content[:50])

            # 3. LLM 评分 + 4. 高分推送（只对"别人发来的文本"；自发/非文本跳过，省 token）
            if is_text and not is_self:
                score, score_reason = self.scorer.score(
                    group_name=group_name, sender=sender, content=content
                )
                self.store.update_score(msg_id, score, score_reason)
                log.info("评分 msg=%s score=%d (%s)", msg_id, score, score_reason)

                # score_failed / score_skipped 不参与推送，避免 LLM 故障时批量误推
                score_ok = not score_reason.startswith(("score_failed", "score_skipped"))
                if score_ok and score >= self.push_threshold:
                    title = f"[{score}] {group_name}"
                    body = f"{sender}: {content[:120]}"
                    pushed = self.notifier.push(title=title, body=body, group=group_name)
                    if pushed:
                        self.store.mark_pushed(msg_id)
                        log.info("已推送 msg=%s", msg_id)
            elif is_self:
                log.debug("自发消息，跳过 LLM 评分/推送: %s", msg_id)
            else:
                log.debug("非文本消息（type=%d），跳过 LLM 评分/推送", msg_type)

        except Exception:  # noqa: BLE001
            log.exception("处理消息失败: %r", msg)

    def _wait_for_dll(self, timeout_s: float, interval_s: float = 1.0) -> bool:
        """等 DLL 就绪。app 模式会先启动本进程、再拉起微信，所以必须能等。"""
        deadline = time.time() + timeout_s
        last_log = 0.0
        while time.time() < deadline:
            if self._stop:
                return False
            try:
                log.info("DLL 在线: %s", self.hook.status())
                return True
            except HookError:
                now = time.time()
                if now - last_log >= 10.0:
                    last_log = now
                    remain = int(deadline - now)
                    log.info("等待微信/DLL 就绪…（剩余 %ds）", remain)
                time.sleep(interval_s)
        return False

    def run(self, wait_for_dll: bool = False, dll_timeout_s: float = 600.0) -> None:
        # 启动期：探测 DLL 是否在
        try:
            status = self.hook.status()
            log.info("DLL 在线: %s", status)
        except HookError as e:
            if not wait_for_dll:
                log.error("连不上 DLL: %s", e)
                log.error("请确认微信已启动 + version.dll 在 WeChat 目录", exc_info=False)
                sys.exit(3)
            log.info("DLL 尚未就绪，进入等待（微信还没起来）: %s", e)

        # 先起 HTTP server —— 这样微信一启动我们就能立刻注册回调
        self.start_server()

        if wait_for_dll and not self._wait_for_dll(dll_timeout_s):
            if self._stop:
                log.info("等待期间收到停止信号")
            else:
                log.error("等待 DLL 超时（%.0fs）—— 微信没启动或 version.dll 缺失", dll_timeout_s)
            self.store.close()
            return

        # 注册回调（app 模式下带重试，避免微信刚起来时竞态）
        attempts = 30 if wait_for_dll else 1
        for i in range(attempts):
            try:
                self.register_callback()
                break
            except HookError as e:
                if i == attempts - 1:
                    log.error("注册回调失败: %s", e)
                    if not wait_for_dll:
                        sys.exit(3)
                    self.store.close()
                    return
                time.sleep(1.0)

        # 阻塞直到 stop()
        try:
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
        except ValueError:
            # 非主线程（app 内嵌运行时）装不了信号处理器，由 app 负责调用 stop()
            log.debug("非主线程运行，跳过信号处理器安装")
        log.info("进入消息循环。Ctrl+C 停止。")
        try:
            while not self._stop:
                # serve_forever 跑在另一个线程，主线程 idle 等信号
                # 用 sleep + flag 而不是 join，因为 shutdown 是阻塞的
                import time
                time.sleep(0.5)
        finally:
            log.info("清理资源...")
            self.store.close()


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("配置错误: %s", e)
        sys.exit(2)
    consumer = Consumer(cfg)
    consumer.run()


if __name__ == "__main__":
    main()
