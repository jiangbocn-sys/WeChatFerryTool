"""直接测试 send_text 链路：绕过模板匹配，手动发一条消息到指定群/私聊。

用法：
  python scripts/test_send.py <to_wxid> "<message>"
  例: python scripts/test_send.py 195940014@chatroom "测试消息 by bobo"
"""
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from consumer.hook_client import HookClient, HookConfig, HookError


def main():
    if len(sys.argv) < 3:
        print("用法: python scripts/test_send.py <to_wxid> <message>")
        print("例: python scripts/test_send.py 195940014@chatroom \"测试消息\"")
        sys.exit(1)
    to_wxid = sys.argv[1]
    msg = sys.argv[2]

    client = HookClient(HookConfig(api_base="http://127.0.0.1:30001"))
    try:
        # 先 status 探测
        st = client.status()
        print(f"DLL status: {st}")
    except HookError as e:
        print(f"DLL 连不上: {e}")
        sys.exit(2)

    print(f"准备发送：to={to_wxid} 内容={msg[:50]}")
    try:
        result = client.send_text(to_wxid, msg)
        print(f"✅ DLL 返回: {result}")
    except HookError as e:
        print(f"❌ 失败: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()
