"""Build work-search indexes in a separate corpus copy; never alter source text."""
import argparse,json,pathlib,sqlite3,sys,time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import CORPUS
from app.text import normalize

def add_indexes(db):
    started=time.monotonic()
    db.executescript('''
    DROP TABLE IF EXISTS work_fts;
    DROP TABLE IF EXISTS work_chars;
    DROP TABLE IF EXISTS work_search;
    CREATE TABLE work_search(id INTEGER PRIMARY KEY,poem_id TEXT UNIQUE,author TEXT,body TEXT);
    CREATE INDEX work_author ON work_search(author,poem_id);
    CREATE TABLE work_chars(char TEXT,work_id INTEGER,PRIMARY KEY(char,work_id)) WITHOUT ROWID;
    CREATE VIRTUAL TABLE work_fts USING fts5(body,content='work_search',content_rowid='id',tokenize='trigram',detail='none');
    ''')
    n=0
    for pid,author,paragraphs in db.execute('SELECT id,author,paragraphs FROM poems ORDER BY id'):
        n+=1;body=normalize(''.join(json.loads(paragraphs)))
        db.execute('INSERT INTO work_search VALUES(?,?,?,?)',(n,pid,normalize(author),body))
        db.executemany('INSERT INTO work_chars VALUES(?,?)',[(c,n) for c in set(body)])
        if n%20000==0:db.commit();print('Indexed',n,flush=True)
    db.execute("INSERT INTO work_fts(work_fts) VALUES('rebuild')");db.commit()
    report={'version':1,'works':n,'elapsed_seconds':round(time.monotonic()-started,2)}
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('search',json.dumps(report)));db.commit()
    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('索引完整性检查失败')
    return report

def build(source,output):
    if source.resolve()==output.resolve() or output.exists():raise ValueError('必须使用尚不存在的独立输出文件')
    try:
        with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as src,sqlite3.connect(output) as db:
            src.backup(db);db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF')
            return add_indexes(db)
    except BaseException:
        output.unlink(missing_ok=True);raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=pathlib.Path,default=CORPUS);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--report',type=pathlib.Path)
    a=p.parse_args();report=build(a.source,a.output)
    if a.report:a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))
