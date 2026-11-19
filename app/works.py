import json,re
from functools import lru_cache
from .text import normalize,simplify,HAN
from . import education

class Works:
    def __init__(self,corpus):self.corpus=corpus
    @lru_cache(maxsize=1)
    def ready(self):
        with self.corpus.connect() as db:return bool(db.execute("SELECT 1 FROM meta WHERE key='search'").fetchone())
    @lru_cache(maxsize=32)
    def count(self,mode,q):
        source,params=self.selection(mode,q)
        with self.corpus.connect() as db:
            if education.ready(db):source+=' AND NOT EXISTS(SELECT 1 FROM education_inactive_works x WHERE x.poem_id=w.poem_id)'
            return db.execute('SELECT count(*) '+source,params).fetchone()[0]
    def selection(self,mode,q):
        if mode=='author':return 'FROM work_search w WHERE instr(w.author,?)>0',[q]
        if len(q)>=3:
            # Trigram LIKE has no MATCH query syntax and supports phrases longer than 3 characters.
            return 'FROM work_fts f JOIN work_search w ON w.id=f.rowid WHERE f.body LIKE ?',['%'+q+'%']
        return '''FROM work_search w WHERE w.id IN
            (SELECT work_id FROM work_chars WHERE char=? INTERSECT SELECT work_id FROM work_chars WHERE char=?)
            AND instr(w.body,?)>0''',[q[0],q[1],q]
    def search(self,mode,raw,offset,limit,stage='all'):
        if mode not in ('author','text'):raise ValueError('搜索方式无效')
        if len(raw)>200 or re.search(r'[A-Za-z0-9]',raw):raise ValueError('请输入作者姓名或中文诗句')
        q=normalize(raw)
        if not q or len(q)>80:raise ValueError('请输入 1 至 80 个汉字')
        if mode=='text' and len(q)<2:raise ValueError('诗句搜索至少输入两个汉字，单字请使用令字模式')
        with self.corpus.connect() as db:
            source,params=self.selection(mode,q)
            if education.ready(db):
                source+=' AND NOT EXISTS(SELECT 1 FROM education_inactive_works x WHERE x.poem_id=w.poem_id)'
                if mode=='author':
                    stage_hit='EXISTS(SELECT 1 FROM education_work_stage es WHERE es.poem_id=w.poem_id AND es.stage=?)'
                else:
                    stage_hit='''EXISTS(SELECT 1 FROM education_stage_spans es WHERE es.poem_id=w.poem_id
                        AND es.stage=? AND instr(substr(w.body,es.start_char+1,es.end_char-es.start_char),?)>0)'''
                stage_order="CASE WHEN ?='all' THEN 1 WHEN "+stage_hit+' THEN 0 ELSE 1 END'
                verified_order='CASE WHEN EXISTS(SELECT 1 FROM education_verified ev WHERE ev.poem_id=w.poem_id) THEN 0 ELSE 1 END'
                if mode=='author':order=' ORDER BY CASE WHEN w.author=? THEN 0 ELSE 1 END,'+stage_order+','+verified_order+',w.poem_id';params=params+[q,stage,stage]
                else:order=' ORDER BY '+stage_order+','+verified_order+',w.poem_id';params=params+[stage,stage,q]
            else:
                order=' ORDER BY CASE WHEN w.author=? THEN 0 ELSE 1 END,w.poem_id' if mode=='author' else ' ORDER BY w.poem_id'
                if mode=='author':params=params+[q]
            total=self.count(mode,q)
            ids=[r[0] for r in db.execute('SELECT w.poem_id '+source+order+' LIMIT ? OFFSET ?',params+[limit,offset])]
        items=[]
        for pid in ids:
            p=self.corpus.poem(pid);body=''.join(p['paragraphs']);snippet=self.snippet(body,q if mode=='text' else '')
            with self.corpus.connect() as db:
                meta=education.poem_metadata(db,pid,stage)
                if mode=='text' and education.ready(db) and stage!='all':
                    meta['stage_match']=bool(db.execute('''SELECT 1 FROM education_stage_spans es
                        JOIN work_search w ON w.poem_id=es.poem_id WHERE es.poem_id=? AND es.stage=?
                        AND instr(substr(w.body,es.start_char+1,es.end_char-es.start_char),?)>0 LIMIT 1''',(pid,stage,q)).fetchone())
            items.append({'id':pid,'poem_id':pid,'title':p['title'],'author':p['author'],'dynasty':p['dynasty'],'source':p['source'],**snippet,**meta})
        with self.corpus.connect() as db:version=education.version(db)
        return {'mode':mode,'query':q,'total':total,'items':items,'offset':offset,'has_more':offset+limit<total,'stage':stage,'corpus_version':version}
    @staticmethod
    def snippet(text,q):
        text=simplify(text);positions=[i for i,c in enumerate(text) if HAN.fullmatch(c)];plain=''.join(text[i] for i in positions)
        start=plain.find(q) if q else -1
        if start>=0:
            a=positions[start];b=positions[start+len(q)-1]+1
            left=max(0,a-18);right=min(len(text),b+28)
            return {'text':('…' if left else '')+text[left:right]+('…' if right<len(text) else ''),'match_text':text[a:b]}
        return {'text':text[:80]+('…' if len(text)>80 else ''),'match_text':''}
