import pytest
from fastapi.testclient import TestClient
from app import main
from app.games import Games
from app.accounts import Accounts
from app.community import Community,record_activity
from app import security

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'games',Games(main.corpus,tmp_path/'games.sqlite3'))
    monkeypatch.setattr(main,'accounts',Accounts(main.games.path))
    monkeypatch.setattr(main,'community',Community(main.games.path))
    monkeypatch.setattr(security,'limits',security.RateLimits())
    main.games.event_recorder=record_activity
    with TestClient(main.app) as client:
        assert client.post('/api/auth/guest',json={}).status_code==200
        yield client

def create(client):
    response=client.post('/api/games',json={'word':'月','difficulty':'easy','request_id':'create-api-test'})
    assert response.status_code==200
    return response.json()['game']

def test_lookup_validation_and_ownership(client):
    assert client.get('/').status_code==200
    assert client.get('/health').json()['status']=='ok'
    assert client.get('/api/search',params={'word':'a'}).status_code==400
    assert client.get('/api/search',params={'word':'月','limit':100}).status_code==400
    rows=client.get('/api/search',params={'word':'風'}).json()
    assert rows['word']=='风' and all('风' in r['text'] for r in rows['items'])
    assert client.get('/api/poems/'+rows['items'][0]['poem_id']).json()['original']
    game=create(client)
    client.cookies.clear();client.post('/api/auth/guest',json={})
    assert client.get('/api/games/'+game['id']).status_code==404

def test_text_retry_and_candidate_confirmation(client):
    game=create(client);path='/api/games/'+game['id']
    args={'text':'床前明月亮','request_id':'answer-api-test'}
    first=client.post(path+'/answers',json=args).json()
    assert first['verdict']['kind']=='candidates'
    assert client.post(path+'/answers',json=args).json()['verdict']==first['verdict']
    candidate=first['game']['candidates'][0]['candidate_id']
    args={'request_id':'confirm-api-test','candidate_id':candidate}
    result=client.post(path+'/confirm',json=args).json()
    assert result['game']['round']==1
    assert client.post(path+'/confirm',json=args).json()['verdict']==result['verdict']
    assert client.get(path).json()['game']['round']==1

def test_retired_audio_and_text_limits(client):
    game=create(client);path='/api/games/'+game['id']
    assert client.post(path+'/audio',content=b'').status_code==410
    assert '文字作答' in client.post(path+'/audio',content=b'').json()['detail']
    assert client.post(path+'/audio',content=b'x'*20000).status_code==410
    assert client.get('/health').json()['speech']['status']=='disabled'
    assert client.post(path+'/answers',content=b'x'*20000).status_code==413
    assert client.get(path).json()['game']['round']==0

def test_hints_and_end(client):
    game=create(client);path='/api/games/'+game['id']
    for i in range(2):assert client.post(path+'/hint',json={'request_id':f'hint-api-{i}'}).status_code==200
    assert client.post(path+'/hint',json={'request_id':'hint-api-limit'}).status_code==400
    assert client.post(path+'/end',json={'request_id':'end-api-test'}).json()['game']['status']=='abandoned'
    assert client.post(path+'/answers',json={'text':'床前明月光','request_id':'after-end-test'}).status_code==409

def test_pair_search_mode(client):
    r=client.get('/api/search',params={'word':'月','unit':'pair','limit':2}).json()
    assert r['unit']=='pair' and len(r['items'])==2
    assert r['items'][0]['clauses']==['床前明月光','疑是地上霜']
    assert len(r['items'][0]['line_ids'])==2
    next_page=client.get('/api/search',params={'word':'月','unit':'pair','limit':2,'offset':2}).json()
    assert set(x['id'] for x in r['items']).isdisjoint(x['id'] for x in next_page['items'])
    assert client.get('/api/search',params={'word':'月','unit':'bad'}).status_code==400
    assert client.get('/api/config').json()['pairing']['pairs']>1000000

def test_works_api_and_history_security(client):
    r=client.get('/api/works',params={'mode':'author','q':'李白','limit':2});assert r.status_code==200 and r.json()['total']>100
    assert client.get('/api/works',params={'mode':'text','q':'明月'}).json()['total']>0
    assert client.get('/api/works',params={'mode':'text','q':'月'}).status_code==400
    assert client.get('/api/works',params={'mode':'text','q':'明月','limit':0}).status_code==400
    g=create(client);path='/api/games/'+g['id']
    assert client.get(path+'/messages').json()['items']==[]
    assert client.get(path,params={'after':-1}).status_code==400
    assert client.get(path+'/messages',params={'limit':101}).status_code==400
    client.post(path+'/answers',json={'request_id':'history-first','text':'床前明月光'})
    data=client.get(path,params={'after':1}).json()['game'];assert [m['seq'] for m in data['messages']]==[2]
    assert len(client.get(path+'/messages').json()['items'])==2
    client.cookies.clear();client.post('/api/auth/guest',json={});assert client.get(path+'/messages').status_code==404

def test_visitor_profile_and_game_list(client):
    assert client.get('/api/me').json()['education_stage']=='all'
    assert client.patch('/api/me',json={'education_stage':'primary'}).json()=={'education_stage':'primary'}
    assert client.get('/api/me').json()['education_stage']=='primary'
    assert client.patch('/api/me',json={'education_stage':'invalid'}).status_code==400
    assert client.patch('/api/me',json={'education_stage':'all','owner':'another'}).status_code==422
    assert client.patch('/api/me',json={'education_stage':'all'},headers={'Origin':'https://other.example'}).status_code==403
    assert client.patch('/api/me',content=b'a'*20000).status_code==413
    ids=[client.post('/api/games',json={'word':'月','difficulty':'easy','request_id':f'profile-list-{i}'}).json()['game']['id'] for i in range(3)]
    first=client.get('/api/games',params={'limit':2}).json()
    assert len(first['items'])==2 and first['has_more'] and first['next_cursor']
    second=client.get('/api/games',params={'limit':2,'cursor':first['next_cursor']}).json()
    assert [x['id'] for x in first['items']+second['items']]==ids[::-1]
    assert client.get('/api/games',params={'cursor':'invalid'}).status_code==400
    assert client.get('/api/search',params={'word':'月','stage':'primary'}).json()['stage']=='primary'
    assert client.get('/api/search',params={'word':'月'}).json()['stage']=='primary'
    assert client.get('/api/works',params={'mode':'text','q':'明月'}).json()['stage']=='primary'
    client.cookies.clear();client.post('/api/auth/guest',json={})
    assert client.get('/api/me').json()['education_stage']=='all'
    assert client.get('/api/games').json()['items']==[]
    assert client.get('/api/games/'+ids[0]).status_code==404

def test_imported_poem_exposes_original_source(client):
    imported=client.get('/api/works',params={'mode':'text','q':'草长莺飞二月天'}).json()['items'][0]
    poem=client.get('/api/poems/'+imported['poem_id']).json()
    assert poem['source_label']=='古诗词网 · 原文'
    assert poem['source_url'].startswith('https://www.gushici.net/shici/')
    assert poem['verification']=='unreviewed'
