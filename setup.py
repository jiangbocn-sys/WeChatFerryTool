"""初始化配置生成 —— 把向导答案写成 config.yaml（安全默认优先）。

安全默认
--------
* `replies.templates` 初始为**空**：即使 `enabled: true`，没有任何模板也发不出东西。
  这是第二道闸门 —— 用户在向导里选了"自动回复"也不等于立刻会说话。
* `replies.enabled` 只有在用户**明确选择**了"抓取+自动回复"模式时才写 true。
* 已有 config.yaml 会先备份成 `config.yaml.bak-<时间戳>`，不直接覆盖。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import paths

RUN_MODE_CAPTURE = "capture"              # 只抓取 / 归档 / 评分
RUN_MODE_CAPTURE_REPLY = "capture_reply"  # 额外启用自动回复（用户明确选择）


def _wizard_templates(ans: dict, replies_on: bool) -> list[dict]:
    """向导只建一个模板（多个模板到 Web「自动回复」页维护）。

    * 未启用自动回复 → 空列表
    * `use_llm: false` → `response` 是**固定回复**，原样发出，完全不碰 LLM
    * `use_llm: true`  → `response` 是**人设**；另给 `fallback` 作 LLM 不可用时的固定兜底
      （绝不能拿人设去兜底，那会"破功"）
    """
    if not replies_on:
        return []
    t = ans.get("template") or {}
    trigger = str(t.get("trigger") or "").strip()
    if not trigger:
        return []

    use_llm = bool(t.get("use_llm"))
    response = str(t.get("response") or "").strip()
    if not response:
        response = ("你是这个微信号的主人，语气随意、简短地回复。"
                    if use_llm else "我暂时离开，稍后回复")

    tpl: dict = {
        "name": "wizard-1",
        "trigger": trigger,
        "match_type": str(t.get("match_type") or "substring"),
        "response": response,
        "use_llm": use_llm,
        "test_only": bool(t.get("test_only", True)),
        "scope": {"groups": [], "senders": []},
    }
    if use_llm:
        tpl["fallback"] = (str(t.get("fallback") or "").strip()
                           or "我现在不方便，稍后回你")
    return [tpl]


def default_config(ans: dict) -> dict:
    """按答案生成完整配置。ans 缺项一律走保守默认。"""
    mode = str(ans.get("run_mode") or RUN_MODE_CAPTURE)
    replies_on = mode == RUN_MODE_CAPTURE_REPLY

    return {
        "hook": {
            "api_base": "http://127.0.0.1:30001",
            "callback_host": "127.0.0.1",
            "callback_port": 8888,
            "api_timeout_s": 5.0,
            # 留空：账号隔离模式下由账号目录名自动推导（更可靠，见 paths.slug_alias）
            "self_wxid": str(ans.get("self_wxid") or ""),
        },
        "filter": {
            "groups": [],
            "senders": [],
            "keywords": [],
            "case_insensitive": True,
        },
        "llm": {
            # base_url / model **不再默认成某一家** —— 用户可能用 OpenAI / DeepSeek /
            # 通义 / 月之暗面 / 本地 Ollama。客户端层是 OpenAI 兼容协议，base_url 直接透传，
            # 所以换 provider 不需要改代码，只需要在这里填对。
            # 留空 = 不启用 LLM：消息照常抓取入库，只是不做评分。
            "base_url": str(ans.get("llm_base_url") or "").strip(),
            "api_key": str(ans.get("llm_api_key") or "").strip(),
            "model": str(ans.get("llm_model") or "").strip(),
            "push_threshold": 4,
            # 逐条 LLM 评分：默认关闭（只给 Bark 推送做阈值判断；回复/归档都不依赖它）
            "score_enabled": False,
        },
        "bark": {"enabled": False, "server": "https://api.day.app", "key": "REPLACE_ME"},
        "storage": {
            "sqlite_path": "data/messages.db",
            # 入库类型闸门：这些 msg_type 不入库（**表情 47 恒排除**，代码里还会强制加回）。
            # 其余分类在 Web「消息分类」页勾选，保存后由 consumer 下次处理消息时生效。
            "ingest_exclude_types": [47],
            # 消息库按期自动清理（R-001）：0 = 不自动清理（默认，先观察再打开）。
            # 打开后建议 90 天；1~6 天会被判为非法、不执行。见 docs/requirements.md。
            "retention_days": 0,
            "auto_cleanup": True,
            "cleanup_time": "00:00",
            "cleanup_vacuum": False,
        },
        "voice": {"enabled": True, "dir": "data/voices"},
        "asr": {
            "enabled": bool(ans.get("asr_enabled")),
            "url": str(ans.get("asr_url") or ""),
            "timeout_s": 120,
        },
        "digest": {
            "enabled": True,
            "time": str(ans.get("digest_time") or "23:30"),
            "dir": "reports",
            # 归档后顺带让 LLM 写当日总结（没配 LLM 时消费者会自动跳过，不会报错）
            "summarize": True,
            # 总结方式：per_group = 每个群单独一份 summary-<日期>-<群名>.md（每群一次调用）；
            #           combined  = 所有群合并成一份 summary-<日期>.md（只调一次）
            "summarize_mode": "per_group",
            # 归档排除的消息类型：表情(47)/系统(51、10000)/撤回(10002)。
            # 与「入库闸门」storage.ingest_exclude_types 是两件事：这里决定"进不进归档"，
            # 改完可以直接重跑当天归档，不需要重新入库。
            "exclude_types": [47, 51, 10000, 10002],
        },
        "replies": {
            "enabled": replies_on,
            "history_count": 10,
            # 初始为空或向导建的那一个；没有模板就不可能发出消息
            "templates": _wizard_templates(ans, replies_on),
            "rate_limit": {
                "per_group_cooldown_s": 15,
                "global_daily_limit": 100,
                "min_delay_s": 5,
                "max_delay_s": 30,
            },
        },
        "app": {
            "launch_wechat": True,
            "auto_inject_keyhook": bool(ans.get("capture_keys")),
            "web_host": "127.0.0.1",
            "web_port": 6060,
            "run_mode": mode,
            "initialized_at": int(time.time()),
            "initialized_by": "setup-wizard",
        },
    }


KEEP_API_KEY = "__KEEP__"   # 前端在"不修改已保存的 key"时回传的哨兵值


def _existing_llm() -> dict:
    """读当前已保存的 llm 段（读不到就空 dict）。只读，不创建任何东西。"""
    try:
        import yaml
        p = paths.config_path()
        if not p.is_file():
            return {}
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cfg.get("llm") or {}
    except Exception:  # noqa: BLE001
        return {}


def apply_answers(ans: dict, dry_run: bool = False) -> dict:
    """写 config.yaml（写基目录，即全局配置）。返回结果摘要。

    ``dry_run=True`` 时**只计算不落盘**，并在返回值里给出 ``content``。
    这是给测试用的：避免"忘了把数据根重定向到临时目录"就把真实配置覆盖掉。
    """
    llm_key = str(ans.get("llm_api_key") or "").strip()
    llm_base = str(ans.get("llm_base_url") or "").strip()
    llm_model = str(ans.get("llm_model") or "").strip()

    # 向导页出于安全不回显 key。若用户没重填（空 / 哨兵值），保留已保存的 key，
    # 免得"再点一次保存"把 key 抹掉。
    # 但**只在他还填着 base_url 时才继承** —— base_url 留空 = 明确不要 LLM，
    # 这时若继承旧 key 会被下面的校验挡下（"填了 key 却没 base_url"），语义混乱。
    if llm_base and llm_key in ("", KEEP_API_KEY):
        old_key = str(_existing_llm().get("api_key") or "").strip()
        if old_key:
            llm_key = old_key
            ans = dict(ans, llm_api_key=old_key)      # 让 default_config 也拿到

    if llm_key and not llm_base:
        return {"ok": False,
                "error": "填了 LLM API Key 就必须同时填 base_url"
                         "（OpenAI 兼容接口地址，例如 https://api.deepseek.com/v1 "
                         "或 https://api.openai.com/v1）"}
    # 配了接口地址却没有模型名 = 运行时必然失败（scorer/summarize 都会报"未配置模型名"），
    # 之前会静默写出 model=''，所以这里挡在写入前。
    if llm_base and not llm_model:
        return {"ok": False,
                "error": "填了 LLM base_url 就必须填模型名（model），"
                         "例如 MiniMax-M3 / deepseek-chat / gpt-4o-mini"}

    cfg = default_config(ans)
    target = paths.base_root() / "config.yaml"

    backup = None
    if not dry_run and target.is_file():
        backup = target.with_name(f"config.yaml.bak-{int(time.time())}")
        try:
            shutil.copy2(target, backup)
        except OSError:
            backup = None

    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        return {"ok": False, "error": f"缺少 pyyaml: {e}"}

    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
    header = (
        "# 由初始化向导生成。\n"
        "# 说明：hook.self_wxid 留空时由微信账号目录名自动推导（更可靠）。\n"
        "# 自动回复模板默认为空 —— 没有模板就不会回复任何消息。\n\n"
    )
    if dry_run:
        # 只算不写：测试专用，避免误写真实配置
        return {
            "ok": True,
            "dry_run": True,
            "path": str(target),
            "content": header + text,
            "run_mode": cfg["app"]["run_mode"],
            "replies_enabled": cfg["replies"]["enabled"],
            "templates": len(cfg["replies"]["templates"]),
        }

    try:
        target.write_text(header + text, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"写入失败: {e}"}

    return {
        "ok": True,
        "path": str(target),
        "backup": str(backup) if backup else None,
        "run_mode": cfg["app"]["run_mode"],
        "replies_enabled": cfg["replies"]["enabled"],
        "templates": len(cfg["replies"]["templates"]),
        "restart_required": True,
    }


def peek_answers_from_report(rep: dict) -> dict:
    """预填问卷：**先读已保存的配置**，读不到才用采集结果的保守默认。

    以前这里无条件返回空值，导致"保存过配置再点初始化，页面还是空的" ——
    看着像没保存成功。注意 api_key **不回显**（安全），只回一个
    `llm_api_key_saved` 标记让页面提示"已保存，留空=不修改"。
    """
    acc = rep.get("account") or {}
    old = _existing_llm()
    old_key = str(old.get("api_key") or "").strip()

    run_mode = RUN_MODE_CAPTURE
    try:
        import yaml
        p = paths.config_path()
        if p.is_file():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            run_mode = str(((cfg.get("app") or {}).get("run_mode")) or run_mode)
            digest_time = str(((cfg.get("digest") or {}).get("time")) or "23:30")
            asr_enabled = bool((cfg.get("asr") or {}).get("enabled"))
        else:
            digest_time, asr_enabled = "23:30", False
    except Exception:  # noqa: BLE001
        digest_time, asr_enabled = "23:30", False

    return {
        "run_mode": run_mode,                  # 已保存的就按已保存的显示
        "self_wxid": str(old.get("self_wxid") or "") or acc.get("self_wxid") or "",
        "digest_time": digest_time,
        "llm_base_url": str(old.get("base_url") or ""),
        "llm_model": str(old.get("model") or ""),
        "llm_api_key": "",                     # 安全：永不回显
        "llm_api_key_saved": bool(old_key),    # 但告诉页面"已保存"
        "llm_api_key_hint": (f"{old_key[:6]}…{old_key[-4:]}" if len(old_key) > 12 else ""),
        "capture_keys": False,
        "asr_enabled": asr_enabled,
        "config_path": str(paths.config_path()),
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    import setup_env
    rep = setup_env.collect_report()
    print(json.dumps({"peek": peek_answers_from_report(rep),
                      "config_ready": rep["config"]["ready"]},
                     ensure_ascii=False, indent=2))
