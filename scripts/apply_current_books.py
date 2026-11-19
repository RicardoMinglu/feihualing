"""Stage works listed in the current PEP textbook tables of contents."""
import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import ROOT
from app.text import normalize, simplify
from scripts.build_education import pieces, SOURCES, POEMS, CORRECTIONS, RECOMMENDATIONS, digest

BOOKS = ROOT / 'data/curriculum-current-books.json'


def _match(db, title, author, first_line=None, anchor_line=None):
    title, author = simplify(title), simplify(author)
    exact = list(db.execute('SELECT id FROM poems WHERE title=? AND author=? ORDER BY id', (title, author)))
    if first_line:
        first_line = normalize(first_line)
    if anchor_line:
        anchor_line = normalize(anchor_line)
    def with_first_line(rows):
        if first_line:
            rows = [r for r in rows if db.execute('''SELECT 1 FROM occurrences o JOIN lines l ON l.id=o.line_id
                WHERE o.poem_id=? AND o.pos=0 AND l.text=?''', (r[0], first_line)).fetchone()]
        if anchor_line:
            rows = [r for r in rows if db.execute('''SELECT 1 FROM occurrences o JOIN lines l ON l.id=o.line_id
                WHERE o.poem_id=? AND l.text=?''', (r[0], anchor_line)).fetchone()]
        return rows
    rows = with_first_line(exact)
    if not rows:
        requested = normalize(title)
        broader = [(pid,) for pid, candidate in db.execute('''SELECT p.id,p.title FROM work_search w
            JOIN poems p ON p.id=w.poem_id WHERE w.author=? ORDER BY p.id''', (normalize(author),))
            if requested in normalize(candidate)]
        rows = with_first_line(broader)
    if len(rows)>1:
        versions = [(r, normalize(''.join(json.loads(db.execute('SELECT paragraphs FROM poems WHERE id=?',
            (r[0],)).fetchone()[0])))) for r in rows]
        if len({body for _, body in versions})==1:
            rows = sorted(rows, key=lambda r: (
                not bool(db.execute('SELECT 1 FROM education_verified WHERE poem_id=?', (r[0],)).fetchone()),
                db.execute('SELECT source FROM poems WHERE id=?', (r[0],)).fetchone()[0]!='本项目常见诗词校对集',
                r[0]))[:1]
    return [r[0] for r in rows]


