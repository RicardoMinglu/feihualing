import sqlite3,json,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from app.corpus import Corpus
from app.games import Games,GameError
from app.text import normalize,word

@pytest.fixture(scope='module')
def corpus():return Corpus()
@pytest.fixture
def games(corpus,tmp_path):return Games(corpus,tmp_path/'games.sqlite3')

def test_corpus_and_search(corpus):
    stats=corpus.stats();assert stats['poems']>300000;assert stats['source_files']==336
    rows=corpus.search('月',0,20);assert rows['total']>1000
    assert all('月' in r['text'] for r in rows['items'])
    assert corpus.poem(rows['items'][0]['poem_id'])['original']
    assert {r['id'] for r in rows['items']}.isdisjoint(r['id'] for r in corpus.search('月',20,20)['items'])

@pytest.mark.parametrize('text,char', [('床前明月光','月'),('床前明月光，疑是地上霜','月'),('海上生明月天涯共此时','月'),('舉頭望明月','月')])
def test_exact(corpus,text,char):assert corpus.judge(text,char)['kind']=='accepted'

def test_lingdingyang_standard_text(corpus):
    single=corpus.judge('身世浮沉雨打萍','萍')
    pair=corpus.judge('山河破碎风飘絮，身世浮沉雨打萍','萍')
    assert single['kind']=='accepted' and not single['corrected']
    assert pair['kind']=='accepted' and not pair['corrected']
    assert single['option']['title']==pair['option']['title']=='过零丁洋'
    assert pair['option']['clauses']==['山河破碎风飘絮','身世浮沉雨打萍']
    assert corpus.poem(single['option']['poem_id'])['source_url']=='https://www.gushici.net/shici/57/58863.html'

@pytest.mark.parametrize('text,expected',[
    ('明月几时有千里共婵娟','invalid'),
    ('明月几时有，千里共婵娟','invalid'),
    ('明月几时有把酒问青天','accepted'),
    ('明月几时有，把酒问青天','accepted'),
])
def test_same_work_still_requires_adjacent_clauses(corpus,text,expected):
    assert corpus.judge(text,'月')['kind']==expected

def test_correction(corpus):
    r=corpus.judge('床前明悦光','月');assert r['kind']=='accepted' and r['corrected'];assert r['option']['text']=='床前明月光'
    r=corpus.judge('海上升明月','月');assert r['kind'] in ('accepted','candidates')
    r=corpus.judge('床前明月广','月');assert r['kind']=='accepted' and r['corrected']
    r=corpus.judge('床前明月亮','月');assert r['kind']=='candidates'
    r=corpus.judge('海上升明月天涯共此时','月');assert r['kind']=='accepted' and len(r['option']['line_ids'])==2
    r=corpus.judge('床前明悦光，疑是地上霜','月');assert r['kind']=='accepted'
    r=corpus.judge('床前啊月光','月');assert r['kind']=='candidates' and 2<=len(r['options'])<=3

def test_invalid(corpus):
    assert corpus.judge('春眠不觉晓','月')['kind']=='missing_word'
    assert corpus.judge('床前明月光，海上生明月','月')['kind']=='invalid'
    assert corpus.judge('我今天想吃麻辣火锅','月')['kind']=='invalid'
    assert corpus.judge('明月','月')['kind']=='accepted' # A real two-character clause in 调笑令.
    assert corpus.judge('明悦','月')['kind']=='invalid' # Short clauses have no typo tolerance.
    assert corpus.judge('<script>床前明月光','月')['kind']=='invalid'

def play(games,gid,key,text):
    cached=games.reserve(gid,'owner',key,'text:'+text)
    if cached:return cached
    return games.finish(gid,'owner',key,games.corpus.judge(text,games.get(gid,'owner')['word']))

