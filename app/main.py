import asyncio,logging,os,re,secrets
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,ConfigDict
from .config import ROOT,CORPUS,LEVELS
from .corpus import Corpus
from .games import Games,GameError
from .text import word
from .limits import BodyLimit
from .works import Works
from .education import checked_stage

from contextlib import asynccontextmanager
from typing import Literal
from .accounts import Accounts,AuthError
from .community import Community,CommunityError,record_activity
from .security import check_origin,limited,username_limit,password_slot
from .admin import build_router

corpus=Corpus();games=Games(corpus);works=Works(corpus)
accounts=Accounts(games.path);community=Community(games.path)
games.event_recorder=record_activity
SPEECH_DISABLED={'status':'disabled'}
COOKIE='feihua_session'

@asynccontextmanager
async def lifespan(app):
    stop=asyncio.Event()
    worker=asyncio.create_task(community.run_notifications(stop))
    try:yield
    finally:
        stop.set()
        await worker

app=FastAPI(title='飞花令',version='1.5.0',lifespan=lifespan)

@app.middleware('http')
async def identity(request,call_next):
    request.state.user=await asyncio.to_thread(accounts.resolve,request.cookies.get(COOKIE,''))
    request.state.owner=request.state.user['id'] if request.state.user else None
    path=request.url.path
    try:
        if path.startswith('/api/'):
            if request.method in ('POST','PATCH','PUT','DELETE'):check_origin(request)
            public=path in ('/api/config','/api/auth/guest','/api/auth/login','/api/auth/logout')
            if not public and not request.state.user:raise HTTPException(401,'请刷新页面或登录以恢复身份')
            if request.state.user and request.state.user['must_change_password'] and path not in (
                '/api/me','/api/auth/guest','/api/auth/credentials','/api/auth/logout','/api/config'):
                raise HTTPException(403,'请先修改临时密码')
        response=await call_next(request)
    except HTTPException as e:
        response=JSONResponse({'detail':e.detail},status_code=e.status_code,headers=e.headers)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='DENY'
    if path.startswith('/api/') or path.startswith('/admin'):response.headers['Cache-Control']='no-store'
    return response

@app.exception_handler(GameError)
@app.exception_handler(AuthError)
@app.exception_handler(CommunityError)
async def business_error(request,e):
    if isinstance(e,GameError) and request.state.owner and e.status not in (401,403,404):
        # Failed attempts have no committed game transaction. Replays still share
        # the same receipt as successful operations, without storing answer text.
        try:
            payload=await request.json();key=payload.get('request_id')
            match=re.fullmatch(r'/api/games(?:/([^/]+)/(answers|hint|confirm|end))?',request.url.path)
            if match and isinstance(key,str) and 8<=len(key)<=80:
                gid,action=match.groups()
                function={'answers':'game_answer','confirm':'game_answer','hint':'game_hint','end':'game_end'}.get(action,'game_create')
                if gid:receipt=gid+':'+key
                else:
                    import hashlib
                    receipt=hashlib.sha256((request.state.owner+key).encode()).hexdigest()[:32]+':'+key
                await asyncio.to_thread(community.record,request.state.owner,function,'rejected',receipt)
        except Exception:pass
    return JSONResponse({'detail':e.message},status_code=e.status)

@app.exception_handler(HTTPException)
async def http_error(request,e):
    if request.state.owner and e.status_code in (400,404,503):
        function='search' if request.url.path in ('/api/search','/api/works') else ('poem_view' if request.url.path.startswith('/api/poems/') else None)
        if function:
            try:await asyncio.to_thread(active_read,request,function,'rejected')
            except Exception:pass
    return JSONResponse({'detail':e.detail},status_code=e.status_code,headers=e.headers)


def user_profile(user):return user|games.profile(user['id'])

def session_response(user,token=None):
    response=JSONResponse(user_profile(user))
    if token:
        response.set_cookie(COOKIE,token,max_age=(180 if user['kind']=='guest' else 30)*86400,
                            httponly=True,samesite='lax',secure=os.getenv('FEIHUA_SECURE_COOKIE','0')=='1')
    response.delete_cookie('feihua_visitor')
    return response

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')
class Register(Input):
    username:str=Field(min_length=2,max_length=32)
    password:str=Field(min_length=8,max_length=128)
class Login(Register):
    # Authenticate existing credentials independently of new-password policy.
    password:str=Field(min_length=1,max_length=128)
    confirm_switch:bool=False