def apply(db, books_path=BOOKS):
    books_path = pathlib.Path(books_path)
    books = json.loads(books_path.read_text())
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='education_work_stage'").fetchone():
        raise ValueError('诗库尚未建立学段关系')
    ids = set()
    for book in books:
        if book['id'] in ids or book['stage'] not in ('primary', 'junior', 'senior'):
            raise ValueError('教材 ID 重复或学段无效')
        ids.add(book['id'])
        if not book['works'] or book['image_dir'] not in ('page', 'mobile'):
            raise ValueError('教材目录不完整: '+book['id'])
        if book.get('variant_source_id') and not db.execute('SELECT 1 FROM education_sources WHERE id=?',
            (book['variant_source_id'],)).fetchone():
            raise ValueError('异文核对来源不存在: '+book['id'])
    previous = db.execute("SELECT value FROM meta WHERE key='education'").fetchone()[0]
    saved_base = db.execute("SELECT value FROM meta WHERE key='education_base'").fetchone()
    manifest_digest = hashlib.sha256(books_path.read_bytes()).hexdigest()
    expected_base = '1.4.0-'+hashlib.sha256((''.join(digest(path) for path in
        (SOURCES,POEMS,CORRECTIONS,RECOMMENDATIONS))).encode()).hexdigest()[:16]
    expected_current = '1.4.0-'+hashlib.sha256((expected_base+manifest_digest).encode()).hexdigest()[:16]
    base_version = saved_base[0] if saved_base else expected_base if previous==expected_current else previous
    removed = db.execute("DELETE FROM education_work_stage WHERE stage IN ('primary','junior')").rowcount
    db.execute("DELETE FROM education_sources WHERE id LIKE 'pep-book-%'")
    result = {'books': len(books), 'listed': 0, 'matched': 0, 'ambiguous': [],
              'missing': [], 'removed_legacy_stage_links': removed, 'added_stage_links': 0}
    for book in books:
        source_id = 'pep-book-'+book['id']
        source = {'id': source_id, 'organization': '人民教育出版社',
                  'title': '义务教育教科书 语文 '+book['volume'],
                  'version': '2026-10-03 在线目录', 'volume': book['volume'],
                  'url': f"https://book.pep.com.cn/{book['id']}/mobile/index.html",
                  'toc_pages': book['toc_pages'],
                  'toc_images': [f"https://book.pep.com.cn/{book['id']}/files/{book['image_dir']}/{page}.jpg"
                                 for page in book['toc_pages']],
                  'variant_source_id': book.get('variant_source_id'),
                  'note': '只核对官方目录的篇目归属；正文仍须逐首对照。'}
        db.execute('INSERT INTO education_sources VALUES(?,?)', (source_id, json.dumps(source, ensure_ascii=False)))
        for index, work in enumerate(book['works']):
            if len(work) not in (2, 3, 4):raise ValueError('篇目字段无效: '+source_id)
            title, author = work[:2]
            first_line = work[2] if len(work)==3 else None
            if len(work)==4:first_line,anchor_line=work[2:]
            else:anchor_line=None
            result['listed'] += 1
            matches = _match(db, title, author, first_line, anchor_line)
            item = {'book_id': book['id'], 'volume': book['volume'],
                    'title': title, 'author': author, 'first_line': first_line,
                    'anchor_line': anchor_line}
            if not matches:
                result['missing'].append(item)
                continue
            if len(matches)>1:
                result['ambiguous'].append({**item, 'candidate_ids': matches})
                continue
            pid = matches[0]
            paragraphs = json.loads(db.execute('SELECT paragraphs FROM poems WHERE id=?', (pid,)).fetchone()[0])
            segments = pieces(paragraphs)
            if not segments:
                result['missing'].append(item)
                continue
            entry_id = source_id+':'+str(index)
            inserted = db.execute('INSERT OR IGNORE INTO education_work_stage VALUES(?,?,?,?,?)',
                                  (pid, book['stage'], 0, len(segments), entry_id)).rowcount
            result['matched'] += 1
            result['added_stage_links'] += inserted
    db.execute('DELETE FROM education_stage_spans')
    db.execute('DELETE FROM education_line_stage')
    db.execute('DELETE FROM education_pair_stage')
    for pid, stage, start, end, _ in db.execute('SELECT * FROM education_work_stage'):
        paragraphs = json.loads(db.execute('SELECT paragraphs FROM poems WHERE id=?', (pid,)).fetchone()[0])
        segments = pieces(paragraphs)
        db.execute('INSERT OR IGNORE INTO education_stage_spans VALUES(?,?,?,?)',
                   (pid, stage, sum(len(x) for x in segments[:start]), sum(len(x) for x in segments[:end])))
        verified = int(bool(db.execute('SELECT 1 FROM education_verified WHERE poem_id=?', (pid,)).fetchone()))
        occurrences = list(db.execute('SELECT pos,line_id FROM occurrences WHERE poem_id=? AND pos>=? AND pos<? ORDER BY pos',
                                      (pid, start, end)))
        for pos, lid in occurrences:
            db.execute('INSERT OR IGNORE INTO education_line_stage VALUES(?,?,?,?,?)',
                       (lid, stage, pid, pos, verified))
        for pos, lid in occurrences:
            if pos+1 >= end:continue
            next_row = db.execute('SELECT line_id FROM occurrences WHERE poem_id=? AND pos=?',
                                  (pid, pos+1)).fetchone()
            if not next_row:continue
            pair = db.execute('SELECT id FROM pairs WHERE first_id=? AND second_id=?',
                              (lid, next_row[0])).fetchone()
            if pair:
                db.execute('INSERT OR IGNORE INTO education_pair_stage VALUES(?,?,?,?)',
                           (pair[0], stage, pid, verified))
    version = '1.4.0-'+hashlib.sha256((base_version+manifest_digest).encode()).hexdigest()[:16]
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('education_base', base_version))
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('education', version))
    result['corpus_version'] = version
    result['stages'] = dict(db.execute('SELECT stage,count(DISTINCT poem_id) FROM education_work_stage GROUP BY stage'))
    return result


def build(source, output, report, books_path=BOOKS):
    source, output, report = map(pathlib.Path, (source, output, report))
    if source.resolve()==output.resolve() or output.exists():raise ValueError('输出必须是尚不存在的独立文件')
    try:
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src, sqlite3.connect(output) as db:
            src.backup(db)
            result = apply(db, books_path)
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('候选诗库完整性检查失败')
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except BaseException:
        output.unlink(missing_ok=True)
        raise


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=pathlib.Path)
    parser.add_argument('--output', required=True, type=pathlib.Path)
    parser.add_argument('--report', required=True, type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.report), ensure_ascii=False))
