"""图片理解：把已归档的图片交给视觉模型描述，供归档/总结使用。

背景（2026-10-08）
------------------
PC 微信的聊天图默认是加密 `.dat`，我们只能拿到**偶发明文**（缓存 `Thumb\\*_thumb.jpg`，
见 `consumer/image_sweeper.py`）。既然能拿到一部分，就让这部分**真正用起来**：
把图片交给视觉模型（MiniMax-M3 原生多模态）描述成文字，写进归档与总结 ——
这样"卦例截图/表格/单据"这类图就能参与理解，而不是一行 `[图片 214×480]` 占位。

设计要点
--------
* **本地缓存**：描述按 `(msg_id, 文件大小, mtime)` 缓存到 `data/image_desc.json`，
  重跑归档/总结**不会重复烧额度**。
* **只描述有明文的图**：`data/images/<msg_id>.jpg` 存在才调；否则跳过（不编造）。
* **失败不致命**：任何异常都只记日志、返回空，绝不影响归档生成。
* **可在设置页开关**：`llm.image_understand`（默认关，避免不知情地花额度）。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("consumer.vision")

#: 描述提示词。**强调"照抄图上的文字与数字"** —— 卦例/表格/单据的价值几乎都在文字上；
#: 同时要求简短，避免把额度花在形容词上。
DESC_PROMPT = (
    "这是一条微信群聊消息里的图片。请用中文简要描述，供后续归档与总结使用。要求：\n"
    "1. **优先照抄图上的文字与数字**（标题、卦爻/干支、金额、日期、单位、表格里的小字）；"
    "看不清的字用「□」代替，不要猜；\n"
    "2. 说明这是什么类型的图（截图/照片/表格/卦盘/聊天记录截图/实物…）；\n"
    "3. 若是照片，一句话说清主体与场景即可；\n"
    "4. 不要评价、不要寒暄，直接输出描述，总长控制在 150 字以内。"
)


def _cache_path(data_root: Path) -> Path:
    return Path(data_root) / "data" / "image_desc.json"


def load_cache(data_root: Path) -> dict:
    p = _cache_path(data_root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_cache(data_root: Path, cache: dict) -> None:
    p = _cache_path(data_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        log.warning("图片描述缓存写入失败: %s", e)


def enabled(llm_cfg: dict) -> bool:
    """是否启用图片理解（设置页的 `llm.image_understand`，默认关）。"""
    return bool((llm_cfg or {}).get("image_understand"))


def describe_image(path: Path, llm_cfg: dict, timeout_s: float = 120.0) -> str:
    """调视觉模型描述一张图；失败返回空串（不抛异常）。"""
    base_url = str(llm_cfg.get("base_url") or "").strip()
    model = str(llm_cfg.get("model") or "").strip()
    api_key = str(llm_cfg.get("api_key") or "").strip() or "not-needed"
    if not (base_url and model):
        raise RuntimeError("未配置 LLM：base_url / model 为空")
    if not path.is_file():
        return ""
    try:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as e:
        log.warning("读图失败 %s: %s", path.name, e)
        return ""
    # 按扩展名给 mime（微信给的临时文件可能没有扩展名，我们统一存成 .jpg）
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s)
    resp = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": DESC_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    )
    return (resp.choices[0].message.content or "").strip()


def get_description(msg_id: str, images_dir: Path, data_root: Path, llm_cfg: dict,
                    cache: dict | None = None, save: bool = True) -> str:
    """取某条消息图片的描述（有缓存就用缓存；没有则调模型并写缓存）。

    返回空串表示"没有图片明文"或"描述失败" —— 调用方据此决定是否标注。
    """
    p = Path(images_dir) / f"{msg_id}.jpg"
    png = Path(images_dir) / f"{msg_id}.png"
    img = p if p.is_file() else (png if png.is_file() else None)
    if img is None:
        return ""
    try:
        st = img.stat()
        key = f"{msg_id}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return ""
    own = cache is None
    if own:
        cache = load_cache(data_root)
    assert cache is not None
    hit = cache.get(key)
    if isinstance(hit, str) and hit.strip():
        return hit.strip()
    try:
        desc = describe_image(img, llm_cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("图片描述失败 msg=%s: %s: %s", msg_id, type(e).__name__, str(e)[:120])
        return ""
    if desc:
        cache[key] = desc
        if save:
            save_cache(data_root, cache)
    return desc


def describe_many(msg_ids: list[str], images_dir: Path, data_root: Path, llm_cfg: dict,
                  limit: int = 20) -> dict[str, str]:
    """批量描述（按顺序、带缓存与上限）。返回 {msg_id: 描述}。"""
    out: dict[str, str] = {}
    if not enabled(llm_cfg):
        return out
    cache = load_cache(data_root)
    n = 0
    for mid in msg_ids:
        if n >= limit:
            log.info("图片理解：已达本次上限 %d 张，其余跳过", limit)
            break
        if not (Path(images_dir) / f"{mid}.jpg").is_file() and \
           not (Path(images_dir) / f"{mid}.png").is_file():
            continue
        desc = get_description(mid, images_dir, data_root, llm_cfg, cache=cache, save=False)
        if desc:
            out[mid] = desc
            n += 1
    if out:
        save_cache(data_root, cache)
        log.info("图片理解：本次描述 %d 张（缓存共 %d 条）", len(out), len(cache))
    return out
