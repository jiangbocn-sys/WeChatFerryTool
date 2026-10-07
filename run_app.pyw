"""双击启动入口。

用 .pyw 扩展名是为了**不弹控制台窗口**；日志全部写到 logs/app.log。
需要看实时输出时改用：.venv\\Scripts\\python.exe -m app.main
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
