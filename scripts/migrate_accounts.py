"""Idempotent legacy-guest migration. Back up the database before running."""
import argparse
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.accounts import Accounts
from app.config import GAMES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=pathlib.Path, default=GAMES)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("数据库不存在，请核对路径")
    Accounts(args.database)
    with sqlite3.connect(args.database) as db:
        result = {
            "users": db.execute("SELECT count(*) FROM users").fetchone()[0],
            "legacy_mappings": db.execute("SELECT count(*) FROM legacy_identities").fetchone()[0],
            "quick_check": db.execute("PRAGMA quick_check").fetchone()[0],
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
