"""Add frozen title-level stage evidence to an already-built candidate corpus."""
import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import ROOT
from app.text import simplify
from scripts.build_education import SOURCES, POEMS, CORRECTIONS, RECOMMENDATIONS, digest, pieces


def apply(database, recommendations=RECOMMENDATIONS):
    rows=json.loads(pathlib.Path(recommendations).read_text())
    source_ids={x['id'] for x in json.loads(SOURCES.read_text())}
    with sqlite3.connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='education_work_stage'").fetchone():
            raise ValueError('源文件不是已构建的教育候选诗库')
        db.execute('CREATE INDEX IF NOT EXISTS education_poem_title_author ON poems(title,author)')
        db.execute("DELETE FROM education_work_stage WHERE entry_id LIKE 'hs-%'")
        db.execute('''DELETE FROM education_stage_spans WHERE NOT EXISTS(
            SELECT 1 FROM education_work_stage w WHERE w.poem_id=education_stage_spans.poem_id
            AND w.stage=education_stage_spans.stage)''')
        db.execute('''DELETE FROM education_line_stage WHERE NOT EXISTS(
            SELECT 1 FROM education_work_stage w WHERE w.poem_id=education_line_stage.poem_id
            AND w.stage=education_line_stage.stage AND education_line_stage.pos>=w.start_pos
            AND education_line_stage.pos<w.end_pos)''')
        db.execute('''DELETE FROM education_pair_stage WHERE NOT EXISTS(
            SELECT 1 FROM pairs q JOIN education_work_stage w ON w.poem_id=q.poem_id
            WHERE q.id=education_pair_stage.pair_id AND w.poem_id=education_pair_stage.poem_id
            AND w.stage=education_pair_stage.stage AND q.pos>=w.start_pos AND q.pos+1<w.end_pos)''')
        report={'recommendations':len(rows),'matched':0,'unmatched':[],'held':[],'stage_links_added':0}
        for rec in rows:
            if rec['source_id'] not in source_ids or rec['stage'] not in ('primary','junior','senior'):
                raise ValueError('推荐篇目来源或学段无效: '+rec['id'])
            if rec.get('hold'):
                report['held'].append(rec['id']);continue
            matches=[r[0] for r in db.execute('SELECT id FROM poems WHERE title=? AND author=?',(simplify(rec['title']),simplify(rec['author'])))]
            if len(matches)!=1:report['unmatched'].append(rec['id']);continue
            report['matched']+=1
            for pid in matches:
                segments=pieces(json.loads(db.execute('SELECT paragraphs FROM poems WHERE id=?',(pid,)).fetchone()[0]))
                if not segments:continue
                existing=db.execute('INSERT OR IGNORE INTO education_work_stage VALUES(?,?,?,?,?)',(pid,rec['stage'],0,len(segments),rec['id']))
                report['stage_links_added']+=existing.rowcount
                db.execute('INSERT OR IGNORE INTO education_stage_spans VALUES(?,?,?,?)',(pid,rec['stage'],0,sum(len(x) for x in segments)))
                verified=int(bool(db.execute('SELECT 1 FROM education_verified WHERE poem_id=?',(pid,)).fetchone()))
                occurrences=list(db.execute('SELECT pos,line_id FROM occurrences WHERE poem_id=? ORDER BY pos',(pid,)))
                for pos,lid in occurrences:
                    db.execute('INSERT OR IGNORE INTO education_line_stage VALUES(?,?,?,?,?)',(lid,rec['stage'],pid,pos,verified))
                for pos,lid in occurrences:
                    next_row=db.execute('SELECT line_id FROM occurrences WHERE poem_id=? AND pos=?',(pid,pos+1)).fetchone()
                    if not next_row:continue
                    pair=db.execute('SELECT id FROM pairs WHERE first_id=? AND second_id=?',(lid,next_row[0])).fetchone()
                    if pair:db.execute('INSERT OR IGNORE INTO education_pair_stage VALUES(?,?,?,?)',(pair[0],rec['stage'],pid,verified))
        version_hash=hashlib.sha256((''.join(digest(x) for x in (SOURCES,POEMS,CORRECTIONS,pathlib.Path(recommendations)))).encode()).hexdigest()[:16]
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('education','1.4.0-'+version_hash))
        db.commit()
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('候选诗库完整性检查失败')
        report['corpus_version']='1.4.0-'+version_hash
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',required=True,type=pathlib.Path)
    parser.add_argument('--report',required=True,type=pathlib.Path)
    args=parser.parse_args()
    result=apply(args.database)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
