"""Bound request bodies before multipart parsing, including chunked uploads."""
from starlette.responses import JSONResponse

class BodyLimit:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope['method'] not in ('POST','PATCH'):
            return await self.app(scope,receive,send)
        if scope['path'].startswith('/api/games/') and scope['path'].endswith('/audio'):
            return await JSONResponse({'detail':'录音作答已停用，请刷新页面后使用文字作答'},status_code=410)(scope,receive,send)
        limit=16384
        chunks=[];size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect':return
            body=message.get('body',b'');size+=len(body)
            if size>limit:
                return await JSONResponse({'detail':'提交内容过大，请缩短后重试'},status_code=413)(scope,receive,send)
            chunks.append(body)
            if not message.get('more_body'):break
        delivered=False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {'type':'http.request','body':b''.join(chunks),'more_body':False}
            return await receive()
        await self.app(scope,replay,send)
