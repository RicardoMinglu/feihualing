import json
import sqlite3

from app.corpus import Corpus
from app.works import Works
from scripts.build_education import build
from scripts.apply_current_books import apply as apply_current_books
from scripts.apply_current_books import _match
from scripts.migrate_business import migrate


def test_education_copy_and_variant(tmp_path):
    source=tmp_path/'source.sqlite3';output=tmp_path/'output.sqlite3'
    with sqlite3.connect(source) as db:
        db.executescript('''
        CREATE TABLE poems(id TEXT PRIMARY KEY,title TEXT,author TEXT,dynasty TEXT,paragraphs TEXT,original TEXT,source TEXT);
        CREATE TABLE lines(id INTEGER PRIMARY KEY,text TEXT UNIQUE,n INTEGER,rank INTEGER);
        CREATE TABLE occurrences(poem_id TEXT,pos INTEGER,line_id INTEGER,PRIMARY KEY(poem_id,pos));
        CREATE INDEX occurrence_line ON occurrences(line_id,poem_id,pos);
        CREATE TABLE chars(char TEXT,line_id INTEGER,PRIMARY KEY(char,line_id));
        CREATE TABLE pairs(id TEXT PRIMARY KEY,first_id INTEGER,second_id INTEGER,poem_id TEXT,pos INTEGER,rank INTEGER,UNIQUE(first_id,second_id));
        CREATE TABLE pair_chars(char TEXT,pair_id TEXT,rank INTEGER,sort_id INTEGER,PRIMARY KEY(char,pair_id));
        CREATE TABLE pair_members(line_id INTEGER PRIMARY KEY);
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        ''')
        db.execute('INSERT INTO meta VALUES(?,?)',('pairing','{"pairs":2}'))
        db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',('old','早发白帝城','李白','唐',json.dumps(['朝辞白帝彩云间，千里江陵一日还。','两岸猨声啼不尽，轻舟已过万重山。']),'原文','测试'))
        for i,line in enumerate(['朝辞白帝彩云间','千里江陵一日还','两岸猨声啼不尽','轻舟已过万重山'],1):
            db.execute('INSERT INTO lines VALUES(?,?,?,10)',(i,line,len(line)))
            db.execute('INSERT INTO occurrences VALUES(?,?,?)',('old',i-1,i))
            db.executemany('INSERT INTO chars VALUES(?,?)',[(c,i) for c in set(line)])
        for a,b in [(1,2),(3,4)]:
            pair=f'pair:{a}:{b}'
            db.execute('INSERT INTO pairs VALUES(?,?,?,?,?,10)',(pair,a,b,'old',a-1))
            db.executemany('INSERT INTO pair_chars VALUES(?,?,10,?)',[(c,pair,a) for c in set(db.execute('SELECT text FROM lines WHERE id=?',(a,)).fetchone()[0]+db.execute('SELECT text FROM lines WHERE id=?',(b,)).fetchone()[0])])
            db.executemany('INSERT OR IGNORE INTO pair_members VALUES(?)',[(a,),(b,)])
        db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',('extra','测试归属','李白','唐',json.dumps(['朝辞白帝彩云间，千里江陵一日还。']),'原文','测试'))
        db.executemany('INSERT INTO occurrences VALUES(?,?,?)',[('extra',0,1),('extra',1,2)])
    sources=tmp_path/'sources.json';sources.write_text(json.dumps([{'id':'evidence'}]))
    entries=tmp_path/'entries.json';entries.write_text(json.dumps([{
        'id':'baidi','title':'早发白帝城','author':'李白','dynasty':'唐','status':'verified',
        'stages':['primary'],'source_ids':['evidence'],'evidence':'印刷页已核对',
        'paragraphs':['朝辞白帝彩云间，千里江陵一日还。','两岸猿声啼不住，轻舟已过万重山。'],
        'range':[0,2]}],ensure_ascii=False))
    corrections=tmp_path/'corrections.json';corrections.write_text(json.dumps([{
        'id':'variant','old_poem_id':'old','new_entry_id':'baidi','classification':'variant',
        'equivalent_positions':[[2,2]]}]))
    recommendations=tmp_path/'recommendations.json'
    recommendations.write_text(json.dumps([
        {'id':'unique','title':'测试归属','author':'李白','stage':'senior','source_id':'evidence'},
        {'id':'ambiguous','title':'早发白帝城','author':'李白','stage':'senior','source_id':'evidence'},
        {'id':'held','title':'测试归属','author':'李白','stage':'junior','source_id':'evidence','hold':'节选待核实'}]))
    result=build(source,output,tmp_path/'report',sources,entries,corrections,recommendations)
    assert result['new_poems']==1 and result['new_lines']==1
    assert result['recommendations_matched']==1
    assert result['recommendations_unmatched']==['ambiguous']
    assert result['recommendations_held']==['held']
    corpus=Corpus(output)
    assert corpus.exact('两岸猿声啼不住')
    assert corpus.judge('两岸猿声啼不住，轻舟已过万重山','山')['kind']=='accepted'
    assert len(corpus.equivalent_ids({3}))==2
    rows=corpus.search('朝',0,20,'pair','primary')
    assert rows['items'][0]['stage_match'] and rows['items'][0]['verification']=='verified'
    assert Works(corpus).search('text','朝辞白帝',0,20,'primary')['items'][0]['stage_match']
    assert not Works(corpus).search('text','猿声啼不住',0,20,'primary')['items'][0]['stage_match']
    with corpus.connect() as db:
        assert db.execute("SELECT stage FROM education_work_stage WHERE poem_id='extra'").fetchall()[0][0]=='senior'
    with sqlite3.connect(output) as db:
        db.execute("INSERT OR IGNORE INTO education_line_stage VALUES(4,'senior','old',3,0)")
    assert corpus.search('山',0,5,'single','all')['items'][0]['text']=='轻舟已过万重山'
    books=tmp_path/'books.json'
    books.write_text(json.dumps([{'id':'123','stage':'junior','volume':'测试册','toc_pages':[5],
        'image_dir':'page','works':[['测试归属','李白']]}]))
    with sqlite3.connect(output) as db:
        result=apply_current_books(db,books)
        assert result['matched']==1 and result['removed_legacy_stage_links']>=1
        assert db.execute("SELECT stage FROM education_work_stage WHERE poem_id='extra' ORDER BY stage").fetchall()==[('junior',),('senior',)]
        assert db.execute("SELECT count(*) FROM education_work_stage WHERE stage='primary'").fetchone()[0]==0
        again=apply_current_books(db,books)
        assert again['corpus_version']==result['corpus_version']
        assert again['stages']==result['stages']
    with sqlite3.connect(source) as old,sqlite3.connect(output) as new:
        assert old.execute('SELECT id,text FROM lines ORDER BY id').fetchall()==new.execute('SELECT id,text FROM lines WHERE id<=4 ORDER BY id').fetchall()
        assert old.execute('SELECT id,paragraphs FROM poems ORDER BY id').fetchall()==new.execute("SELECT id,paragraphs FROM poems WHERE id IN ('old','extra') ORDER BY id").fetchall()


