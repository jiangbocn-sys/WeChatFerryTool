"""收尾项回归：E3(digest.exclude_types 默认值) / E4(LLM model 校验) / config_path 口径。

安全约束（本会话教训）：
* 调 `setup.apply_answers()` **一律 dry_run=True**，绝不用临时答案覆盖真实 config.yaml；
* 真实 config.yaml 与账号 labels.json 前后做 sha256 比对，证明没被测试改动。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SCRATCH = Path(r"D:\projects\.wft-final2-scratch")
REAL_CFG = PROJ / "config.yaml"
REAL_LABELS = PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "labels.json"

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


cfg_sha_before = sha(REAL_CFG)
labels_sha_before = sha(REAL_LABELS)

shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "data").mkdir(parents=True, exist_ok=True)
(SCRATCH / "reports").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SCRATCH)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

import paths  # noqa: E402

print("[1] config_path 口径：数据根优先，其次基目录")
check("数据根没有 config.yaml 时回退到基目录", paths.config_path() == PROJ / "config.yaml",
      str(paths.config_path()))

# 在数据根放一份 config.yaml → 必须优先用它
(SCRATCH / "config.yaml").write_text("llm:\n  base_url: https://scratch.example/v1\n  model: scratch-model\n",
                                     encoding="utf-8")
check("数据根有 config.yaml 时优先用它", paths.config_path() == SCRATCH / "config.yaml",
      str(paths.config_path()))

import yaml  # noqa: E402

import setup as setup_mod  # noqa: E402
from consumer import digest as digest_mod  # noqa: E402

print("\n[2] E3：digest.exclude_types 默认值")
cfg = setup_mod.default_config({"run_mode": setup_mod.RUN_MODE_CAPTURE})
# 10-07 起默认只留真噪音（表情/系统），撤回(10002) 归入正文以保住上下文
check("default_config 写出 digest.exclude_types", cfg["digest"].get("exclude_types") == [47, 51, 10000],
      str(cfg["digest"].get("exclude_types")))
check("storage 默认仍只有 47", cfg["storage"].get("ingest_exclude_types") == [47],
      str(cfg["storage"].get("ingest_exclude_types")))

# 配置里没有该键 → 代码兜底；配置里有 → 以配置为准
(SCRATCH / "config.yaml").write_text("digest:\n  enabled: true\n", encoding="utf-8")
check("配置缺该键时用代码兜底", digest_mod._exclude_types() == {47, 51, 10000},
      str(sorted(digest_mod._exclude_types())))
(SCRATCH / "config.yaml").write_text("digest:\n  exclude_types: [47, 51]\n", encoding="utf-8")
check("配置有该键时以配置为准", digest_mod._exclude_types() == {47, 51},
      str(sorted(digest_mod._exclude_types())))
# 真实 config.yaml 读出来必须与默认一致
(SCRATCH / "config.yaml").unlink()
check("真实 config.yaml 的 exclude_types 生效", digest_mod._exclude_types() == {47, 51, 10000},
      str(sorted(digest_mod._exclude_types())))

print("\n[3] E4：LLM model 非空校验（全部 dry_run=True）")
cases = [
    ("只填 key 不填 base_url", {"llm_api_key": "sk-x"}, False),
    ("只填 base_url 不填 key", {"llm_base_url": "https://api.example/v1"}, False),
    ("base_url + key 但 model 空", {"llm_api_key": "sk-x", "llm_base_url": "https://api.example/v1"}, False),
    ("base_url + key + model", {"llm_api_key": "sk-x", "llm_base_url": "https://api.example/v1",
                                "llm_model": "some-model"}, True),
    ("完全留空（不启用 LLM）", {}, True),
]
for name, ans, want_ok in cases:
    ans = dict(ans, run_mode=setup_mod.RUN_MODE_CAPTURE)
    res = setup_mod.apply_answers(ans, dry_run=True)
    check(f"{name} → {'通过' if want_ok else '被挡'}",
          bool(res.get("ok")) is want_ok,
          (res.get("error") or "")[:70] if not res.get("ok") else "")
check("被挡时给出的是人话（提到模型名）",
      "模型名" in (setup_mod.apply_answers(
          {"llm_base_url": "https://api.example/v1", "run_mode": "capture"}, dry_run=True).get("error") or ""))

print("\n[4] dry_run 不落盘 + 真实文件未被改动")
res = setup_mod.apply_answers({"run_mode": setup_mod.RUN_MODE_CAPTURE, "self_wxid": "SHOULD-NOT-WRITE"},
                              dry_run=True)
check("dry_run 返回 content 但不写盘", res.get("dry_run") is True and "content" in res)
check("dry_run 后真实 config.yaml sha256 不变", sha(REAL_CFG) == cfg_sha_before)
check("真实 config.yaml 里没有测试痕迹", "SHOULD-NOT-WRITE" not in REAL_CFG.read_text(encoding="utf-8"))
check("账号 labels.json sha256 不变", sha(REAL_LABELS) == labels_sha_before)
real_cfg = yaml.safe_load(REAL_CFG.read_text(encoding="utf-8"))
check("真实 config.yaml 仍可解析且 llm 段完好",
      real_cfg["llm"]["base_url"].startswith("https://") and real_cfg["llm"]["model"],
      f'{real_cfg["llm"]["base_url"]} / {real_cfg["llm"]["model"]}')
check("真实 config.yaml 已含 digest.exclude_types",
      real_cfg["digest"].get("exclude_types") == [47, 51, 10000],
      str(real_cfg["digest"].get("exclude_types")))

print("\n[5] summarize.strip_think：真实 MiniMax 返回格式")
from consumer.summarize import strip_think  # noqa: E402

closed = "<think>先说一堆推理\n第二行</think>\n- 要点一\n- 要点二"
check("闭合的 think 块被剥掉", strip_think(closed) == "- 要点一\n- 要点二", repr(strip_think(closed)))
unclosed = "<think>推理被截断了，没有闭标签，后面也没有正文"
check("未闭合且无正文 → 空串", strip_think(unclosed) == "", repr(strip_think(unclosed)))
mixed = "正文在前\n<think>后面是推理"
check("think 之后的正文能保住", strip_think(mixed) == "正文在前", repr(strip_think(mixed)))
check("没有 think 时原样返回", strip_think("  - 就一行  ") == "- 就一行")

print("\n" + "=" * 60)
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILS:
    print("FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("E3/E4/config_path/strip_think 回归全部通过")
