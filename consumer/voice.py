"""语音消息捕获 + 解码模块。

原理（已实测验证）：
- 微信 4.x PC 收到语音时，会自动下载到临时目录：
    <微信数据目录>/<账号>/cache/<YYYY-MM>/Message/<md5(会话ID)>/VoiceTemp/<文件>
- 目录名 = md5(会话 roomid 或 对方 wxid)，实测匹配
- 临时文件会被微信清理，所以收到语音事件后必须**立即**抓取（轮询等它出现，几秒内）

流程：
1. capture(roomid, msg_id) —— 轮询 VoiceTemp，把新文件复制到我们的存档目录
2. decode_to_wav(silk_path, wav_path) —— pysilk 解码 silk → 16kHz 单声道 WAV
3. （ASR 转写在 asr.py，后端可插拔）
"""
import hashlib
import logging
import shutil
import time
import wave
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---- 微信数据目录定位 ----

def _documents_dir() -> Path:
    """取 Windows「文档」目录（可能被重定向到 D 盘等）。"""
    try:
        import winreg  # Windows only
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        val, _ = winreg.QueryValueEx(key, "Personal")
        return Path(str(val).replace("%USERPROFILE%", str(Path.home())))
    except Exception:  # noqa: BLE001
        return Path.home() / "Documents"


def _fallback_roots() -> list[Path]:
    """兜底候选：Documents 被重定向到其他盘的自定义目录时使用。

    不硬编码任何用户名或盘符：按 <盘>:/<用户目录>/Documents 与 <盘>:/Documents 逐个探测。
    """
    cands: list[Path] = [Path.home() / "Documents" / "xwechat_files"]
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        base = Path(f"{letter}:/")
        if not base.is_dir():
            continue
        cands.append(base / "Documents" / "xwechat_files")
        try:
            subs = [d for d in base.iterdir() if d.is_dir()]
        except OSError:  # 无权限的盘/目录直接跳过
            continue
        cands.extend(sub / "Documents" / "xwechat_files" for sub in subs)
    return cands


def find_wechat_files_dir(account_wxid: str) -> Optional[Path]:
    """定位微信账号数据目录 <文档>/xwechat_files/<wxid>_xxxx。

    account_wxid 例如 "wxid_xxxxxxxx"（config.yaml 里的 self_wxid）。
    """
    root = _documents_dir() / "xwechat_files"
    if not root.is_dir():
        # 兜底：Documents 可能被重定向到其他盘
        for cand in _fallback_roots():
            if cand.is_dir():
                root = cand
                break
        else:
            return None
    if account_wxid:
        for d in root.iterdir():
            if d.is_dir() and d.name.startswith(account_wxid):
                return d
    # 没有匹配时返回第一个账号目录
    for d in root.iterdir():
        if d.is_dir() and d.name not in ("all_users", "Backup"):
            return d
    return None


def _voicetemp_dirs(account_dir: Path, conv_id: str) -> list[Path]:
    """返回该会话所有月份的 VoiceTemp 目录（通常只有当月或上月的）。"""
    conv_hash = hashlib.md5(conv_id.encode("utf-8")).hexdigest()
    out: list[Path] = []
    cache = account_dir / "cache"
    if not cache.is_dir():
        return out
    for month_dir in sorted(cache.iterdir(), reverse=True):
        vt = month_dir / "Message" / conv_hash / "VoiceTemp"
        if vt.is_dir():
            out.append(vt)
    return out


# ---- 捕获 ----

# 已抓取过的源文件指纹（路径+mtime），避免同会话连续语音时重复抓同一个文件
_captured_sources: dict[str, float] = {}


