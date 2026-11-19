"""Import reviewed curriculum poems into a separate copy of the active corpus."""
import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import ROOT, CORPUS
from app.text import HAN, normalize, simplify
from scripts.apply_current_books import apply as apply_books
from scripts.build_education import pieces
from scripts.build_search import add_indexes
from scripts.extend_pairs import add_pairs

MANIFEST = ROOT/'data/curriculum-gushici-import.json'
DECISIONS = ROOT/'data/curriculum-review-decisions.json'
STAGE_REPORT = ROOT/'docs/current-stage-build-20261004.json'


def poem_id(title, author, paragraphs):
    return hashlib.sha256((title+'|'+author+'|'+''.join(paragraphs)).encode()).hexdigest()[:24]


def add_poem(db, entry, next_line, report):
    title, author = simplify(entry['title']), simplify(entry['author'])
    paragraphs = [simplify(text) for text in entry['paragraphs']]
    if not title or not author or not paragraphs or len(paragraphs)>100:
        raise ValueError('补录作品缺少必要字段: '+entry['id'])
    pid = poem_id(title, author, paragraphs)
    existing = db.execute('SELECT paragraphs FROM poems WHERE id=?', (pid,)).fetchone()
    if existing:
        if [normalize(x) for x in json.loads(existing[0])] != [normalize(x) for x in paragraphs]:
            raise ValueError('作品 ID 与现有正文冲突: '+entry['id'])
        report['reused_poems'] += 1
    else:
        db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',
            (pid,title,author,entry['dynasty'],json.dumps(paragraphs,ensure_ascii=False),
             entry['original'],'校对补充/'+entry['id']))
        report['new_poems'] += 1
        for pos, segment in enumerate(pieces(paragraphs)):
            if not HAN.fullmatch(segment) or not 2 <= len(segment) <= 40:
                report['skipped_segments'].append({'title':title,'pos':pos,'text':segment})
                continue
            row = db.execute('SELECT id FROM lines WHERE text=?', (segment,)).fetchone()
            if row:
                lid = row[0]
            else:
                lid = next_line
                next_line += 1
                db.execute('INSERT INTO lines VALUES(?,?,?,0)', (lid,segment,len(segment)))
                db.executemany('INSERT INTO chars VALUES(?,?)', [(char,lid) for char in set(segment)])
                report['new_lines'] += 1
            db.execute('INSERT INTO occurrences VALUES(?,?,?)', (pid,pos,lid))
    body = {**entry, 'poem_id':pid, 'source_ids':entry['source_ids'], 'status':'pending'}
    db.execute('INSERT INTO education_entries VALUES(?,?,?,?)',
        (entry['id'],pid,'pending',json.dumps(body,ensure_ascii=False)))
    return pid,next_line


def add_stage(db, pid, decision):
    row = db.execute('SELECT paragraphs FROM poems WHERE id=?', (pid,)).fetchone()
    if not row:
        raise ValueError('所选版本不存在: '+decision['title'])
    segments = pieces(json.loads(row[0]))
    stage,book_id = decision['stage'],decision['book_id']
    entry_id = f"review:{book_id}:{decision['title']}"
    db.execute('INSERT OR IGNORE INTO education_work_stage VALUES(?,?,?,?,?)',
        (pid,stage,0,len(segments),entry_id))
    db.execute('INSERT OR IGNORE INTO education_stage_spans VALUES(?,?,?,?)',
        (pid,stage,0,sum(len(segment) for segment in segments)))
    verified = int(bool(db.execute('SELECT 1 FROM education_verified WHERE poem_id=?',(pid,)).fetchone()))
    rows = list(db.execute('SELECT pos,line_id FROM occurrences WHERE poem_id=? ORDER BY pos',(pid,)))
    for pos,lid in rows:
        db.execute('INSERT OR IGNORE INTO education_line_stage VALUES(?,?,?,?,?)',
            (lid,stage,pid,pos,verified))
    for pos,lid in rows:
        next_row = db.execute('SELECT line_id FROM occurrences WHERE poem_id=? AND pos=?',(pid,pos+1)).fetchone()
        if not next_row:continue
        pair = db.execute('SELECT id FROM pairs WHERE first_id=? AND second_id=?',(lid,next_row[0])).fetchone()
        if pair:
            db.execute('INSERT OR IGNORE INTO education_pair_stage VALUES(?,?,?,?)',
                (pair[0],stage,pid,verified))