@pytest.mark.parametrize('difficulty,target,seconds,hints',[('easy',5,None,2),('normal',10,None,0),('hard',15,None,0)])
def test_complete_games(games,difficulty,target,seconds,hints):
    g=games.create('owner','月',difficulty,'create-'+difficulty);assert g['level']['seconds']==seconds and g['hints']==hints
    for i in range(target):
        used=games.get(g['id'],'owner')['used'];r=next(x for x in games.corpus.pool('月') if x['id'] not in used)
        response=play(games,g['id'],'turn-'+str(i),r['text']);assert response['verdict']['kind']=='accepted'
    assert response['game']['status']=='won' and response['game']['round']==target
    assert response['game']['messages']

def test_idempotence_and_restore(games):
    g=games.create('owner','月','easy','create-request');assert games.create('owner','月','easy','create-request')['id']==g['id']
    a=play(games,g['id'],'answer-request','床前明月光');b=play(games,g['id'],'answer-request','床前明月光');assert a['verdict']==b['verdict'] and a['game']['round']==b['game']['round']
    assert Games(games.corpus,games.path).get(g['id'],'owner')['round']==1
    assert play(games,g['id'],'repeat-request','床前明月光')['verdict']['kind']=='duplicate'
    with pytest.raises(GameError):games.get(g['id'],'other')

def test_pair_used_and_hints(games):
    g=games.create('owner','月','easy','pair-request')
    r=play(games,g['id'],'pair-answer','床前明月光，疑是地上霜');assert len(r['game']['messages'][0]['line_ids'])==2
    hints=[games.action(g['id'],'owner','hint-'+str(i),'hint')['verdict'] for i in range(2)]
    assert all(h['kind']=='hint' for h in hints) and hints[0]['title']!=hints[1]['title']
    with pytest.raises(GameError):games.action(g['id'],'owner','hint-3','hint')

def test_timeout_and_processing(games):
    g=games.create('owner','月','easy','timeout-create')
    games.reserve(g['id'],'owner','audio-request','audio:hash')
    with games.tx() as db:
        x=json.loads(db.execute('SELECT state FROM games').fetchone()[0]);x['processing_until']=time.time()-1;x['remaining']=30;games.save(db,x)
    restored=games.get(g['id'],'owner');assert restored['status']=='playing';assert restored['deadline'] is None
    late=games.finish(g['id'],'owner','audio-request',games.corpus.judge('床前明月光','月'));assert late['verdict']['kind']=='error';assert late['game']['round']==0
    with games.tx() as db:
        x=json.loads(db.execute('SELECT state FROM games').fetchone()[0]);x['deadline']=time.time()-1;games.save(db,x)
    assert games.get(g['id'],'owner')['status']=='playing'

def test_candidates_bound_to_turn(games):
    g=games.create('owner','月','easy','candidate-create');r=play(games,g['id'],'candidate-input','床前明月亮')
    assert r['verdict']['kind']=='candidates';candidate=r['game']['candidates'][0]['candidate_id']
    r=games.action(g['id'],'owner','candidate-confirm','confirm',candidate);assert r['game']['round']==1
    with pytest.raises(GameError):games.action(g['id'],'owner','candidate-repeat','confirm',candidate)

def test_duplicate_concurrent_request(games):
    g=games.create('owner','月','easy','parallel-create')
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:play(games,g['id'],'same-request','海上生明月'),range(2)))
    assert games.get(g['id'],'owner')['round']==1

def test_unknown_word(games):
    with pytest.raises(GameError):games.create('owner','龘','hard','unknown-create')

def test_bot_exhaustion(games,monkeypatch):
    g=games.create('owner','月','hard','exhaust-create')
    monkeypatch.setattr(games.corpus,'computer_options',lambda *_:[])
    r=play(games,g['id'],'exhaust-answer','床前明月光')
    assert r['game']['status']=='won' and r['game']['round']==1
    assert '无可接' in r['game']['reason']

def test_bot_pair_usage_and_player_single(games):
    g=games.create('owner','月','easy','natural-pair-create')
    r=play(games,g['id'],'natural-pair-answer','床前明月光')
    player,bot=r['game']['messages']
    assert len(player['line_ids'])==1 and player['text']=='床前明月光'
    assert len(bot['line_ids'])==2 and bot['text'].endswith('。')
    assert all(lid in r['game']['used'] for lid in bot['line_ids'])
    for i,clause in enumerate(bot['clauses']):
        if '月' in clause:assert play(games,g['id'],'repeat-bot-'+str(i),clause)['verdict']['kind']=='duplicate'
    restored=Games(games.corpus,games.path).get(g['id'],'owner')
    assert restored['messages']==r['game']['messages']