class Credentials(Input):
    current_password:str=Field(min_length=1,max_length=128)
    username:str|None=Field(default=None,min_length=2,max_length=32)
    password:str|None=Field(default=None,min_length=8,max_length=128)
class Event(Input):
    request_id:str=Field(min_length=8,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')
class BrowserEvent(Event):
    action:Literal['read_aloud','support_view','support_save']
class Feedback(Event):
    kind:Literal['bug','suggestion','content','other']
    body:str=Field(min_length=5,max_length=3000)
    contact:str=Field(default='',max_length=200)

@app.post('/api/auth/guest')
def guest(request:Request):
    limited(request,'bootstrap',60)
    user,token=accounts.bootstrap(request.cookies.get(COOKIE),request.cookies.get('feihua_visitor'))
    return session_response(user,token)

@app.post('/api/auth/register')
def register(body:Register,request:Request):
    limited(request,'credentials',10,900)
    with password_slot():user,token=accounts.upgrade(request.state.owner,body.username,body.password)
    return session_response(user,token)

@app.post('/api/auth/login')
def login(body:Login,request:Request):
    limited(request,'login',20,900);username_limit(body.username)
    previous=request.state.user
    if previous and previous['kind']=='guest' and previous['has_records'] and not body.confirm_switch:
        raise HTTPException(409,'当前游客已有记录，登录后不会自动合并。可先设置游客账号保留记录，再确认切换。')
    with password_slot():user,token=accounts.login(body.username,body.password)
    accounts.logout(request.cookies.get(COOKIE,''))
    return session_response(user,token)

@app.post('/api/auth/credentials')
def credentials(body:Credentials,request:Request):
    limited(request,'credentials',10,900)
    with password_slot():
        user,token=accounts.change_credentials(request.state.owner,body.current_password,username=body.username,password=body.password)
    return session_response(user,token)

@app.post('/api/auth/logout')
def logout(request:Request):
    accounts.logout(request.cookies.get(COOKIE,''))
    response=JSONResponse({'status':'signed_out'})
    response.delete_cookie(COOKIE);response.delete_cookie('feihua_visitor')
    return response

@app.post('/api/visits')
def visit(body:Event,request:Request):
    limited(request,'visit-ip',120);limited(request,'visit',20,user=True)
    added=community.record(request.state.owner,'visit',request_id=body.request_id)
    return {'status':'received' if added else 'duplicate'}

@app.post('/api/events')
def event(body:BrowserEvent,request:Request):
    limited(request,'event-ip',240)
    return community.browser_event(request.state.user,body.action,body.request_id)

@app.post('/api/feedback')
def feedback(body:Feedback,request:Request):
    limited(request,'feedback-ip',20,600)
    return community.submit_feedback(request.state.user,body.kind,body.body,body.contact,body.request_id)


def active_read(request,function,outcome='success'):
    key=request.headers.get('x-activity-id')
    if key:
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}',key):raise HTTPException(400,'请求编号无效')
        limited(request,'active-read',120,user=True)
        community.record(request.state.owner,function,outcome,request_id=key)


def require_admin(request:Request):
    if not request.state.user or request.state.user['role']!='admin':raise HTTPException(403,'仅管理员可访问')
    if request.state.user['must_change_password']:raise HTTPException(403,'请先修改临时密码')
    return request.state.user

app.include_router(build_router(lambda:accounts,lambda:community,require_admin))

class Create(BaseModel):
    word:str=Field(min_length=1,max_length=8)
    difficulty:str
    request_id:str=Field(min_length=8,max_length=80)
class Answer(BaseModel):
    text:str=Field(min_length=1,max_length=200)
    request_id:str=Field(min_length=8,max_length=80)
class Action(BaseModel):
    request_id:str=Field(min_length=8,max_length=80)
    candidate_id:str=''
class ProfileUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    education_stage:str
def checked_word(value):
    try:return word(value)
    except ValueError as e:raise HTTPException(400,str(e))

@app.get('/health')
def health():return {'status':'ok' if CORPUS.exists() else 'missing_corpus','speech':SPEECH_DISABLED}
@app.get('/api/config')
def config():return {'speech':SPEECH_DISABLED,'corpus':corpus.stats(),'pairing':corpus.pairing_stats(),'version':'1.5.0','levels':LEVELS,'work_search':works.ready()}
@app.get('/api/me')
def profile(request:Request):return user_profile(request.state.user)
@app.patch('/api/me')
def update_profile(body:ProfileUpdate,request:Request):
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
        raise HTTPException(403,'请求来源无效')
    if request.headers.get('sec-fetch-site') not in (None,'same-origin','none'):
        raise HTTPException(403,'请求来源无效')
    try:return games.set_profile(request.state.owner,body.education_stage)
    except ValueError as e:raise HTTPException(400,str(e))