def capture_voice(
    account_dir: Path,
    conv_id: str,
    msg_id: str,
    dest_dir: Path,
    wait_s: float = 20.0,
    fresh_s: float = 180.0,
    poll_interval: float = 0.5,
    near_ts: int | None = None,
    tol_s: int = 30,
) -> Optional[Path]:
    """在 VoiceTemp 里等待并抓取**属于这条消息的**语音文件（微信会清理临时文件）。

    :param conv_id: 会话 ID（group roomid 或私聊对方 wxid，= 消息的 group_name）
    :param fresh_s: 只接受最近 fresh_s 秒内被写入的文件（避免抓到旧残留）
    :param near_ts: 该消息的秒级时间戳。**强烈建议传** —— VoiceTemp 文件名后缀就是语音的
        秒级时间戳，用它把"时间最接近"的文件挑出来；不传就只能取最新的，
        短时间内多条语音会抢同一个文件。
    :param tol_s: 与 `near_ts` 的允许差值（秒）。
    :return: 复制到的目标路径；失败返回 None

    ⚠️ 2026-10-08 修的严重 bug：原来只按"最新"取、且取完不记账，
    导致**三条时长/aeskey 完全不同的语音**被配到同一个文件、转写成同一段文字（内容全错）。
    现在改为"按时间戳最接近 + 取走即记账（同一文件不再给第二条消息）"。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        now = time.time()
        for vt in _voicetemp_dirs(account_dir, conv_id):
            try:
                cands = []
                for f in vt.iterdir():
                    if not f.is_file():
                        continue
                    st = f.stat()
                    if now - st.st_mtime >= fresh_s:
                        continue
                    if f"{f}|{st.st_mtime_ns}" in _captured_sources:
                        continue
                    cands.append((f, st))
            except OSError:
                continue
            if not cands:
                continue
            # 挑"与消息时间最接近"的文件；没有 near_ts 时退回"最新"
            if near_ts:
                def _dist(item):
                    f, _st = item
                    try:
                        return abs(int(f.name.split("_")[-1]) - int(near_ts))
                    except (ValueError, IndexError):
                        return 10 ** 9
                cands.sort(key=_dist)
                best, best_st = cands[0]
                if _dist((best, best_st)) > tol_s:
                    time.sleep(poll_interval)
                    continue                     # 时间对不上，继续等（别的文件可能稍后出现）
            else:
                best, best_st = max(cands, key=lambda it: it[1].st_mtime)
            suffix = best.suffix or ".bin"
            dest = dest_dir / f"{msg_id}{suffix}"
            try:
                shutil.copy2(best, dest)
                _captured_sources[f"{best}|{best_st.st_mtime_ns}"] = now
                log.info("语音已捕获: %s -> %s (%d bytes)", best.name, dest, dest.stat().st_size)
                return dest
            except OSError as e:
                log.warning("复制语音文件失败: %s", e)
                return None
        time.sleep(poll_interval)
    log.warning("语音捕获超时（%ss 内未在 VoiceTemp 发现新文件）: conv=%s msg=%s", wait_s, conv_id, msg_id)
    return None


# ---- 解码 ----

def decode_to_wav(src: Path, wav_path: Path, sample_rate: int = 16000) -> bool:
    """silk(或amr) → 16kHz 单声道 WAV。

    微信缓存里的语音文件通常是 silk（可能缺头），pysilk 解码。
    实测 pysilk 编码产物 magic = b"\\x02#!SILK_V3"。
    """
    try:
        import pysilk  # silk-python 包，pip install silk-python
    except ImportError:
        log.error("pysilk 不可用，请 pip install silk-python")
        return False

    raw = src.read_bytes()
    if raw.startswith(b"#!AMR"):
        log.warning("这是 AMR 文件（非 silk），暂不支持: %s", src.name)
        return False
    # 没有 silk 头的裸流 → 补头（腾讯变体前缀 \\x02）
    if not raw.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        raw = b"\x02#!SILK_V3" + raw

    try:
        from io import BytesIO
        fi = BytesIO(raw)
        out = BytesIO()
        pysilk.decode(fi, out, sample_rate)  # PCM 写入 out 流（16-bit）
        pcm = out.getvalue()
        if len(pcm) < 100:
            log.warning("解码产物过短（%d bytes），可能不是有效 silk: %s", len(pcm), src.name)
            return False
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        log.info("语音解码成功: %s -> %s (%d bytes PCM)", src.name, wav_path.name, len(pcm))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("silk 解码失败 (%s): %s", src.name, e)
        return False


def voice_duration_s(content_xml: str) -> int:
    """从语音消息 XML 里解析时长（毫秒→秒，向上取整）。失败返回 0。"""
    import re
    m = re.search(r'voicelength="(\d+)"', content_xml or "")
    if m:
        return max(1, int(int(m.group(1)) / 1000 + 0.5))
    return 0