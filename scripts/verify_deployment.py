"""Exercise a deployed HTTPS site with fresh, isolated anonymous game sessions."""
import argparse,json,uuid
from pathlib import Path
import httpx

parser=argparse.ArgumentParser()
parser.add_argument('--url',default='https://feihualing.ltkeep.cn')
parser.add_argument('--report',default='docs/lt-work-deployment-checks.json')
args=parser.parse_args()
checks=[]
def check(name,condition):
    if not condition:raise AssertionError(name)
    checks.append(name)
def key():return uuid.uuid4().hex
with httpx.Client(base_url=args.url,timeout=25) as client:
    def get(path,**kwargs):
        r=client.get(path,**kwargs);r.raise_for_status();return r.json()
    def post(path,**kwargs):
        r=client.post(path,**kwargs);r.raise_for_status();return r.json()
    def create(level='easy'):
        return post('/api/games',json={'word':'月','difficulty':level,'request_id':key()})['game']
    def action(g,kind,**fields):
        return post('/api/games/'+g['id']+'/'+kind,json={'request_id':key(),**fields})
    home=client.get('/');home.raise_for_status()
    check('HTTPS home',args.url.startswith('https://') and '飞花令' in home.text)
    check('secure visitor cookie','Secure' in home.headers.get('set-cookie',''))
    health=get('/health')
    check('poetry ready; recognition disabled',health['status']=='ok' and health['speech']['status']=='disabled')
    cfg=get('/api/config')
    rows=get('/api/search',params={'word':'月','limit':50})['items']
    check('search and provenance',len(rows)==50 and all('月' in r['text'] and r['title'] for r in rows))
    traditional=get('/api/search',params={'word':'風'})
    check('traditional search',traditional['word']=='风')
    poem=get('/api/poems/'+rows[0]['poem_id'])
    check('full poem',bool(poem['original']) and bool(poem['paragraphs']))
    check('no recorder asset','recorder.wav.min.js' not in home.text and 'live.js?v=1.3.0' in home.text)
    pairs=get('/api/search',params={'word':'月','unit':'pair','limit':20})
    check('pair search',pairs['items'][0]['clauses']==['床前明月光','疑是地上霜'])
    more=get('/api/search',params={'word':'月','unit':'pair','limit':20,'offset':20})
    check('pair pagination',set(p['id'] for p in pairs['items']).isdisjoint(p['id'] for p in more['items']))
    check('pair corpus indexed',cfg['pairing']['pairs']>1000000)
    for text,expected in [('明月几时有，把酒问青天','accepted'),('明月几时有，千里共婵娟','invalid')]:
        g=create();reply=action(g,'answers',text=text)
        check('adjacent-only '+text,reply['verdict']['kind']==expected)
        action(g,'end')
    for level,rounds in [('easy',5),('normal',10),('hard',15)]:
        g=create(level)
        check(level+' rules',g['level']['rounds']==rounds and g['level']['seconds'] is None and g['deadline'] is None)
        for turn in range(rounds):
            line=next(r for r in rows if r['id'] not in g['used'])
            body={'request_id':key(),'text':line['text']}
            path='/api/games/'+g['id']+'/answers'
            reply=post(path,json=body);g=reply['game']
            check(level+f' round {turn+1}',reply['verdict']['kind']=='accepted' and g['round']==turn+1)
            if turn==0:
                check(level+' bot couplet',len(g['messages'][-1]['line_ids'])==2 and all(lid in g['used'] for lid in g['messages'][-1]['line_ids']))
                check(level+' idempotency',post(path,json=body)['verdict']==reply['verdict'])
                check(level+' restore',get('/api/games/'+g['id'])['game']['round']==1)
        check(level+' victory',g['status']=='won')
    check('work search ready',cfg['work_search'])
    works=get('/api/works',params={'mode':'author','q':'李白','limit':2})
    check('author works',works['total']>100 and all('李白' in x['author'] for x in works['items']))
    more=get('/api/works',params={'mode':'author','q':'李白','offset':2,'limit':2})
    check('works pagination',set(x['id'] for x in works['items']).isdisjoint(x['id'] for x in more['items']))
    phrase=get('/api/works',params={'mode':'text','q':'明月光，疑是地上霜'})
    check('phrase across punctuation',phrase['total']>=1 and phrase['items'][0]['title']=='静夜思')
    check('two character search',get('/api/works',params={'mode':'text','q':'明月'})['total']>100)
    g=create('endless');seen=set()
    check('endless rules',all(g['level'][x] is None for x in ('rounds','seconds','hints')))
    for turn in range(28):
        line=next(r for r in rows if r['id'] not in seen)
        response=action(g,'answers',text=line['text']);g=response['game']
        for m in g['messages']:seen.update(m['line_ids'])
        check('endless '+str(turn+1),g['round']==turn+1 and g['status']=='playing')
        if len(seen)>35:rows=get('/api/search',params={'word':'月','limit':50,'offset':turn*3})['items']
    check('latest history bounded',len(g['messages'])==50 and g['message_count']==56)
    history=get('/api/games/'+g['id']+'/messages',params={'before':7})
    check('history paging',[m['seq'] for m in history['items']]==list(range(1,7)))
    recent=get('/api/games/'+g['id'],params={'after':54})['game']
    check('incremental messages',[m['seq'] for m in recent['messages']]==[55,56])
    for i in range(3):check('unlimited hint '+str(i),action(g,'hint')['game']['hints'] is None)
    check('endless leave',action(g,'end')['game']['status']=='abandoned')
    g=create()
    check('candidate correction',action(g,'answers',text='床前明月亮')['verdict']['kind']=='candidates')
    candidate=get('/api/games/'+g['id'])['game']['candidates'][0]
    reply=action(g,'confirm',candidate_id=candidate['candidate_id'])
    check('confirm correction',reply['game']['round']==1)
    check('duplicate rejection',action(g,'answers',text='床前明月光')['verdict']['kind']=='duplicate')
    check('missing word rejection',action(g,'answers',text='春眠不觉晓')['verdict']['kind']=='missing_word')
    check('fabricated line rejection',action(g,'answers',text='我今天想吃麻辣火锅')['verdict']['kind']=='invalid')
    for _ in range(2):check('hint',action(g,'hint')['verdict']['kind']=='hint')
    check('hint limit',client.post('/api/games/'+g['id']+'/hint',json={'request_id':key()}).status_code==400)
    action(g,'end')
    g=create()
    check('old audio retired',client.post('/api/games/'+g['id']+'/audio',content=b'').status_code==410)
    check('audio did not advance',get('/api/games/'+g['id'])['game']['round']==0)
    action(g,'end')
report={'url':args.url,'checks':checks,'count':len(checks),'version':cfg['version'],'corpus':cfg['corpus']}
Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in report.items() if k not in ('corpus','checks')},ensure_ascii=False,indent=2))