def test_business_migration_is_repeatable(tmp_path):
    path=tmp_path/'games.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE games(id TEXT PRIMARY KEY,owner TEXT,state TEXT)')
        db.execute('CREATE TABLE messages(game_id TEXT,seq INTEGER,body TEXT,PRIMARY KEY(game_id,seq))')
        state={'id':'a'*32,'word':'月','difficulty':'easy','status':'won','reason':'已完成',
               'created_at':123,'round':1,'messages':[{'who':'player','text':'明月'}]}
        db.execute('INSERT INTO games VALUES(?,?,?)',('a'*32,'owner',json.dumps(state)))
        assert migrate(db)==1
        assert migrate(db)==0
        row=db.execute('SELECT player_answer_count,ended_at FROM game_summaries').fetchone()
        assert row==(1,None)


def test_current_book_match_keeps_text_variants_ambiguous():
    from app.config import CORPUS
    with sqlite3.connect(f'file:{CORPUS}?mode=ro', uri=True) as db:
        assert len(_match(db,'小池','杨万里'))==1
        assert len(_match(db,'泊船瓜洲','王安石'))==2
        assert len(_match(db,'绝句','杜甫','迟日江山丽'))==1
        assert _match(db,'枫桥夜泊','张继','月落乌啼霜满天','江枫渔火对愁眠')==[
            '6908bbcd6e887b0b8151f445']
