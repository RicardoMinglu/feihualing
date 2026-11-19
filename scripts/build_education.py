"""Build a reviewed corpus copy while preserving all existing poem and line IDs."""
import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import ROOT, CORPUS
from app.education import STAGES
from app.text import normalize, simplify, HAN
from scripts.build_pairs import natural_positions
from scripts.build_search import add_indexes

SOURCES = ROOT / 'data/curriculum-sources.json'
POEMS = ROOT / 'data/curriculum-poems.json'
CORRECTIONS = ROOT / 'data/poem-corrections.json'
RECOMMENDATIONS = ROOT / 'data/curriculum-stage-recommendations.json'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pieces(paragraphs):
    import re
    return [normalize(x) for x in re.split(r'[，。！？；、,!?;\n]+', ''.join(paragraphs)) if normalize(x)]


def build(source, output, report, sources_path=SOURCES, poems_path=POEMS, corrections_path=CORRECTIONS, recommendations_path=RECOMMENDATIONS):
    source, output, report = map(pathlib.Path, (source, output, report))
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError('输出必须是尚不存在的独立文件')
    sources = json.loads(sources_path.read_text())
    entries = json.loads(poems_path.read_text())
    corrections = json.loads(corrections_path.read_text())
    recommendations = json.loads(recommendations_path.read_text()) if recommendations_path.exists() else []
    source_ids = {x['id'] for x in sources}
    if len(source_ids) != len(sources):raise ValueError('来源 ID 重复')
    entry_ids = {x['id'] for x in entries}
    if len(entry_ids) != len(entries):raise ValueError('作品条目 ID 重复')
    for entry in entries:
        if not set(entry.get('source_ids', [])) <= source_ids:raise ValueError('作品引用未知来源: '+entry['id'])
        if entry['status'] not in ('pending','verified','blocked'):raise ValueError('校对状态无效')
        if set(entry.get('stages', [])) - (set(STAGES)-{'all'}):raise ValueError('作品学段无效')
        if entry['status']=='verified' and (not entry.get('paragraphs') or not entry.get('source_ids') or not entry.get('evidence')):
            raise ValueError('已校对作品缺少正文或证据: '+entry['id'])
    for rec in recommendations:
        if rec['source_id'] not in source_ids or rec['stage'] not in STAGES or rec['stage']=='all':
            raise ValueError('推荐篇目来源或学段无效: '+rec['id'])
    began=time.monotonic()
    stats={'entries':len(entries),'verified':0,'pending':0,'blocked':0,'new_poems':0,'new_lines':0,'stage_links':0,'corrections':0,'recommendations':len(recommendations),'recommendations_matched':0,'recommendations_unmatched':[],'recommendations_held':[]}
    try:
        with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as src, sqlite3.connect(output) as db:
            src.backup(db)
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            db.executescript('''
            CREATE TABLE education_sources(id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE education_entries(id TEXT PRIMARY KEY,poem_id TEXT,status TEXT,body TEXT NOT NULL);
            CREATE TABLE education_verified(poem_id TEXT PRIMARY KEY);
            CREATE TABLE education_work_stage(poem_id TEXT,stage TEXT,start_pos INTEGER,end_pos INTEGER,entry_id TEXT,
                PRIMARY KEY(poem_id,stage,start_pos,end_pos,entry_id));
            CREATE INDEX education_work_stage_lookup ON education_work_stage(stage,poem_id,start_pos,end_pos);
            CREATE TABLE education_stage_spans(poem_id TEXT,stage TEXT,start_char INTEGER,end_char INTEGER,
                PRIMARY KEY(poem_id,stage,start_char,end_char));
            CREATE INDEX education_stage_spans_lookup ON education_stage_spans(stage,poem_id);
            CREATE TABLE education_line_stage(line_id INTEGER,stage TEXT,poem_id TEXT,pos INTEGER,verified INTEGER,
                PRIMARY KEY(line_id,stage,poem_id,pos));
            CREATE INDEX education_line_stage_lookup ON education_line_stage(stage,line_id);
            CREATE TABLE education_pair_stage(pair_id TEXT,stage TEXT,poem_id TEXT,verified INTEGER,
                PRIMARY KEY(pair_id,stage,poem_id));
            CREATE INDEX education_pair_stage_lookup ON education_pair_stage(stage,pair_id);
            CREATE TABLE education_invalid_occurrences(poem_id TEXT,pos INTEGER,reason TEXT,PRIMARY KEY(poem_id,pos));
            CREATE TABLE education_inactive_works(poem_id TEXT PRIMARY KEY);
            CREATE TABLE education_inactive_lines(line_id INTEGER PRIMARY KEY);
            CREATE TABLE education_inactive_pairs(pair_id TEXT PRIMARY KEY);
            CREATE TABLE education_equivalents(line_id INTEGER PRIMARY KEY,group_id TEXT NOT NULL);
            CREATE INDEX education_equivalent_group ON education_equivalents(group_id,line_id);
            CREATE TABLE education_corrections(id TEXT PRIMARY KEY,body TEXT NOT NULL);
            ''')
            db.executemany('INSERT INTO education_sources VALUES(?,?)',[(x['id'],json.dumps(x,ensure_ascii=False)) for x in sources])
            next_line=db.execute('SELECT coalesce(max(id),0) FROM lines').fetchone()[0]+1
            for entry in sorted(entries,key=lambda x:x['id']):
                status=entry['status'];stats[status]+=1
                paragraphs=entry.get('paragraphs') or []
                existing=entry.get('poem_id')
                pid=None
                if paragraphs and status!='blocked':
                    if existing:
                        old=db.execute('SELECT title,author,paragraphs FROM poems WHERE id=?',(existing,)).fetchone()
                        if not old:raise ValueError('关联作品不存在: '+entry['id'])
                        if (simplify(old[0]),simplify(old[1]),[normalize(x) for x in pieces(json.loads(old[2]))]) != (simplify(entry['title']),simplify(entry['author']),pieces(paragraphs)):
                            raise ValueError('关联作品正文不一致: '+entry['id'])
                        pid=existing
                    else:
                        title=simplify(entry['title']);author=simplify(entry['author'])
                        signature=hashlib.sha256((title+'|'+author+'|'+''.join(simplify(x) for x in paragraphs)).encode()).hexdigest()[:24]
                        if db.execute('SELECT 1 FROM poems WHERE id=?',(signature,)).fetchone():
                            pid=signature
                        else:
                            pid=signature
                            db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',(pid,title,author,entry['dynasty'],json.dumps([simplify(x) for x in paragraphs],ensure_ascii=False),'\n'.join(paragraphs),'校对补充/'+entry['id']))
                            stats['new_poems']+=1
                            for pos,segment in enumerate(pieces(paragraphs)):
                                if not HAN.fullmatch(segment) or not 2<=len(segment)<=40:raise ValueError('补充诗句无效: '+entry['id'])
                                row=db.execute('SELECT id FROM lines WHERE text=?',(segment,)).fetchone()
                                if row:lid=row[0]
                                else:
                                    lid=next_line;next_line+=1;stats['new_lines']+=1
                                    db.execute('INSERT INTO lines VALUES(?,?,?,0)',(lid,segment,len(segment)))
                                    db.executemany('INSERT INTO chars VALUES(?,?)',[(c,lid) for c in set(segment)])
                                db.execute('INSERT INTO occurrences VALUES(?,?,?)',(pid,pos,lid))
                db.execute('INSERT INTO education_entries VALUES(?,?,?,?)',(entry['id'],pid,status,json.dumps(entry,ensure_ascii=False)))
                if status!='verified':continue
                if not pid:raise ValueError('已校对作品没有正文: '+entry['id'])
                db.execute('INSERT OR IGNORE INTO education_verified VALUES(?)',(pid,))
                length=db.execute('SELECT count(*) FROM occurrences WHERE poem_id=?',(pid,)).fetchone()[0]
                start,end=entry.get('range',[0,length])
                if not 0<=start<end<=length:raise ValueError('选文范围无效: '+entry['id'])
                for stage in entry['stages']:
                    db.execute('INSERT INTO education_work_stage VALUES(?,?,?,?,?)',(pid,stage,start,end,entry['id']))
                    text=json.loads(db.execute('SELECT paragraphs FROM poems WHERE id=?',(pid,)).fetchone()[0])
                    segments=pieces(text)
                    db.execute('INSERT OR IGNORE INTO education_stage_spans VALUES(?,?,?,?)',(
                        pid,stage,sum(len(x) for x in segments[:start]),sum(len(x) for x in segments[:end])))
                    stats['stage_links']+=1
            db.execute('CREATE INDEX education_poem_title_author ON poems(title,author)')
            for rec in recommendations:
                if rec.get('hold'):
                    stats['recommendations_held'].append(rec['id'])
                    continue
                title=simplify(rec['title']);author=simplify(rec['author'])
                matched=[r[0] for r in db.execute('SELECT id FROM poems WHERE title=? AND author=?',(title,author))]
                if len(matched)!=1:stats['recommendations_unmatched'].append(rec['id']);continue
                stats['recommendations_matched']+=1
                for pid in matched:
                    segments=pieces(json.loads(db.execute('SELECT paragraphs FROM poems WHERE id=?',(pid,)).fetchone()[0]))
                    if not segments:continue
                    db.execute('INSERT OR IGNORE INTO education_work_stage VALUES(?,?,?,?,?)',(pid,rec['stage'],0,len(segments),rec['id']))
                    db.execute('INSERT OR IGNORE INTO education_stage_spans VALUES(?,?,?,?)',(pid,rec['stage'],0,sum(len(x) for x in segments)))
            for correction in corrections:
                old=correction['old_poem_id'];new_id=correction['new_entry_id']
                target=db.execute('SELECT poem_id FROM education_entries WHERE id=?',(new_id,)).fetchone()
                if not target or not target[0] or not db.execute('SELECT 1 FROM poems WHERE id=?',(old,)).fetchone():
                    raise ValueError('校订版本引用失效: '+correction['id'])
                db.execute('INSERT INTO education_corrections VALUES(?,?)',(correction['id'],json.dumps(correction,ensure_ascii=False)))
                stats['corrections']+=1
                if correction['classification']=='error':
                    for pos in correction['invalid_positions']:
                        if not db.execute('SELECT 1 FROM occurrences WHERE poem_id=? AND pos=?',(old,pos)).fetchone():
                            raise ValueError('错误句序不存在: '+correction['id'])
                        db.execute('INSERT INTO education_invalid_occurrences VALUES(?,?,?)',(old,pos,correction['id']))
                elif correction['classification']!='variant':raise ValueError('校订分类无效')
                for old_pos,new_pos in correction.get('equivalent_positions',[]):
                    a=db.execute('SELECT line_id FROM occurrences WHERE poem_id=? AND pos=?',(old,old_pos)).fetchone()
                    b=db.execute('SELECT line_id FROM occurrences WHERE poem_id=? AND pos=?',(target[0],new_pos)).fetchone()
                    if not a or not b:raise ValueError('等价句序不存在: '+correction['id'])
                    group='equiv:'+min(a[0],b[0]).__str__()
                    for lid in (a[0],b[0]):
                        previous=db.execute('SELECT group_id FROM education_equivalents WHERE line_id=?',(lid,)).fetchone()
                        if previous and previous[0]!=group:raise ValueError('诗句属于多个等价组')
                        db.execute('INSERT OR IGNORE INTO education_equivalents VALUES(?,?)',(lid,group))
            db.execute('''INSERT INTO education_inactive_works SELECT p.id FROM poems p
                WHERE EXISTS(SELECT 1 FROM education_invalid_occurrences x WHERE x.poem_id=p.id)
                AND NOT EXISTS(SELECT 1 FROM education_corrections c WHERE json_extract(c.body,'$.old_poem_id')=p.id AND json_extract(c.body,'$.classification')='variant')''')
            db.execute('''INSERT INTO education_inactive_lines SELECT l.id FROM lines l WHERE
                EXISTS(SELECT 1 FROM education_invalid_occurrences x JOIN occurrences o ON o.poem_id=x.poem_id AND o.pos=x.pos WHERE o.line_id=l.id)
                AND NOT EXISTS(SELECT 1 FROM occurrences o WHERE o.line_id=l.id AND NOT EXISTS(
                    SELECT 1 FROM education_invalid_occurrences x WHERE x.poem_id=o.poem_id AND x.pos=o.pos))''')
            db.execute('''INSERT INTO education_inactive_pairs SELECT q.id FROM pairs q WHERE
                NOT EXISTS(SELECT 1 FROM occurrences a JOIN occurrences b ON b.poem_id=a.poem_id AND b.pos=a.pos+1
                    WHERE a.line_id=q.first_id AND b.line_id=q.second_id
                    AND NOT EXISTS(SELECT 1 FROM education_invalid_occurrences x WHERE x.poem_id=a.poem_id AND x.pos=a.pos)
                    AND NOT EXISTS(SELECT 1 FROM education_invalid_occurrences x WHERE x.poem_id=b.poem_id AND x.pos=b.pos))''')
            for pid,stage,start,end,entry_id in db.execute('SELECT * FROM education_work_stage'):
                verified=bool(db.execute('SELECT 1 FROM education_verified WHERE poem_id=?',(pid,)).fetchone())
                rows=list(db.execute('SELECT pos,line_id FROM occurrences WHERE poem_id=? AND pos>=? AND pos<? ORDER BY pos',(pid,start,end)))
                for pos,lid in rows:db.execute('INSERT OR IGNORE INTO education_line_stage VALUES(?,?,?,?,?)',(lid,stage,pid,pos,int(verified)))
            # Rebuild pair indexes if new works were added. Existing IDs remain stable.
            if stats['new_poems']:
                from scripts.extend_pairs import add_pairs
                add_pairs(db)
            for pid,stage,start,end,entry_id in db.execute('SELECT * FROM education_work_stage'):
                verified=bool(db.execute('SELECT 1 FROM education_verified WHERE poem_id=?',(pid,)).fetchone())
                for first,second in db.execute('''SELECT a.line_id,b.line_id FROM occurrences a
                    JOIN occurrences b ON b.poem_id=a.poem_id AND b.pos=a.pos+1
                    WHERE a.poem_id=? AND a.pos>=? AND b.pos<?''',(pid,start,end)):
                    pair=db.execute('SELECT id FROM pairs WHERE first_id=? AND second_id=?',(first,second)).fetchone()
                    if pair:db.execute('INSERT OR IGNORE INTO education_pair_stage VALUES(?,?,?,?)',(pair[0],stage,pid,int(verified)))
            add_indexes(db)
            version_hash=hashlib.sha256((''.join(digest(x) for x in (sources_path,poems_path,corrections_path,recommendations_path) if x.exists())).encode()).hexdigest()[:16]
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('education','1.4.0-'+version_hash))
            if sources_path == SOURCES and poems_path == POEMS and corrections_path == CORRECTIONS:
                from scripts.apply_current_books import apply as apply_current_books
                stats['current_books'] = apply_current_books(db)
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('诗库完整性检查失败')
            stats.update(corpus_version='1.4.0-'+version_hash,elapsed_seconds=round(time.monotonic()-began,2),new_poem_count=db.execute('SELECT count(*) FROM poems').fetchone()[0])
        report.mkdir(parents=True,exist_ok=True)
        (report/'education-build.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2))
        return stats
    except BaseException:
        output.unlink(missing_ok=True)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=pathlib.Path,default=CORPUS)
    parser.add_argument('--output',required=True,type=pathlib.Path)
    parser.add_argument('--report',required=True,type=pathlib.Path)
    args=parser.parse_args()
    print(json.dumps(build(args.source,args.output,args.report),ensure_ascii=False))
