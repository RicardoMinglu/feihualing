"""Use SQLite's online backup API, so committed WAL data is included."""
import argparse,datetime,hashlib,json,pathlib,sqlite3,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import DATA

parser=argparse.ArgumentParser()
parser.add_argument('--destination',default='backups')
args=parser.parse_args()
target=pathlib.Path(args.destination)/datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
target.mkdir(parents=True,exist_ok=False)
manifest={}
for name in ('poetry.sqlite3','games.sqlite3'):
    source=DATA/name
    if not source.exists():continue
    src=sqlite3.connect(f'file:{source}?mode=ro',uri=True);dst=sqlite3.connect(target/name)
    try:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('Backup validation failed')
    finally:src.close();dst.close()
    with (target/name).open('rb') as f:manifest[name]=hashlib.file_digest(f,'sha256').hexdigest()
(target/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(target.resolve())
