"""知识库 CLI：抽取 / 蒸馏 / 问答预演 / 状态查看。

四个子命令（都可安全预演）：

  extract   只抽文字，报告每个文件抽了多少字、哪些失败/跳过（**不调 LLM**）
  distill   增量蒸馏成笔记。**默认预演**，加 --yes 才真调 LLM 并写文件
  ask       用知识库回答一个问题（打印回答 + 命中来源 + 自检结果，**不发送**）
  status    看台账：已蒸馏哪些文件、字数、上次蒸馏时间

用法：
    python tools/kb.py extract  --group 弟子群 --dir "D:\\JiangBo\\Documents\\wechatdocs\\弟子群"
    python tools/kb.py distill  --group 弟子群 --dir "..."          # 预演
    python tools/kb.py distill  --group 弟子群 --dir "..." --yes    # 真跑
    python tools/kb.py ask      --group 弟子群 --q "@小易 寅午戌能三合吗"
    python tools/kb.py status   --group 弟子群
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))


def _out(s: str = "") -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    print(s)


def _data_root() -> Path:
    import paths
    return paths.data_root()


def cmd_extract(a) -> int:
    from consumer import kb_extract as EX
    rep = EX.extract_dir(Path(a.dir), tuple(a.ext.split(",")))
    _out(f"\n资料目录: {a.dir}")
    _out(f"  成功 {len(rep.ok)} 个，合计 {rep.total_chars:,} 字"
         f"（失败 {len(rep.failed)}、跳过 {len(rep.skipped)}）")
    for e in rep.ok:
        w = f"  ⚠️ {e.warning}" if e.warning else ""
        _out(f"    ✅ [{e.method:<9}] {e.chars:>8,} 字  {e.path.name}{w}")
    for p, w in rep.warnings:
        _out(f"    ⚠️  {p.name}: {w}")
    for p, r in rep.skipped:
        _out(f"    ⏭️  跳过 {p.name}: {r}")
    for p, r in rep.failed:
        _out(f"    ❌ 失败 {p.name}: {r}")
    return 0 if not rep.failed else 1


def cmd_distill(a) -> int:
    from consumer.kb_notes import distill_group, summarize_report
    llm = {}
    if a.yes:
        from consumer.summarize import _llm_cfg, llm_ready
        if not llm_ready():
            _out("❌ 未配置 LLM（设置页填 base_url 与模型名后再来）")
            return 2
        llm = _llm_cfg()
    rep = distill_group(Path(a.dir), a.group, _data_root(), llm,
                        allow_ext=tuple(a.ext.split(",")),
                        dry_run=not a.yes,
                        progress=(_out if a.verbose else None))
    _out()
    _out(summarize_report(rep))
    if not a.yes and rep.plan_calls:
        _out(f"\n⚠️ 以上是**预演**：确认无误后加 --yes 真正蒸馏（将调用 LLM {rep.plan_calls} 次）")
    return 0 if not rep.failed else 1


def cmd_ask(a) -> int:
    from consumer.kb_answer import answer_question
    res = answer_question(a.q, a.group, _data_root(), top_k=a.top_k,
                          kb_dir=_kb_dir_of(a.group) or None, verbose=True)
    _out()
    _out("=" * 70)
    _out(f"问题：{res.question}")
    _out(f"命中片段：{len(res.hits)} 段")
    for h in res.hits:
        _out(f"  - 〔{h.note_file}〕{h.topic}（相关度 {h.score:.2f}）")
    _out(f"LLM 调用：{res.llm_calls} 次")
    if res.grounded is not None:
        _out(f"自检：{'通过（无越界内容）' if res.grounded else '**发现越界内容，已裁剪/兜底**'}")
    if res.dropped:
        _out(f"  自检删掉的内容：{res.dropped[:200]}")
    _out("-" * 70)
    _out("回答：")
    _out(res.answer or "（无）")
    return 0


def _kb_dir_of(group_name: str) -> str:
    """从 labels.json 取该群的资料目录（多个群可能共享同一套笔记）。"""
    import json
    try:
        import paths
        lab = json.loads((paths.data_root() / "data" / "labels.json").read_text(encoding="utf-8"))
        for _gid, v in (lab.get("groups") or {}).items():
            if str((v or {}).get("name") or _gid) == group_name:
                return str((v or {}).get("kb_dir") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def cmd_status(a) -> int:
    from consumer.kb_notes import kb_root, load_sources
    root = kb_root(_data_root(), a.group, _kb_dir_of(a.group))
    entries = load_sources(root)
    _out(f"\n群「{a.group}」知识库: {root}")
    if not entries:
        _out("  （台账为空：还没蒸馏过）")
        return 0
    done = [e for e in entries.values() if e.note_file]
    _out(f"  资料 {len(entries)} 份（已蒸馏 {len(done)}）")
    for rel, e in sorted(entries.items()):
        flag = "✅" if e.note_file else "⏳"
        warn = f"  ⚠️ {e.warning}" if e.warning else ""
        _out(f"    {flag} {rel}: {e.chars:,} 字 → {e.note_file or '（未蒸馏）'}"
             f"  上次 {e.distilled_at or '-'}{warn}")
    idx = root / "_index.md"
    if idx.is_file():
        _out(f"  索引: {idx}")
    return 0


def _load_yaml(p):
    import yaml
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _save_yaml(p, data) -> None:
    import yaml
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def cmd_setup(a) -> int:
    """按群配置知识库：加入监控名单 + 写 `kb_dir/kb_at/kb_on`（+ 可选开总闸）。

    为什么需要它：监控页**只显示监控名单里**的群，名单外的群根本没法在页面上配。
    """
    import json
    from pathlib import Path as _P
    import paths
    drop = _data_root()
    cfg_path = paths.config_path()
    labels_path = drop / "data" / "labels.json"

    gid = a.group
    if a.dir and not _P(a.dir).is_dir():
        _out(f"❌ 资料目录不存在：{a.dir}")
        return 2

    # ---- labels.json：群级字段 ----
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.is_file() else {}
    groups = labels.setdefault("groups", {})
    ent = groups.setdefault(gid, {})
    ent.setdefault("name", a.name or gid)
    ent["monitored"] = True
    if a.dir:
        ent["kb_dir"] = a.dir
    if a.at:
        ent["kb_at"] = a.at
    if a.on:
        ent["kb_on"] = True
    if a.off:
        ent.pop("kb_on", None)
    if a.name:
        ent["name"] = a.name
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
    _out(f"✅ 标定已更新：{labels_path}")
    _out(f"   群 {gid}  name={ent.get('name')!r}  monitored=True"
         f"  kb_on={ent.get('kb_on')}  kb_at={ent.get('kb_at')!r}")
    if a.dir:
        _out(f"   kb_dir={ent.get('kb_dir')!r}")

    # ---- config.yaml：加入监控名单 + 可选开总闸 ----
    cfg = _load_yaml(cfg_path)
    filt = cfg.setdefault("filter", {})
    glist = [str(x) for x in (filt.get("groups") or [])]
    if gid not in glist:
        glist.append(gid)
        filt["groups"] = glist
        _out(f"  已加入监控名单 filter.groups（现 {len(glist)} 个群）")
    else:
        _out("  已在监控名单里")
    if a.enable_kb:
        cfg.setdefault("replies", {}).setdefault("kb", {})["enabled"] = True
        _out("  已打开知识库总闸 replies.kb.enabled=true")
    if a.test:
        cfg.setdefault("replies", {}).setdefault("kb", {})["test_only"] = True
        _out("  已设 replies.kb.test_only=true（只记日志不发）")
    elif a.real:
        cfg.setdefault("replies", {}).setdefault("kb", {})["test_only"] = False
        _out("  ⚠️ 已设 replies.kb.test_only=false —— **真实发送**")
    _save_yaml(cfg_path, cfg)
    _out(f"✅ 配置已更新：{cfg_path}")
    return 0


def cmd_check(a) -> int:
    """配置自检：把「页面上要开的东西」逐项核对，避免开了却因为漏配而不生效。"""
    import json
    import yaml
    import paths
    from consumer.kb_notes import kb_root, load_sources
    drop = _data_root()
    cfg = yaml.safe_load(paths.config_path().read_text(encoding="utf-8")) or {}
    labels_path = drop / "data" / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.is_file() else {}
    kb = ((cfg.get("replies") or {}).get("kb") or {})
    _out(f"\n=== 知识库配置自检 ===")
    ok = True

    def line(cond, text, fix=""):
        nonlocal ok
        _out(f"  {'✅' if cond else '❌'} {text}")
        if not cond:
            ok = False
            if fix:
                _out(f"       修：{fix}")

    line(bool(kb.get("enabled")), f"总闸 replies.kb.enabled = {kb.get('enabled')}",
         "tools/kb.py setup --enable-kb")
    line("test_only" in kb, f"test_only = {kb.get('test_only', '(未设→默认 True 测试模式)')}")
    _out(f"     冷却 {kb.get('cooldown_s', 120)}s ｜ 延迟 "
         f"{kb.get('delay_min_s', 5)}~{kb.get('delay_max_s', 10)}s ｜ "
         f"队列 {kb.get('max_queue', 10)} ｜ 自检 {kb.get('self_check', True)}")
    line(bool((cfg.get("llm") or {}).get("base_url")), "LLM 已配置（base_url + model）")
    line(bool((cfg.get("llm") or {}).get("model")), "LLM 模型名已配置")
    _out("")
    mon = [str(x) for x in ((cfg.get("filter") or {}).get("groups") or [])]
    groups = labels.get("groups") or {}
    targets = [g for g, v in groups.items() if (v or {}).get("kb_on")]
    line(bool(targets), f"有 {len(targets)} 个群勾选了「启用问答」")
    for g in targets:
        v = groups.get(g) or {}
        nm = v.get("name") or g
        ind = "  "
        _out(f"    · {nm}（{g}）")
        _out(f"{ind}  虚拟号名 kb_at = {v.get('kb_at') or '(未设→默认「小易」)'}")
        d = v.get("kb_dir")
        if not d:
            line(False, f"{ind}  资料目录 kb_dir 未设置", f"setup --group {g} --dir <目录>")
            continue
        from pathlib import Path as _P
        line(_P(d).is_dir(), f"{ind}  资料目录存在：{d}")
        entries = load_sources(kb_root(drop, nm, d))
        ndone = sum(1 for e in entries.values() if e.note_file)
        line(ndone > 0, f"{ind}  已蒸馏 {ndone} 份资料（台账 {len(entries)} 条）",
             f"tools/kb.py distill --group {nm} --dir {d} --yes")
        line(g in mon, f"{ind}  在监控名单里（否则页面上看不到）",
             f"setup --group {g} --dir ...")
    _out("")
    _out("结论：" + ("✅ 配置齐了，重启 app 后生效" if ok else "❌ 有项目没配好（见上面 ❌）"))
    return 0 if ok else 1


def cmd_migrate(a) -> int:
    """把旧的「按群名」笔记目录改成「按资料目录」命名（多群共用一份资料不再重蒸）。"""
    import json
    import paths
    from consumer.kb_notes import migrate_to_dir_keyed
    lab = json.loads((paths.data_root() / "data" / "labels.json").read_text(encoding="utf-8"))
    groups = [(str((v or {}).get("name") or g), str((v or {}).get("kb_dir") or ""))
              for g, v in (lab.get("groups") or {}).items()]
    bases = [Path(d).parent for _n, d in groups if d]
    if not bases and a.base:
        bases = [Path(x) for x in a.base]
    _out(f"\n知识库目录迁移（{'**执行**' if a.apply else '预演'}）")
    _out(f"  资料根目录：{', '.join(str(b) for b in bases) or '（无，用 --base 指定）'}")
    lines = migrate_to_dir_keyed(_data_root(), bases, apply=a.apply)
    for ln in lines:
        _out("    " + ln)
    if not a.apply:
        _out("\n⚠️ 以上是**预演**：确认无误后加 --apply 真正改名")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="群知识库工具（抽取 / 蒸馏 / 问答预演 / 状态）")
    ap.add_argument("--verbose", action="store_true", help="打印每一步细节")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="只抽文字（不调 LLM）")
    p.add_argument("--dir", required=True)
    p.add_argument("--ext", default=",".join(
        (".txt", ".md", ".csv", ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx")))
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("distill", help="增量蒸馏（默认预演）")
    p.add_argument("--group", required=True, help="群名（笔记按群存放）")
    p.add_argument("--dir", required=True, help="该群的资料目录")
    p.add_argument("--yes", action="store_true", help="真正调用 LLM 并写文件")
    p.add_argument("--ext", default=",".join(
        (".txt", ".md", ".csv", ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx")))
    p.set_defaults(func=cmd_distill)

    p = sub.add_parser("ask", help="用知识库回答问题（不发送到微信）")
    p.add_argument("--group", required=True)
    p.add_argument("--q", required=True, help="问题（可带 @小易 前缀，会被剥掉）")
    p.add_argument("--top-k", type=int, default=5, help="检索多少段")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("status", help="看台账")
    p.add_argument("--group", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("setup", help="按群配置知识库（加入监控 + 资料目录 + 虚拟号名 + 开关）")
    p.add_argument("--group", required=True, help="群 id（roomid）")
    p.add_argument("--name", default="", help="群名（用于知识库目录；建议填）")
    p.add_argument("--dir", default="", help="该群的资料目录")
    p.add_argument("--at", default="", help="虚拟号名（默认「小易」）")
    p.add_argument("--on", action="store_true", help="勾选该群「启用问答」")
    p.add_argument("--off", action="store_true", help="取消该群「启用问答」")
    p.add_argument("--enable-kb", action="store_true", help="同时打开总闸 replies.kb.enabled")
    p.add_argument("--test", action="store_true", help="设 test_only=true（只记日志不发）")
    p.add_argument("--real", action="store_true", help="⚠️ 设 test_only=false（真实发送）")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("check", help="配置自检（逐项核对，避免漏配）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("migrate", help="把笔记目录改成「按资料目录」命名（多群共用一份）")
    p.add_argument("--apply", action="store_true", help="真正改名（默认预演）")
    p.add_argument("--base", action="append", default=[],
                   help="资料根目录（可多次）；不给则从各群 kb_dir 推导")
    p.set_defaults(func=cmd_migrate)

    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return int(a.func(a) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
