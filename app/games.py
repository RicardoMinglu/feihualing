import hashlib,json,sqlite3,time,secrets,base64,binascii,math
from contextlib import contextmanager
from .config import GAMES,LEVELS
from .education import checked_stage
from scripts.migrate_business import migrate

class GameError(Exception):
    def __init__(self,message,status=400):self.message=message;self.status=status

class Games:
    PAGE=50
    def __init__(self,corpus,path=GAMES):
        self.event_recorder=None;self.corpus=corpus;self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        with self.tx() as db:db.executescript('''
        CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY,owner TEXT,state TEXT);
        CREATE TABLE IF NOT EXISTS requests(game_id TEXT,key TEXT,fingerprint TEXT,response TEXT,PRIMARY KEY(game_id,key));
        CREATE TABLE IF NOT EXISTS messages(game_id TEXT,seq INTEGER,body TEXT,PRIMARY KEY(game_id,seq));
        CREATE TABLE IF NOT EXISTS used_lines(game_id TEXT,line_id INTEGER,PRIMARY KEY(game_id,line_id));
        ''')
        with self.tx() as db:migrate(db)
    @contextmanager
    def tx(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:db.execute('PRAGMA journal_mode=WAL');db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except:db.rollback();raise
        finally:db.close()
    def save(self,db,g):
        if 'messages' in g:
            for m in g['messages']:
                db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?)',(g['id'],m['seq'],json.dumps(m,ensure_ascii=False)))
        if 'used' in g:
            db.executemany('INSERT OR IGNORE INTO used_lines VALUES(?,?)',[(g['id'],lid) for lid in set(g['used'])-g.get('_saved_used',set())])
        state={k:v for k,v in g.items() if k not in ('messages','used') and not k.startswith('_')}
        state['storage_version']=2
        db.execute('UPDATE games SET state=? WHERE id=?',(json.dumps(state,ensure_ascii=False),g['id']))
        player_count=g.get('player_answer_count')
        if player_count is None:
            player_count=db.execute("SELECT count(*) FROM messages WHERE game_id=? AND json_extract(body,'$.who')='player'",(g['id'],)).fetchone()[0]
        db.execute('''INSERT INTO game_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(game_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,
            ended_at=excluded.ended_at,round=excluded.round,
            player_answer_count=excluded.player_answer_count,message_count=excluded.message_count''',(
            g['id'],g.get('_owner') or db.execute('SELECT owner FROM games WHERE id=?',(g['id'],)).fetchone()[0],
            g['word'],g['difficulty'],g['status'],g.get('reason',''),g['created_at'],
            g.get('ended_at'),g['round'],player_count,g.get('message_count',0)))
    def load(self,db,gid,owner):
        row=db.execute('SELECT state FROM games WHERE id=? AND owner=?',(gid,owner)).fetchone()
        if not row:raise GameError('对局不存在或已失效',404)
        g=json.loads(row[0]);g['deadline']=None;g.pop('remaining',None)
        if g.get('storage_version')!=2:
            for i,m in enumerate(g.get('messages',[]),1):m['seq']=i
            g['message_count']=len(g.get('messages',[]));self.save(db,g)
        g['messages']=[json.loads(r[0]) for r in db.execute('SELECT body FROM messages WHERE game_id=? ORDER BY seq DESC LIMIT ?',(gid,self.PAGE))][::-1]
        if 'player_answer_count' not in g:
            g['player_answer_count']=db.execute("SELECT count(*) FROM messages WHERE game_id=? AND json_extract(body,'$.who')='player'",(gid,)).fetchone()[0]
        g['used']=[r[0] for r in db.execute('SELECT line_id FROM used_lines WHERE game_id=?',(gid,))]
        g['_saved_used']=set(g['used'])
        if g['status']=='processing' and time.time()>=g['processing_until']:
            pending=g['pending'];g.update(status='playing',pending=None);g['revision']=g.get('revision',0)+1
            body={'verdict':{'kind':'error','message':'处理超时，请重新作答'}}
            db.execute('UPDATE requests SET response=? WHERE game_id=? AND key=?',(json.dumps(body),gid,pending))
            self.record_event(db,owner,'game_answer','error',gid+':'+pending,now=g['processing_until']-15)
        self.save(db,g);return g
    def public(self,g):
        result={k:v for k,v in g.items() if k not in ('pending','remaining','hinted_poems','storage_version','processing_until') and not k.startswith('_')}
        result['messages']=g.get('messages',[])[-self.PAGE:]
        result['has_older']=bool(result['messages'] and result['messages'][0]['seq']>1)
        result['used_count']=len(g.get('used',[]))
        if g['difficulty']=='endless':result.pop('used',None)
        return result|{'server_time':time.time(),'level':LEVELS[g['difficulty']]}
    def profile(self,owner):
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT education_stage FROM visitor_profiles WHERE owner=?',(owner,)).fetchone()
            return {'education_stage':row[0] if row else 'all'}
    def set_profile(self,owner,stage):
        checked_stage(stage)
        now=time.time()
        with self.tx() as db:
            db.execute('''INSERT INTO visitor_profiles VALUES(?,?,?,?)
                ON CONFLICT(owner) DO UPDATE SET education_stage=excluded.education_stage,
                updated_at=excluded.updated_at''',(owner,stage,now,now))
        return {'education_stage':stage}
    def list_games(self,owner,limit=20,cursor=None):
        if not 1<=limit<=50:raise GameError('分页参数无效')
        if cursor and len(cursor)>256:raise GameError('分页游标无效')
        where='WHERE owner=?';params=[owner]
        if cursor:
            try:
                raw=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                created,gid=raw
                if not isinstance(created,(int,float)) or not math.isfinite(created) or not isinstance(gid,str) or len(gid)!=32:
                    raise ValueError()
            except (ValueError,TypeError,OverflowError,binascii.Error):raise GameError('分页游标无效')
            where+=' AND (created_at,game_id)<(?,?)';params.extend([created,gid])
        with sqlite3.connect(self.path) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute('SELECT * FROM game_summaries '+where+
                ' ORDER BY created_at DESC,game_id DESC LIMIT ?',params+[limit+1])]
        more=len(rows)>limit;items=rows[:limit]
        for item in items:item['id']=item.pop('game_id');item.pop('owner')
        next_cursor=None
        if more:
            last=items[-1]
            next_cursor=base64.urlsafe_b64encode(json.dumps([last['created_at'],last['id']],separators=(',',':')).encode()).decode().rstrip('=')
        return {'items':items,'has_more':more,'next_cursor':next_cursor}
    def record_event(self,db,owner,function,outcome,key,now=None):
        if self.event_recorder:self.event_recorder(db,owner,function,outcome,request_id=key,now=now)
    def response(self,db,g,key,body):
        g['revision']=g.get('revision',0)+1
        self.save(db,g)
        # Store only the operation outcome: history belongs in messages, once.
        db.execute('UPDATE requests SET response=? WHERE game_id=? AND key=?',(json.dumps(body,ensure_ascii=False),g['id'],key))
        owner=db.execute('SELECT owner FROM games WHERE id=?',(g['id'],)).fetchone()[0]
        fingerprint=db.execute('SELECT fingerprint FROM requests WHERE game_id=? AND key=?',(g['id'],key)).fetchone()[0]
        function='game_hint' if fingerprint.startswith('hint:') else ('game_end' if fingerprint.startswith('end:') else 'game_answer')
        outcome=body.get('verdict',{}).get('kind','success')
        self.record_event(db,owner,function,outcome,g['id']+':'+key)
        if g['status'] in ('won','lost'):
            self.record_event(db,owner,'game_end',g['status'],g['id']+':completed')
        return {'game':self.public(g),**body}
    def replay(self,g,response):
        body=json.loads(response);body.pop('game',None)
        return {'game':self.public(g),**body}
    def cached(self,db,g,key,fingerprint):
        row=db.execute('SELECT * FROM requests WHERE game_id=? AND key=?',(g['id'],key)).fetchone()
        if row:
            if row['fingerprint']!=fingerprint:raise GameError('请求编号已被其他操作使用',409)
            if row['response']:return self.replay(g,row['response'])
            return {'game':self.public(g),'verdict':{'kind':'processing','message':'正在处理，请稍候'}}
    def create(self,owner,char,difficulty,key):
        if difficulty not in LEVELS:raise GameError('难度无效')
        gid=hashlib.sha256((owner+key).encode()).hexdigest()[:32]
        with self.tx() as db:
            row=db.execute('SELECT state FROM games WHERE id=? AND owner=?',(gid,owner)).fetchone()
            if row:
                existing=json.loads(row[0])
                if existing['word']!=char or existing['difficulty']!=difficulty:raise GameError('请求编号已使用',409)
                return self.public(self.load(db,gid,owner))
            level=LEVELS[difficulty]
            if len(self.corpus.pool(char))<(3*level['rounds'] if level['rounds'] else 1):raise GameError('这个字的可用诗句不足，请换字或降低难度')
            g={'id':gid,'word':char,'difficulty':difficulty,'round':0,'hints':level['hints'],'used':[],'messages':[],'message_count':0,'player_answer_count':0,'status':'playing','reason':'','deadline':None,'candidates':[],'pending':None,'created_at':time.time(),'ended_at':None}
            db.execute('INSERT INTO games VALUES(?,?,?)',(gid,owner,'{}'));self.save(db,g)
            self.record_event(db,owner,'game_create','success',gid+':'+key)
            return self.public(g)
    def get(self,gid,owner,after=None):
        with self.tx() as db:
            g=self.load(db,gid,owner);result=self.public(g)
            summary=db.execute('SELECT player_answer_count,ended_at FROM game_summaries WHERE game_id=?',(gid,)).fetchone()
            if summary:result['player_answer_count']=summary[0];result['ended_at']=summary[1]
            if after is not None:
                result['messages']=[json.loads(r[0]) for r in db.execute('SELECT body FROM messages WHERE game_id=? AND seq>? ORDER BY seq LIMIT ?',(gid,after,self.PAGE))]
                result['incremental']=True
                result['has_newer']=bool(result['messages'] and result['messages'][-1]['seq']<g['message_count'])
            return result
    def history(self,gid,owner,before=None,limit=50):
        with self.tx() as db:
            g=self.load(db,gid,owner)
            rows=db.execute('SELECT body FROM messages WHERE game_id=? AND seq<? ORDER BY seq DESC LIMIT ?',(gid,before if before is not None else g['message_count']+1,limit))
            items=[json.loads(r[0]) for r in rows][::-1]
            return {'items':items,'total':g['message_count'],'has_older':bool(items and items[0]['seq']>1)}
    def reserve(self,gid,owner,key,fingerprint):
        with self.tx() as db:
            g=self.load(db,gid,owner);cached=self.cached(db,g,key,fingerprint)
            if cached:return cached
            if g['status']!='playing':raise GameError('当前无法提交答案',409)
            g.update(status='processing',processing_until=time.time()+15,pending=key,candidates=[]);g['revision']=g.get('revision',0)+1
            db.execute('INSERT INTO requests VALUES(?,?,?,NULL)',(gid,key,fingerprint));self.save(db,g)
    def finish(self,gid,owner,key,verdict,transcript=None):
        with self.tx() as db:
            g=self.load(db,gid,owner)
            if g.get('pending')!=key:
                r=db.execute('SELECT response FROM requests WHERE game_id=? AND key=?',(gid,key)).fetchone()
                return self.replay(g,r[0]) if r and r[0] else {'game':self.public(g),'verdict':{'kind':'error','message':'本次提交已结束'}}
            g.update(status='playing',pending=None)
            if verdict['kind']=='accepted':verdict=self.accept(g,verdict['option'],verdict.get('corrected',False))
            elif verdict['kind']=='candidates':
                g['candidates']=[dict(o,candidate_id=secrets.token_hex(8)) for o in verdict.pop('options')]
                verdict['candidates']=g['candidates']
            return self.response(db,g,key,{'verdict':verdict})
    def append(self,g,option,who):
        g['message_count']+=1
        if who=='player':g['player_answer_count']=g.get('player_answer_count',0)+1
        g['messages'].append(dict(option,who=who,round=g['round']+1,seq=g['message_count']))
        g['used'].extend(option['line_ids'])
    def accept(self,g,option,corrected=False):
        if hasattr(self.corpus,'valid_option') and not self.corpus.valid_option(option):
            return {'kind':'invalid','message':'这句诗当前无法确认，请重新作答'}
        used=self.corpus.equivalent_ids(g['used']) if hasattr(self.corpus,'equivalent_ids') else set(g['used'])
        proposed=self.corpus.equivalent_ids(option['line_ids']) if hasattr(self.corpus,'equivalent_ids') else set(option['line_ids'])
        if proposed & used:return {'kind':'duplicate','message':'这句诗已经用过，请换一句'}
        self.append(g,option,'player');g['candidates']=[]
        used=self.corpus.equivalent_ids(g['used']) if hasattr(self.corpus,'equivalent_ids') else set(g['used'])
        available=self.corpus.computer_options(g['word'],used)
        if available:self.append(g,secrets.choice(available),'computer')
        g['round']+=1;target=LEVELS[g['difficulty']]['rounds']
        if not available or (target is not None and g['round']>=target):g.update(status='won',reason='诗友已无可接的诗句' if not available else '你已完成全部回合',ended_at=time.time())
        return {'kind':'accepted','corrected':corrected,'text':option['text'],'message':'已纠正为：'+option['text'] if corrected else '接令成功'}
    def action(self,gid,owner,key,action,value=''):
        with self.tx() as db:
            g=self.load(db,gid,owner);cached=self.cached(db,g,key,action+':'+value)
            if cached:return cached
            if g['status']!='playing':raise GameError('对局已结束或正在处理',409)
            db.execute('INSERT INTO requests VALUES(?,?,?,NULL)',(gid,key,action+':'+value))
            if action=='confirm':
                option=next((o for o in g['candidates'] if o['candidate_id']==value),None)
                if not option:raise GameError('候选已失效，请重新提交',409)
                verdict=self.accept(g,option,True)
            elif action=='hint':
                if g['hints']==0:raise GameError('本局没有剩余提示')
                rows=self.corpus.available(g['word'],set(g['used']))
                if not rows:raise GameError('没有可用提示')
                seen=set(g.get('hinted_poems',[]));hint=None
                for row in rows:
                    proposed=self.corpus.detail(row['id'])
                    if proposed['poem_id'] not in seen:hint=proposed;break
                if not hint:
                    if g['hints'] is not None:raise GameError('暂时没有新的提示')
                    g['hinted_poems']=[];hint=self.corpus.detail(rows[0]['id'])
                g.setdefault('hinted_poems',[]).append(hint['poem_id'])
                if g['hints'] is not None:g['hints']-=1
                verdict={'kind':'hint','author':hint['author'],'title':hint['title'],'dynasty':hint['dynasty']}
            elif action=='end':g.update(status='abandoned',reason='已结束本局',ended_at=time.time());verdict={'kind':'ended'}
            else:raise GameError('操作无效')
            return self.response(db,g,key,{'verdict':verdict})
