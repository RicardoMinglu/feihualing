"""Create an administrator locally; no public registration or default password."""
import argparse
import getpass
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.accounts import Accounts, AuthError
from app.config import GAMES


def main():
    parser = argparse.ArgumentParser(description="飞花令本地账号管理")
    parser.add_argument("--database", type=pathlib.Path, default=GAMES)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="通过终端交互输入密码创建管理员")
    create.add_argument("--username", required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("请在交互式终端运行；密码不能作为命令行参数或管道输入")
    password = getpass.getpass("管理员密码（至少 8 个字符）：")
    if password != getpass.getpass("再次输入密码："):
        parser.error("两次输入的密码不一致")
    try:
        user = Accounts(args.database).create_admin(args.username, password)
    except AuthError as exc:
        parser.error(exc.message)
    print(json.dumps({"id": user["id"], "username": user["username"], "role": user["role"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
