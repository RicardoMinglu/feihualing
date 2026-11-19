import json,os,pathlib,sqlite3,subprocess,sys

def test_online_backup_includes_committed_wal(tmp_path):
    from app.config import ROOT
    source=tmp_path/'source';source.mkdir();path=source/'games.sqlite3'
    db=sqlite3.connect(path);db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE sample(value TEXT)');db.execute("INSERT INTO sample VALUES('已保存')");db.commit()
    target=tmp_path/'backup'
    subprocess.run([sys.executable,str(ROOT/'scripts/backup.py'),'--destination',str(target)],env={**os.environ,'FEIHUA_DATA_DIR':str(source)},check=True,capture_output=True)
    folders=list(target.iterdir());assert len(folders)==1
    with sqlite3.connect(folders[0]/'games.sqlite3') as restored:assert restored.execute('SELECT value FROM sample').fetchone()[0]=='已保存'
    assert 'games.sqlite3' in json.loads((folders[0]/'manifest.json').read_text())
    db.close()
