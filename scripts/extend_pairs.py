"""Append natural pairs for new curriculum works without renumbering old pairs."""
import json

from scripts.build_pairs import natural_positions


def add_pairs(db):
    added = 0
    for pid, title, source, raw in db.execute("SELECT id,title,source,paragraphs FROM poems WHERE source LIKE '校对补充/%' ORDER BY id"):
        positions = natural_positions(json.loads(raw), source, title)
        rows = {pos: lid for pos, lid in db.execute('SELECT pos,line_id FROM occurrences WHERE poem_id=?', (pid,))}
        for pos, next_pos in positions:
            if pos not in rows or next_pos not in rows:continue
            first, second = rows[pos], rows[next_pos]
            if first == second:continue
            pair_id = f'pair:{first}:{second}'
            result = db.execute('INSERT OR IGNORE INTO pairs VALUES(?,?,?,?,?,?)', (pair_id,first,second,pid,pos,0))
            if not result.rowcount:continue
            added += 1
            chars = db.execute('SELECT text FROM lines WHERE id IN (?,?)', (first,second)).fetchall()
            for char in set(''.join(r[0] for r in chars)):
                db.execute('INSERT INTO pair_chars VALUES(?,?,?,?)', (char,pair_id,0,first))
            db.execute('INSERT OR IGNORE INTO pair_members VALUES(?)', (first,))
            db.execute('INSERT OR IGNORE INTO pair_members VALUES(?)', (second,))
    return added
