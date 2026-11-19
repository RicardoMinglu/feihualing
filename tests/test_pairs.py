import json, sqlite3
import pytest
from app.corpus import Corpus
from scripts.build_pairs import build, natural_positions

@pytest.mark.parametrize('parts,source,expected',[
    (['床前明月光，疑是地上霜。','举头望明月，低头思故乡。'],'全唐诗/poet.tang.0.json',[(0,1),(2,3)]),
    (['床前明月光，疑是地上霜，举头望明月。'],'全唐诗/poet.tang.0.json',[]),
    (['床前明月光，□是地上霜。'],'全唐诗/poet.tang.0.json',[]),
    (['床前明月光，疑是地上霜。'],'宋词/ci.song.0.json',[]),
    (['大江东去，浪淘尽，千古风流人物。'],'全唐诗/poet.tang.0.json',[]),
])
def test_conservative_pairing(parts,source,expected):
    assert natural_positions(parts,source,'测试')==expected

@pytest.fixture
def tiny_pairs(tmp_path):
    source=tmp_path/'original.sqlite3';output=tmp_path/'paired.sqlite3'
    db=sqlite3.connect(source)
    db.executescript('''CREATE TABLE poems(id TEXT PRIMARY KEY,title,author,dynasty,paragraphs,original,source);
    CREATE TABLE lines(id INTEGER PRIMARY KEY,text UNIQUE,n,rank);
    CREATE TABLE occurrences(poem_id,pos,line_id,PRIMARY KEY(poem_id,pos));
    CREATE TABLE chars(char,line_id,PRIMARY KEY(char,line_id));
    CREATE TABLE meta(key PRIMARY KEY,value);''')
    texts=['床前明月光','疑是地上霜','举头望明月','低头思故乡','月明星稀','乌鹊南飞']
    for lid,text in enumerate(texts,1):
        db.execute('INSERT INTO lines VALUES(?,?,?,0)',(lid,text,len(text)))
        db.executemany('INSERT INTO chars VALUES(?,?)',[(c,lid) for c in set(text)])
    paragraphs=['床前明月光，疑是地上霜。','举头望明月，低头思故乡。']
    for pid in ['a','b']:
        db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',(pid,'静夜思','李白','唐',json.dumps(paragraphs),'原文','全唐诗/poet.tang.0.json'))
        db.executemany('INSERT INTO occurrences VALUES(?,?,?)',[(pid,i,i+1) for i in range(4)])
    db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',('c','短歌','佚名','宋',json.dumps(['月明星稀，乌鹊南飞。']),'原文','宋词/ci.song.0.json'))
    db.executemany('INSERT INTO occurrences VALUES(?,?,?)',[('c',0,5),('c',1,6)])
    db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',('d','断档样例','佚名','唐',json.dumps(paragraphs),'原文','全唐诗/poet.tang.0.json'))
    db.executemany('INSERT INTO occurrences VALUES(?,?,?)',[('d',0,1),('d',2,3),('d',3,4)])
    db.commit();db.close()
    curated=tmp_path/'curated.json';curated.write_text('{}')
    common=tmp_path/'common.json';common.write_text('[]')
    report=build(source,output,curated,common)
    assert report['pairs']==2
    for table in ['poems','lines','occurrences','chars']:
        a=sqlite3.connect(source);b=sqlite3.connect(output)
        assert a.execute('SELECT * FROM '+table).fetchall()==b.execute('SELECT * FROM '+table).fetchall()
        a.close();b.close()
    return Corpus(output)

def test_pair_pagination_and_single_fallback(tiny_pairs):
    c=tiny_pairs
    r=c.search('月',0,1,'pair');assert r['total']==3 and r['has_more']
    assert r['items'][0]['text']=='床前明月光，疑是地上霜。'
    assert c.search('月',1,1,'pair')['items'][0]['line_ids']==[3,4]
    final=c.search('月',2,2,'pair')
    assert final['items'][0]['unit']=='single' and not final['has_more']
    assert c.search('月',10,2,'pair')['items']==[]
    assert c.search('霜',0,20,'pair')['items'][0]['line_ids']==[1,2]
    assert len(c.search('月')['items'])==3

def test_bot_excludes_either_half_and_falls_back(tiny_pairs):
    c=tiny_pairs
    assert {tuple(x['line_ids']) for x in c.computer_options('月',set())}=={(1,2),(3,4)}
    assert [x['line_ids'] for x in c.computer_options('月',{2})]==[[3,4]]
    assert all(len(x['line_ids'])==1 for x in c.computer_options('月',{2,4}))
    assert c.computer_options('月',{1,2,3,4,5,6})==[]

def test_real_curated_pairs_and_no_cross_couplet():
    c=Corpus()
    with c.connect() as db:
        shui=db.execute("SELECT a.text,b.text FROM pairs q JOIN lines a ON a.id=q.first_id JOIN lines b ON b.id=q.second_id JOIN poems p ON p.id=q.poem_id WHERE p.title='水调歌头·明月几时有' AND p.source='本项目常见诗词校对集'").fetchall()
        assert ('明月几时有','把酒问青天') in [tuple(r) for r in shui]
        assert ('但愿人长久','千里共婵娟') in [tuple(r) for r in shui]
        assert ('我欲乘风归去','又恐琼楼玉宇') not in [tuple(r) for r in shui]
        wrong=db.execute("SELECT count(*) FROM pairs q JOIN lines a ON a.id=q.first_id JOIN lines b ON b.id=q.second_id WHERE a.text='疑是地上霜' AND b.text='举头望明月'").fetchone()[0]
        assert wrong==0