def build(source, output, report_path, manifest_path=MANIFEST, decisions_path=DECISIONS):
    source,output,report_path,manifest_path,decisions_path = map(pathlib.Path,
        (source,output,report_path,manifest_path,decisions_path))
    if source.resolve()==output.resolve() or output.exists():
        raise ValueError('输出必须是尚不存在的独立文件')
    manifest = json.loads(manifest_path.read_text())
    decisions = json.loads(decisions_path.read_text())
    if len(manifest['items'])!=46 or len(decisions['decisions'])!=5:
        raise ValueError('补录清单或人工决定不完整')
    if decisions['report_sha256'] != hashlib.sha256(STAGE_REPORT.read_bytes()).hexdigest():
        raise ValueError('人工决定与当前学段报告不匹配')
    selected = {(item['book_id'],item['title'],item['author']):item for item in decisions['decisions']}
    if len(selected)!=5 or any(item['kind']!='candidate' for item in selected.values()):
        raise ValueError('仍有未确定的人工决定')
    report = {'new_poems':0,'reused_poems':0,'new_lines':0,'new_pairs':0,
              'skipped_segments':[],'selected_versions':{},'source_works':len(manifest['items'])}
    started=time.monotonic()
    try:
        with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as src,sqlite3.connect(output) as db:
            src.backup(db)
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            if db.execute("SELECT 1 FROM meta WHERE key='curriculum_import'").fetchone():
                raise ValueError('源诗库已包含本次补录，请使用补录前的诗库')
            db.execute('INSERT INTO education_sources VALUES(?,?)',('gushici-selective-20261004',
                json.dumps({'id':'gushici-selective-20261004','organization':'古诗词网',
                    'url':'https://so.gushici.net/','retrieved':'2026-10-04',
                    'scope':'仅补录本次缺失的公有领域原文，不包括注释和翻译'},ensure_ascii=False)))
            next_line = db.execute('SELECT coalesce(max(id),0)+1 FROM lines').fetchone()[0]
            for item in sorted(manifest['items'],key=lambda row:row['id']):
                entry = {**item,'source_ids':['gushici-selective-20261004'],
                         'evidence':item['source_url']}
                _,next_line = add_poem(db,entry,next_line,report)
            reference = next(item for item in decisions['decisions'] if item['version_id'].startswith('reference:'))
            if reference['title']!='芙蓉楼送辛渐' or reference['source_url'] != \
                    'https://www.pep.com.cn/rjdt/mtbd/202307/t20230718_1984406.shtml':
                raise ValueError('新增参考版本来源与校对决定不符')
            reference_entry = {
                'id':'review-pep-furong-2023','title':reference['title'],
                'author':reference['author'],'dynasty':'唐','paragraphs':reference['paragraphs'],
                'original':'\n'.join(reference['paragraphs']),
                'source_ids':['pep-tang-2023'],'evidence':reference['source_url'],
                'book_id':reference['book_id'],'stage':reference['stage'],
            }
            reference_id,next_line = add_poem(db,reference_entry,next_line,report)
            report['new_pairs'] = add_pairs(db)
            add_indexes(db)
            report['current_books'] = apply_books(db)
            for decision in decisions['decisions']:
                if decision['version_id'].startswith('reference:'):
                    pid = reference_id
                else:
                    pid = decision['version_id']
                    row = db.execute('SELECT paragraphs,author FROM poems WHERE id=?',(pid,)).fetchone()
                    if not row or simplify(row[1])!=simplify(decision['author']) or \
                            normalize(''.join(json.loads(row[0]))) != normalize(''.join(decision['paragraphs'])):
                        raise ValueError('已选库内版本发生变化: '+decision['title'])
                add_stage(db,pid,decision)
                report['selected_versions'][decision['title']] = pid
            old_line = db.execute('''SELECT line_id FROM occurrences WHERE poem_id=? AND pos=0''',
                ('db55d3ad22c0438f8a645eb8',)).fetchone()
            new_line = db.execute('SELECT line_id FROM occurrences WHERE poem_id=? AND pos=0',(reference_id,)).fetchone()
            if old_line and new_line and old_line[0]!=new_line[0]:
                group='equiv:'+str(min(old_line[0],new_line[0]))
                for lid in (old_line[0],new_line[0]):
                    existing=db.execute('SELECT group_id FROM education_equivalents WHERE line_id=?',(lid,)).fetchone()
                    if existing and existing[0]!=group:raise ValueError('异文等价组冲突')
                    db.execute('INSERT OR IGNORE INTO education_equivalents VALUES(?,?)',(lid,group))
            signature = hashlib.sha256(manifest_path.read_bytes()+decisions_path.read_bytes()).hexdigest()
            db.execute('INSERT INTO meta VALUES(?,?)',('curriculum_import',signature))
            version='1.4.0-'+hashlib.sha256((db.execute("SELECT value FROM meta WHERE key='education'").fetchone()[0]+signature).encode()).hexdigest()[:16]
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('education',version))
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('补录诗库完整性检查失败')
            report.update(corpus_version=version,elapsed_seconds=round(time.monotonic()-started,2),
                poems=db.execute('SELECT count(*) FROM poems').fetchone()[0],
                lines=db.execute('SELECT count(*) FROM lines').fetchone()[0],
                stage_counts=dict(db.execute('SELECT stage,count(DISTINCT poem_id) FROM education_work_stage GROUP BY stage')))
        report_path.parent.mkdir(parents=True,exist_ok=True)
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report
    except BaseException:
        output.unlink(missing_ok=True)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=pathlib.Path,default=CORPUS)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    parser.add_argument('--report',type=pathlib.Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.source,args.output,args.report),ensure_ascii=False))