def selected_stage(request,stage):
    value=stage if stage is not None else games.profile(request.state.owner)['education_stage']
    try:return checked_stage(value)
    except ValueError as e:raise HTTPException(400,str(e))
@app.get('/api/search')
def search(request:Request,word:str,offset:int=0,limit:int=20,unit:str='single',stage:str|None=None):
    if not 0<=offset<=1000000 or not 1<=limit<=50:raise HTTPException(400,'分页参数无效')
    if unit not in ('single','pair'):raise HTTPException(400,'展示方式无效')
    if unit=='pair' and not corpus.pairing_stats():raise HTTPException(503,'上下句索引尚未就绪')
    result=corpus.search(checked_word(word),offset,limit,unit,selected_stage(request,stage))
    active_read(request,'search');return result
@app.get('/api/works')
def work_search(request:Request,mode:str,q:str,offset:int=0,limit:int=20,stage:str|None=None):
    if not 0<=offset<=1000000 or not 1<=limit<=50:raise HTTPException(400,'分页参数无效')
    if not works.ready():raise HTTPException(503,'搜索索引尚未就绪')
    try:
        result=works.search(mode,q,offset,limit,selected_stage(request,stage))
        active_read(request,'search');return result
    except ValueError as e:raise HTTPException(400,str(e))

@app.get('/api/poems/{pid}')
def poem(pid:str,request:Request):
    p=corpus.poem(pid)
    if not p:raise HTTPException(404,'未找到这首作品')
    active_read(request,'poem_view');return p
@app.post('/api/games')
def create(body:Create,request:Request):
    limited(request,'game-create',30,user=True)
    return {'game':games.create(request.state.owner,checked_word(body.word),body.difficulty,body.request_id)}
@app.get('/api/games')
def list_games(request:Request,limit:int=20,cursor:str|None=None):return games.list_games(request.state.owner,limit,cursor)
@app.get('/api/games/{gid}')
def get_game(gid:str,request:Request,after:int|None=None):
    if after is not None and after<0:raise HTTPException(400,'消息位置无效')
    return {'game':games.get(gid,request.state.owner,after)}

@app.get('/api/games/{gid}/messages')
def history(gid:str,request:Request,before:int|None=None,limit:int=50):
    if (before is not None and before<1) or not 1<=limit<=100:raise HTTPException(400,'分页参数无效')
    return games.history(gid,request.state.owner,before,limit)


async def calculate(gid,owner,key,fn):
    try:return await asyncio.wait_for(asyncio.to_thread(fn),timeout=14.5)
    except asyncio.TimeoutError:return {'kind':'error','message':'处理超时，请重新作答'}
    except ValueError as e:return {'kind':'error','message':str(e)}
    except Exception:
        logging.exception('Answer processing failed')
        return {'kind':'error','message':'暂时无法处理，请重试'}

@app.post('/api/games/{gid}/answers')
async def answer(gid:str,body:Answer,request:Request):
    limited(request,'game-action',120,user=True)
    owner=request.state.owner
    cached=games.reserve(gid,owner,body.request_id,'text:'+body.text)
    if cached:return cached
    g=games.get(gid,owner)
    verdict=await calculate(gid,owner,body.request_id,lambda:corpus.judge(body.text,g['word']))
    return games.finish(gid,owner,body.request_id,verdict)

@app.post('/api/games/{gid}/audio')
def audio_retired(gid:str):
    raise HTTPException(410,'录音作答已停用，请刷新页面后使用文字作答')

@app.post('/api/games/{gid}/{action}')
def action(gid:str,action:str,body:Action,request:Request):
    limited(request,'game-action',120,user=True)
    if action not in ('hint','confirm','end'):raise HTTPException(404,'操作不存在')
    return games.action(gid,request.state.owner,body.request_id,action,body.candidate_id)
@app.get('/admin')
@app.get('/admin/')
def admin_page():return FileResponse(ROOT/'static/admin.html')

@app.get('/')
def index():return FileResponse(ROOT/'static/index.html')
@app.get('/favicon.ico',include_in_schema=False)
def favicon():
    from fastapi.responses import Response
    return Response(status_code=204)
app.add_middleware(BodyLimit)
app.mount('/assets',StaticFiles(directory=ROOT/'static/assets'),name='assets')
