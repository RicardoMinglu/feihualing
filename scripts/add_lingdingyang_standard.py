"""Add the sourced standard text of 过零丁洋 to an offline corpus database."""
import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import CORPUS, ROOT
from app.text import HAN, normalize, simplify
from scripts.build_education import pieces
from scripts.extend_pairs import add_pairs

MANIFEST = ROOT / 'data/lingdingyang-standard-20261021.json'
MARKER = 'poem_patch_lingdingyang_20261021'


def apply(database=CORPUS, manifest_path=MANIFEST):
    manifest_path = pathlib.Path(manifest_path)
    item = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    database = pathlib.Path(database)
    result = {'id': item['id'], 'database': str(database)}
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('BEGIN IMMEDIATE')
        marker = db.execute('SELECT value FROM meta WHERE key=?', (MARKER,)).fetchone()
        if marker:
            if marker[0] != digest:
                raise ValueError('已应用的校订清单与当前文件不一致')
            return {**result, 'status': 'already_applied'}

        old = item['old_poem_id']
        old_row = db.execute('SELECT title,author FROM poems WHERE id=?', (old,)).fetchone()
        old_clauses = [row[0] for row in db.execute('''SELECT l.text FROM occurrences o
            JOIN lines l ON l.id=o.line_id WHERE o.poem_id=? ORDER BY o.pos''', (old,))]
        if old_row != (item['title'], item['author']) or old_clauses != item['old_clauses']:
            raise ValueError('原始《过零丁洋》记录与校订清单不一致')
        stage = item['stage']
        entry_id = item['book_entry_id']
        if db.execute('''SELECT count(*) FROM education_work_stage
            WHERE poem_id=? AND stage=? AND entry_id=?''', (old, stage, entry_id)).fetchone()[0] != 1:
            raise ValueError('教材目录关系与校订清单不一致')

        title, author = simplify(item['title']), simplify(item['author'])
        paragraphs = list(item['paragraphs'])
        clauses = pieces(paragraphs)
        if len(clauses) != 8 or any(not HAN.fullmatch(text) or len(text) != 7 for text in clauses):
            raise ValueError('《过零丁洋》应为八句七言诗')
        poem_id = hashlib.sha256((title+'|'+author+'|'+''.join(paragraphs)).encode()).hexdigest()[:24]
        if db.execute('SELECT 1 FROM poems WHERE id=?', (poem_id,)).fetchone():
            raise ValueError('标准版本已存在，但缺少校订标记')

        db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',
                   (poem_id, title, author, item['dynasty'],
                    json.dumps(paragraphs, ensure_ascii=False), '\n'.join(paragraphs),
                    '校对补充/'+item['id']))
        next_line = db.execute('SELECT coalesce(max(id),0)+1 FROM lines').fetchone()[0]
        new_lines = 0
        line_ids = []
        for pos, clause in enumerate(clauses):
            row = db.execute('SELECT id FROM lines WHERE text=?', (clause,)).fetchone()
            if row:
                line_id = row[0]
            else:
                line_id = next_line
                next_line += 1
                new_lines += 1
                db.execute('INSERT INTO lines VALUES(?,?,?,0)', (line_id, clause, len(clause)))
                db.executemany('INSERT INTO chars VALUES(?,?)', [(char, line_id) for char in set(clause)])
            line_ids.append(line_id)
            db.execute('INSERT INTO occurrences VALUES(?,?,?)', (poem_id, pos, line_id))
        new_pairs = add_pairs(db)
        if any(not db.execute('SELECT 1 FROM pairs WHERE first_id=? AND second_id=?',
                              (line_ids[pos], line_ids[pos+1])).fetchone()
               for pos in range(0, len(line_ids), 2)):
            raise ValueError('《过零丁洋》相邻上下句索引不完整')

        for source in item['sources']:
            db.execute('INSERT INTO education_sources VALUES(?,?)',
                       (source['id'], json.dumps(source, ensure_ascii=False)))
        entry = {**item, 'source_ids': [source['id'] for source in item['sources']],
                 'source_url': item['sources'][0]['url'],
                 'evidence': '两处独立公开作品原文全文一致', 'status': 'verified'}
        db.execute('INSERT INTO education_entries VALUES(?,?,?,?)',
                   (item['id'], poem_id, 'verified', json.dumps(entry, ensure_ascii=False)))
        db.execute('INSERT INTO education_verified VALUES(?)', (poem_id,))

        db.execute('DELETE FROM education_work_stage WHERE poem_id=? AND stage=? AND entry_id=?',
                   (old, stage, entry_id))
        db.execute('INSERT INTO education_work_stage VALUES(?,?,?,?,?)',
                   (poem_id, stage, 0, len(clauses), entry_id))
        db.execute('DELETE FROM education_stage_spans WHERE poem_id=? AND stage=?', (old, stage))
        db.execute('INSERT INTO education_stage_spans VALUES(?,?,?,?)',
                   (poem_id, stage, 0, sum(map(len, clauses))))
        db.execute('DELETE FROM education_line_stage WHERE poem_id=? AND stage=?', (old, stage))
        db.execute('DELETE FROM education_pair_stage WHERE poem_id=? AND stage=?', (old, stage))
        for pos, line_id in enumerate(line_ids):
            db.execute('INSERT INTO education_line_stage VALUES(?,?,?,?,?)',
                       (line_id, stage, poem_id, pos, 1))
        for pos in range(0, len(line_ids), 2):
            pair_id = 'pair:'+str(line_ids[pos])+':'+str(line_ids[pos+1])
            db.execute('INSERT INTO education_pair_stage VALUES(?,?,?,?)',
                       (pair_id, stage, poem_id, 1))

        old_line_ids = [row[0] for row in db.execute(
            'SELECT line_id FROM occurrences WHERE poem_id=? ORDER BY pos', (old,))]
        for old_id, new_id in zip(old_line_ids, line_ids):
            if old_id == new_id:
                continue
            group = 'equiv:'+str(min(old_id, new_id))
            for line_id in (old_id, new_id):
                previous = db.execute('SELECT group_id FROM education_equivalents WHERE line_id=?',
                                      (line_id,)).fetchone()
                if previous and previous[0] != group:
                    raise ValueError('诗句已有不同的异文等价组')
                db.execute('INSERT OR IGNORE INTO education_equivalents VALUES(?,?)',
                           (line_id, group))

        body = normalize(''.join(paragraphs))
        work_id = db.execute('SELECT coalesce(max(id),0)+1 FROM work_search').fetchone()[0]
        db.execute('INSERT INTO work_search VALUES(?,?,?,?)',
                   (work_id, poem_id, normalize(author), body))
        db.executemany('INSERT INTO work_chars VALUES(?,?)',
                       [(char, work_id) for char in set(body)])
        db.execute('INSERT INTO work_fts(rowid,body) VALUES(?,?)', (work_id, body))

        search = json.loads(db.execute("SELECT value FROM meta WHERE key='search'").fetchone()[0])
        search['works'] = db.execute('SELECT count(*) FROM work_search').fetchone()[0]
        db.execute("UPDATE meta SET value=? WHERE key='search'", (json.dumps(search),))
        pairing = json.loads(db.execute("SELECT value FROM meta WHERE key='pairing'").fetchone()[0])
        pairing['pairs'] = db.execute('SELECT count(*) FROM pairs').fetchone()[0]
        db.execute("UPDATE meta SET value=? WHERE key='pairing'", (json.dumps(pairing),))
        previous = db.execute("SELECT value FROM meta WHERE key='education'").fetchone()[0]
        version = '1.4.0-'+hashlib.sha256((previous+digest).encode()).hexdigest()[:16]
        db.execute("UPDATE meta SET value=? WHERE key='education'", (version,))
        db.execute('INSERT INTO meta VALUES(?,?)', (MARKER, digest))
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('校订后诗库完整性检查失败')
        result.update(status='applied', poem_id=poem_id, new_lines=new_lines,
                      new_pairs=new_pairs, corpus_version=version)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=pathlib.Path, default=CORPUS)
    parser.add_argument('--manifest', type=pathlib.Path, default=MANIFEST)
    args = parser.parse_args()
    print(json.dumps(apply(args.database, args.manifest), ensure_ascii=False))
