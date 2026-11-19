import json,sqlite3
import pytest
from app.corpus import Corpus
from app.works import Works
from scripts.build_search import add_indexes

@pytest.fixture
def works(tmp_path):
    path=tmp_path/'works.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE poems(id TEXT PRIMARY KEY,title,author,dynasty,paragraphs,original,source);CREATE TABLE meta(key TEXT PRIMARY KEY,value);')
        for pid,author,text in [('1','李白','床前明月光，疑是地上霜。举头望明月，低头思故乡。'),('2','李白','白日依山尽，黄河入海流。'),('3','李白甫','明月几时有，把酒问青天。')]:
            db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?)',(pid,'题'+pid,author,'唐',json.dumps([text]),text,'测试'))
        db.commit();add_indexes(db)
    return Works(Corpus(path))

def test_work_author_count_and_paging(works):
    r=works.search('author','李白',0,1);assert r['total']==3 and r['has_more'] and r['items'][0]['author']=='李白'
    assert works.search('author','李白',1,1)['items'][0]['id']!=r['items'][0]['id']
    assert works.search('author','不存在',0,20)['total']==0

def test_text_normalization_and_unique_works(works):
    assert works.search('text','明月',0,20)['total']==2
    r=works.search('text','明月光，疑是',0,20);assert r['total']==1 and '明月光,疑是' in r['items'][0]['match_text']
    assert works.search('text','舉頭望明月',0,20)['total']==1
    assert works.search('text','床前明悦光',0,20)['total']==0
    assert works.search('text','月明',0,20)['total']==0
    for mode,q in [('bad','李白'),('text','月'),('author',''),('text','abc')]:
        with pytest.raises(ValueError):works.search(mode,q,0,20)