@pytest.mark.parametrize('difficulty',['easy','normal','hard','endless'])
def test_no_answer_deadline(games,difficulty,monkeypatch):
    g=games.create('owner','月',difficulty,'no-deadline-'+difficulty)
    monkeypatch.setattr('app.games.time.time',lambda:10**12)
    r=games.get(g['id'],'owner');assert r['status']=='playing' and r['deadline'] is None
    assert play(games,g['id'],'late-answer','床前明月光')['game']['round']==1

def test_legacy_migration_keeps_actual_messages(games):
    g=games.create('owner','月','easy','legacy')
    old=dict(g,deadline=1,messages=[{'text':'旧实际诗句','line_ids':[1],'who':'player','round':1}],used=[1],round=1)
    old.pop('storage_version',None);old.pop('message_count',None)
    with games.tx() as db:db.execute('UPDATE games SET state=? WHERE id=?',(json.dumps(old),g['id']))
    restored=games.get(g['id'],'owner');assert restored['status']=='playing' and restored['deadline'] is None
    assert restored['messages'][0]['text']=='旧实际诗句' and restored['messages'][0]['seq']==1
    assert restored['used']==[1]
    with games.tx() as db:
        old['status']='lost';db.execute('UPDATE games SET state=? WHERE id=?',(json.dumps(old),g['id']))
    assert games.get(g['id'],'owner')['status']=='lost'

class LongCorpus:
    def pool(self,char):return list(range(10000))
    def computer_options(self,char,used):
        lid=max(used)+1
        return [dict(line_ids=[lid],text='月照长夜',title='测试诗',author='测试作者',dynasty='唐')]
    def available(self,char,used):return [{'id':99999}]
    def detail(self,lid):return dict(poem_id='hint-poem',title='提示诗',author='测试作者',dynasty='唐')

def test_long_game_storage_and_paging(tmp_path):
    game=Games(LongCorpus(),tmp_path/'long.sqlite3');g=game.create('owner','月','endless','long')
    sizes=[]
    for i in range(1000):
        key='turn-'+str(i);game.reserve(g['id'],'owner',key,key)
        r=game.finish(g['id'],'owner',key,{'kind':'accepted','option':dict(line_ids=[i*2+1],text='月色依然',title='测试诗',author='测试作者',dynasty='唐')})
        assert r['game']['status']=='playing'
        if i in (499,999):
            with game.tx() as db:sizes.append(db.execute('SELECT sum(length(body)) FROM messages').fetchone()[0]+db.execute('SELECT sum(length(response)) FROM requests').fetchone()[0])
    assert sizes[1]<sizes[0]*2.1
    restored=game.get(g['id'],'owner');assert restored['round']==1000 and len(restored['messages'])==50
    assert restored['messages'][-1]['seq']==2000 and 'used' not in restored
    page=game.history(g['id'],'owner',1951);assert [m['seq'] for m in page['items']]==list(range(1901,1951))
    assert [m['seq'] for m in game.get(g['id'],'owner',1998)['messages']]==[1999,2000]
    repeat=game.reserve(g['id'],'owner','turn-0','turn-0');assert repeat['game']['round']==1000
    with game.tx() as db:
        assert 'messages' not in json.loads(db.execute('SELECT state FROM games').fetchone()[0])
        assert 'game' not in json.loads(db.execute('SELECT response FROM requests LIMIT 1').fetchone()[0])
    for i in range(5):assert game.action(g['id'],'owner','hint-'+str(i),'hint')['verdict']['title']=='提示诗'
    assert game.get(g['id'],'owner')['hints'] is None
    assert game.action(g['id'],'owner','end','end')['game']['status']=='abandoned'
