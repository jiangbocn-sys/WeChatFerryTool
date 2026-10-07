# 已废弃：临时调试脚本（内容已清空）

原本是排查「总设置页原样提交后 `storage` 段为何变化」用的一次性脚本。
排查结论已固化进两处，不需要这个文件：

* 现象与修复写法：`docs/checkpoint.md` 里「消息库按期自动清理（R-001）」那条的
  ⚠️ `_form_value` 的 MISSING 语义段（`web/app.py` 的 time / number / bool 三个分支）。
* 防回归：`tests/regression/wft_isolation_check.py` 与 `wft_form_check.py`。

⚠️ 这个文件被 ACL 保护，DSH 会话里 shell `Remove-Item` 会被拒（这是已知的沙箱限制）。
在普通 PowerShell 窗口里可以直接删：

```powershell
Remove-Item .\tests\regression\_tmp_dbg_iso.py -Force
```
