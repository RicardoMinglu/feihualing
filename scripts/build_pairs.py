"""Add natural couplets to an offline copy; preserve all original IDs and tables."""
import argparse, json, os, pathlib, re, sqlite3, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import CORPUS, ROOT
from app.text import normalize, HAN, simplify

def natural_positions(paragraphs, source, title):
    # The importer splits these exact separators. Never compress position gaps.
    if source.startswith('宋词/') or re.search(r'残|断句|存目|^句$', title):
        return []
    parts = re.split(r'[，。！？；、,!?;\n]+', ''.join(paragraphs))
    if parts and not parts[-1].strip(): parts.pop()
    parts = [p.strip(' “”—「」『』：:') for p in parts]
    if len(parts) < 2 or len(parts) % 2: return []
    if not all(HAN.fullmatch(p) for p in parts): return []
    if {len(p) for p in parts} not in ({5}, {7}): return []
    return [(i, i + 1) for i in range(0, len(parts), 2)]

def build(source, output, curated_path=ROOT/'data/curated-pairs.json', common_path=ROOT/'data/common-poems.json'):
    started = time.monotonic()
    if source.resolve() == output.resolve(): raise ValueError('输出必须是独立文件，验证后再替换原库')
    if output.exists(): raise ValueError('输出文件已存在，请使用新的文件名')
    overrides = json.loads(curated_path.read_text())
    common = {p['id']: p for p in json.loads(common_path.read_text())}
    curated = {}
    for key, positions in overrides.items():
        p = common[key]
        expected = [normalize(x) for x in p['lines']]
        for a,b in positions:
            if b != a+1 or a < 0 or b >= len(expected): raise ValueError('校对清单包含非相邻句')
        curated[(simplify(p['title']), simplify(p['author']))] = (positions, expected)
    src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    db = sqlite3.connect(output);db.row_factory = sqlite3.Row
    try:
        src.backup(db);src.close()
        db.executescript('''
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        DROP TABLE IF EXISTS pair_chars;
        DROP TABLE IF EXISTS pair_members;
        DROP TABLE IF EXISTS pairs;
        CREATE TABLE pairs(id TEXT PRIMARY KEY,first_id INTEGER,second_id INTEGER,poem_id TEXT,pos INTEGER,rank INTEGER,UNIQUE(first_id,second_id));
        CREATE TABLE pair_chars(char TEXT,pair_id TEXT,rank INTEGER,sort_id INTEGER,PRIMARY KEY(char,pair_id)) WITHOUT ROWID;
        CREATE TABLE pair_members(line_id INTEGER PRIMARY KEY);
        ''')
        report={'version':1,'poems_examined':0,'auto_poems':0,'curated_poems':0,'curated_pairs':0,'pairs':0}
        matched=set()
        poems=db.execute('SELECT * FROM poems ORDER BY CASE WHEN source=? THEN 0 ELSE 1 END,id',('本项目常见诗词校对集',))
        for p in poems:
            report['poems_examined']+=1
            key=(p['title'],p['author'])
            manual=p['source']=='本项目常见诗词校对集' and key in curated
            positions=curated[key][0] if manual else natural_positions(json.loads(p['paragraphs']),p['source'],p['title'])
            if not positions: continue
            rows={r['pos']:dict(r) for r in db.execute('SELECT o.pos,l.id,l.text FROM occurrences o JOIN lines l ON l.id=o.line_id WHERE o.poem_id=? ORDER BY o.pos',(p['id'],))}
            if manual:
                if [r['text'] for r in rows.values()]!=curated[key][1]:raise ValueError('校对作品与清单不一致: '+p['title'])
                matched.add(key)
            elif set(rows)!=set(range(len(positions)*2)):
                continue
            report['curated_poems' if manual else 'auto_poems']+=1
            for a,b in positions:
                left,right=rows[a],rows[b]
                if left['id']==right['id']: continue
                pair_id=f"pair:{left['id']}:{right['id']}"
                rank=0 if p['source']=='本项目常见诗词校对集' else 10
                result=db.execute('INSERT OR IGNORE INTO pairs VALUES(?,?,?,?,?,?)',(pair_id,left['id'],right['id'],p['id'],a,rank))
                if not result.rowcount: continue
                report['pairs']+=1
                if manual:report['curated_pairs']+=1
                db.executemany('INSERT INTO pair_chars VALUES(?,?,?,?)',[(ch,pair_id,rank,left['id']) for ch in set(left['text']+right['text'])])
            if report['poems_examined']%20000==0:
                db.commit();print(json.dumps(report,ensure_ascii=False),flush=True)
        if matched!=set(curated):raise ValueError('有校对作品未找到')
        db.executescript('''
        INSERT OR IGNORE INTO pair_members SELECT first_id FROM pairs;
        INSERT OR IGNORE INTO pair_members SELECT second_id FROM pairs;
        CREATE INDEX pair_char_order ON pair_chars(char,rank,sort_id,pair_id);
        CREATE INDEX pair_first ON pairs(first_id);
        CREATE INDEX pair_second ON pairs(second_id);
        ''')
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('pairing',json.dumps(report)))
        db.commit()
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('配对库完整性检查失败')
        return report
    except BaseException:
        db.close();src.close();output.unlink(missing_ok=True);raise
    finally:db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=pathlib.Path,default=CORPUS)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    parser.add_argument('--report',type=pathlib.Path)
    args=parser.parse_args()
    report=build(args.source,args.output)
    if args.report:args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
