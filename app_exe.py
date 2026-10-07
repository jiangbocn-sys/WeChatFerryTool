"""PyInstaller 打包入口。

为什么单独一个脚本：PyInstaller 需要「脚本文件」而不是 `-m 模块` 形式，
而且要在导入其它模块之前把项目根放进 sys.path（冻结后 sys.path 里没有源码目录）。

另外这里要**先**处理 --data-dir：paths.data_root() 在模块导入时就被调用，
等 app.main 的 argparse 跑到就太晚了。
"""
import os
import sys
from pathlib import Path


def _consume_data_dir(argv: list[str]) -> None:
    """把 --data-dir=X / --data-dir X 转成 WCF_DATA_DIR 环境变量并从 argv 移除。"""
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        val = None
        if a == "--data-dir":
            if i + 1 < len(argv):
                val = argv[i + 1]
                i += 2
            else:
                i += 1
        elif a.startswith("--data-dir="):
            val = a.split("=", 1)[1]
            i += 1
        else:
            out.append(a)
            i += 1
        if val:
            p = Path(val).expanduser()
            if p.is_dir():
                os.environ["WCF_DATA_DIR"] = str(p)
            else:
                print(f"--data-dir 不是有效目录: {val}", file=sys.stderr)

    argv[:] = out


if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

_consume_data_dir(sys.argv)

# 提权部署模式：必须早于 app.main 导入（worker 不需要托盘/consumer/Web）
if "--deploy-dll" in sys.argv:
    from deploy import worker_main  # noqa: E402

    sys.exit(worker_main())

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
