"""Idempotently add visitor settings and searchable game summaries."""
import argparse
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import GAMES


def migrate(db):
    db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
    db.execute('''CREATE TABLE IF NOT EXISTS visitor_profiles(
        owner TEXT PRIMARY KEY,
        education_stage TEXT NOT NULL DEFAULT 'all'
            CHECK(education_stage IN ('all','primary','junior','senior')),
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS game_summaries(
        game_id TEXT PRIMARY KEY, owner TEXT NOT NULL, word TEXT NOT NULL,
        difficulty TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
        created_at REAL NOT NULL, ended_at REAL, round INTEGER NOT NULL,
        player_answer_count INTEGER NOT NULL, message_count INTEGER NOT NULL
    )''')
    db.execute('''CREATE INDEX IF NOT EXISTS game_summary_owner_time
        ON game_summaries(owner,created_at DESC,game_id DESC)''')
    count = 0
    for gid, owner, raw in db.execute('''SELECT id,owner,state FROM games g
        WHERE NOT EXISTS(SELECT 1 FROM game_summaries s WHERE s.game_id=g.id)'''):
        state = json.loads(raw)
        legacy_messages = state.get('messages', [])
        if legacy_messages:
            player_count = sum(m.get('who') == 'player' for m in legacy_messages)
        else:
            player_count = db.execute('SELECT count(*) FROM messages WHERE game_id=? AND json_extract(body,\'$.who\')=\'player\'', (gid,)).fetchone()[0]
        db.execute('''INSERT INTO game_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (
            gid, owner, state.get('word', ''), state.get('difficulty', 'easy'),
            state.get('status', 'playing'), state.get('reason', ''),
            state.get('created_at', 0), state.get('ended_at'), state.get('round', 0),
            player_count, state.get('message_count', len(legacy_messages))))
        count += 1
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1)')
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=pathlib.Path, default=GAMES)
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        connection.execute('BEGIN IMMEDIATE')
        print(json.dumps({'summaries_added': migrate(connection)}, ensure_ascii=False))
