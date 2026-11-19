import json,sqlite3,re
from contextlib import contextmanager
from functools import lru_cache
from rapidfuzz import process
from rapidfuzz.distance import Levenshtein
from .config import CORPUS,CORPUS_CACHE_SIZE
from .text import normalize,word,similarity,allowance,HAN,simplify
from . import education

class Corpus:
    def __init__(self,path=CORPUS):self.path=path
    @contextmanager
    def connect(self):
        db=sqlite3.connect(f'file:{self.path}?mode=ro',uri=True);db.row_factory=sqlite3.Row
        try:yield db
        finally:db.close()
    def stats(self):
        with self.connect() as db:return json.loads(db.execute("SELECT value FROM meta WHERE key='import'").fetchone()[0])
    @lru_cache(maxsize=1)
    def pairing_stats(self):
        with self.connect() as db:
            row=db.execute("SELECT value FROM meta WHERE key='pairing'").fetchone()
            return json.loads(row[0]) if row else None
    PAIR_SELECT='''SELECT q.id,q.first_id,q.second_id,q.poem_id,p.title,p.author,p.dynasty,p.source,
        a.text first_text,b.text second_text FROM pair_chars c
        JOIN pairs q ON q.id=c.pair_id JOIN poems p ON p.id=q.poem_id
        JOIN lines a ON a.id=q.first_id JOIN lines b ON b.id=q.second_id'''
    SINGLE_FALLBACK='''FROM chars c JOIN lines l ON l.id=c.line_id
        LEFT JOIN pair_members m ON m.line_id=l.id WHERE c.char=? AND m.line_id IS NULL'''
    @staticmethod
    def pair_item(row):
        item=dict(row)
        item['line_ids']=[item.pop('first_id'),item.pop('second_id')]
        item['clauses']=[item.pop('first_text'),item.pop('second_text')]
        item['text']='，'.join(item['clauses'])+'。'
        item['unit']='pair'
        return item
    @lru_cache(maxsize=CORPUS_CACHE_SIZE)
    def pair_counts(self,char):
        with self.connect() as db:
            pairs=db.execute('SELECT count(*) FROM pair_chars WHERE char=?',(char,)).fetchone()[0]
            singles=db.execute('SELECT count(*) '+self.SINGLE_FALLBACK,(char,)).fetchone()[0]
            return pairs,singles
    def search_pairs(self,char,offset,limit,stage='all'):
        if not self.pairing_stats():
            raise ValueError('上下句索引尚未构建')
        with self.connect() as db:
            if education.ready(db):
                return self.education_search(db,char,offset,limit,stage,'pair')
        pairs,singles=self.pair_counts(char);items=[]
        with self.connect() as db:
            if offset<pairs:
                rows=db.execute(self.PAIR_SELECT+' WHERE c.char=? ORDER BY c.rank,c.sort_id,c.pair_id LIMIT ? OFFSET ?',(char,limit,offset))
                items=[self.pair_item(r) for r in rows]
            if len(items)<limit:
                rows=db.execute('SELECT l.id '+self.SINGLE_FALLBACK+' ORDER BY l.rank,l.id LIMIT ? OFFSET ?',(char,limit-len(items),max(0,offset-pairs)))
                for row in rows:
                    item=self.detail(row['id']);items.append(dict(item,line_ids=[row['id']],clauses=[item['text']],unit='single'))
        total=pairs+singles
        return {'word':char,'unit':'pair','total':total,'items':items,'offset':offset,'has_more':offset+limit<total,'stage':stage,'corpus_version':'legacy'}
    def education_search(self,db,char,offset,limit,stage,unit):
        # Curriculum rows are sparse. Fetch them first, then page through the existing indexed order.
        priority={}
        if unit=='pair':
            for pair_id,rank,sort_id,tag_stage,verified in db.execute('''SELECT ep.pair_id,c.rank,c.sort_id,ep.stage,ep.verified
                FROM education_pair_stage ep JOIN pair_chars c ON c.pair_id=ep.pair_id
                WHERE c.char=? AND NOT EXISTS(SELECT 1 FROM education_inactive_pairs x WHERE x.pair_id=ep.pair_id)''',(char,)):
                if not verified and (stage=='all' or tag_stage!=stage):continue
                key=(pair_id,0)
                candidate=(0 if stage!='all' and tag_stage==stage else 1,0 if verified else 1,0,rank,sort_id,pair_id)
                if key not in priority or candidate<priority[key]:priority[key]=candidate
        for line_id,rank,tag_stage,verified in db.execute('''SELECT es.line_id,l.rank,es.stage,es.verified
            FROM education_line_stage es JOIN chars c ON c.line_id=es.line_id
            JOIN lines l ON l.id=es.line_id
            WHERE c.char=? AND NOT EXISTS(SELECT 1 FROM education_inactive_lines x WHERE x.line_id=es.line_id)
            '''+('AND NOT EXISTS(SELECT 1 FROM pair_members m WHERE m.line_id=es.line_id)' if unit=='pair' else ''),(char,)):
            if not verified and (stage=='all' or tag_stage!=stage):continue
            key=(str(line_id),1)
            candidate=(0 if stage!='all' and tag_stage==stage else 1,0 if verified else 1,1,rank,line_id,str(line_id))
            if key not in priority or candidate<priority[key]:priority[key]=candidate
        ordered=sorted(priority,key=lambda key:priority[key])
        if unit=='pair':
            pairs,singles=self.pair_counts(char)
            inactive_pairs=db.execute('SELECT count(*) FROM education_inactive_pairs x JOIN pair_chars c ON c.pair_id=x.pair_id WHERE c.char=?',(char,)).fetchone()[0]
            inactive_singles=db.execute('''SELECT count(*) FROM education_inactive_lines x JOIN chars c ON c.line_id=x.line_id
                WHERE c.char=? AND NOT EXISTS(SELECT 1 FROM pair_members m WHERE m.line_id=x.line_id)''',(char,)).fetchone()[0]
            total=pairs+singles-inactive_pairs-inactive_singles
            normal_pairs=pairs-inactive_pairs-sum(kind==0 for _,kind in ordered)
        else:
            total=db.execute('SELECT count(*) FROM chars WHERE char=?',(char,)).fetchone()[0]
            total-=db.execute('SELECT count(*) FROM education_inactive_lines x JOIN chars c ON c.line_id=x.line_id WHERE c.char=?',(char,)).fetchone()[0]
            normal_pairs=0
        rows=list(ordered[offset:offset+limit])
        priority_pairs=json.dumps([item_id for item_id,kind in ordered if kind==0])
        priority_lines=json.dumps([int(item_id) for item_id,kind in ordered if kind==1])
        remaining=limit-len(rows)
        normal_offset=max(0,offset-len(ordered))
        if remaining and unit=='pair' and normal_offset<normal_pairs:
            fetched=db.execute('''SELECT c.pair_id FROM pair_chars c WHERE c.char=?
                AND c.pair_id NOT IN (SELECT value FROM json_each(?))
                AND NOT EXISTS(SELECT 1 FROM education_inactive_pairs x WHERE x.pair_id=c.pair_id)
                ORDER BY c.rank,c.sort_id,c.pair_id LIMIT ? OFFSET ?''',(char,priority_pairs,remaining,normal_offset)).fetchall()
            rows.extend((r[0],0) for r in fetched)
            remaining-=len(fetched)
        if remaining:
            line_offset=normal_offset-normal_pairs if unit=='pair' else normal_offset
            line_offset=max(0,line_offset)
            fetched=db.execute('''SELECT l.id FROM chars c JOIN lines l ON l.id=c.line_id
                WHERE c.char=? AND l.id NOT IN (SELECT value FROM json_each(?))
                AND NOT EXISTS(SELECT 1 FROM education_inactive_lines x WHERE x.line_id=l.id)
                '''+('AND NOT EXISTS(SELECT 1 FROM pair_members m WHERE m.line_id=l.id)' if unit=='pair' else '')+
                ' ORDER BY l.rank,l.id LIMIT ? OFFSET ?',(char,priority_lines,remaining,line_offset)).fetchall()
            rows.extend((str(r[0]),1) for r in fetched)
        items=[]
        for item_id,kind in rows:
            if kind==0:
                pair=db.execute(self.PAIR_SELECT+' WHERE c.char=? AND q.id=?',(char,item_id)).fetchone()
                item=self.pair_item(pair)
                provenance=db.execute('''SELECT p.id,p.title,p.author,p.dynasty,p.source FROM education_pair_stage ep
                    JOIN poems p ON p.id=ep.poem_id WHERE ep.pair_id=? ORDER BY
                    CASE WHEN ep.stage=? THEN 0 ELSE 1 END,ep.verified DESC,p.id LIMIT 1''',(item_id,stage)).fetchone()
                if provenance:item.update(poem_id=provenance['id'],title=provenance['title'],author=provenance['author'],dynasty=provenance['dynasty'],source=provenance['source'])
            else:
                item=self.detail(int(item_id),stage)
                if unit=='pair':item=dict(item,line_ids=[int(item_id)],clauses=[item['text']],unit='single')
            item.update(education.poem_metadata(db,item['poem_id'],stage))
            items.append(item)
        result={'word':char,'total':total,'items':items,'offset':offset,'has_more':offset+limit<total,'stage':stage,'corpus_version':education.version(db)}
        if unit=='pair':result['unit']='pair'
        return result
    def computer_options(self,char,used):
        used=self.equivalent_ids(used)
        if self.pairing_stats():
            params=[char];exclude=''
            if used:
                exclude=' AND q.first_id NOT IN (SELECT value FROM json_each(?)) AND q.second_id NOT IN (SELECT value FROM json_each(?))'
                packed=json.dumps(list(used));params.extend([packed,packed])
            with self.connect() as db:
                inactive=' AND NOT EXISTS(SELECT 1 FROM education_inactive_pairs x WHERE x.pair_id=q.id)' if education.ready(db) else ''
                rows=db.execute(self.PAIR_SELECT+' WHERE c.char=?'+exclude+inactive+' ORDER BY c.rank,c.sort_id,c.pair_id LIMIT 30',params)
                pairs=[self.pair_item(r) for r in rows]
            if pairs:return pairs
        return [dict(self.detail(r['id']),line_ids=[r['id']]) for r in self.available(char,used)[:30]]
    @lru_cache(maxsize=CORPUS_CACHE_SIZE)
    def pool(self,char):
        with self.connect() as db:
            condition=' AND NOT EXISTS(SELECT 1 FROM education_inactive_lines x WHERE x.line_id=l.id)' if education.ready(db) else ''
            return [dict(r) for r in db.execute('SELECT l.* FROM chars c JOIN lines l ON l.id=c.line_id WHERE c.char=?'+condition+' ORDER BY l.rank,l.id',(char,))]
    def available(self,char,used):return [r for r in self.pool(char) if r['id'] not in used]
    def detail(self,lid,stage='all'):
        with self.connect() as db:
            if education.ready(db):
                row=db.execute('''SELECT l.id,l.text,p.id poem_id,p.title,p.author,p.dynasty,p.source
                    FROM lines l JOIN occurrences o ON o.line_id=l.id JOIN poems p ON p.id=o.poem_id
                    LEFT JOIN education_line_stage es ON es.line_id=l.id AND es.poem_id=p.id AND es.stage=?
                    WHERE l.id=? AND NOT EXISTS(SELECT 1 FROM education_invalid_occurrences x WHERE x.poem_id=o.poem_id AND x.pos=o.pos)
                    ORDER BY CASE WHEN es.stage IS NOT NULL THEN 0 ELSE 1 END,
                    CASE WHEN p.source=? THEN 0 ELSE 1 END,p.id LIMIT 1''',(stage,lid,'本项目常见诗词校对集')).fetchone()
            else:
                row=db.execute('SELECT l.id,l.text,p.id poem_id,p.title,p.author,p.dynasty,p.source FROM lines l JOIN occurrences o ON o.line_id=l.id JOIN poems p ON p.id=o.poem_id WHERE l.id=? ORDER BY CASE WHEN p.source=? THEN 0 ELSE 1 END,p.id LIMIT 1',(lid,'本项目常见诗词校对集')).fetchone()
            return dict(row) if row else None
    def poem(self,pid):
        with self.connect() as db:
            r=db.execute('SELECT * FROM poems WHERE id=?',(pid,)).fetchone()
            if not r:return None
            item={**dict(r),'paragraphs':json.loads(r['paragraphs']),**education.poem_metadata(db,pid)}
            if r['source'].startswith('校对补充/') and education.ready(db):
                row=db.execute('SELECT body FROM education_entries WHERE poem_id=? ORDER BY id LIMIT 1',(pid,)).fetchone()
                if row:
                    entry=json.loads(row[0]);url=entry.get('source_url') or entry.get('evidence','')
                    if isinstance(url,str) and url.startswith('https://www.gushici.net/shici/'):
                        item.update(source_label='古诗词网 · 原文',source_url=url)
                    elif isinstance(url,str) and url.startswith('https://www.pep.com.cn/'):
                        item.update(source_label='人民教育出版社',source_url=url)
            return item
    def search(self,char,offset=0,limit=20,unit='single',stage='all'):
        if unit=='pair':return self.search_pairs(word(char),offset,limit,stage)
        with self.connect() as db:
            if education.ready(db):return self.education_search(db,word(char),offset,limit,stage,'single')
        rows=self.pool(word(char));return {'word':word(char),'total':len(rows),'items':[self.detail(r['id']) for r in rows[offset:offset+limit]],'offset':offset,'has_more':offset+limit<len(rows),'stage':stage,'corpus_version':'legacy'}
    def exact(self,text):
        with self.connect() as db:
            condition=' AND NOT EXISTS(SELECT 1 FROM education_inactive_lines x WHERE x.line_id=lines.id)' if education.ready(db) else ''
            row=db.execute('SELECT * FROM lines WHERE text=?'+condition,(text,)).fetchone();return dict(row) if row else None
    def adjacent(self,first,second):
        with self.connect() as db:
            condition=' AND NOT EXISTS(SELECT 1 FROM education_invalid_occurrences x WHERE x.poem_id=a.poem_id AND x.pos IN (a.pos,b.pos))' if education.ready(db) else ''
            r=db.execute('SELECT p.id poem_id,p.title,p.author,p.dynasty,p.source FROM occurrences a JOIN occurrences b ON b.poem_id=a.poem_id AND b.pos=a.pos+1 JOIN poems p ON p.id=a.poem_id WHERE a.line_id=? AND b.line_id=?'+condition+' LIMIT 1',(first,second)).fetchone()
            return dict(r) if r else None
    def equivalent_ids(self,ids):
        ids=set(ids)
        if not ids:return ids
        with self.connect() as db:
            if not education.ready(db):return ids
            packed=json.dumps(list(ids))
            groups=[r[0] for r in db.execute('SELECT DISTINCT group_id FROM education_equivalents WHERE line_id IN (SELECT value FROM json_each(?))',(packed,))]
            if groups:
                ids.update(r[0] for r in db.execute('SELECT line_id FROM education_equivalents WHERE group_id IN (SELECT value FROM json_each(?))',(json.dumps(groups),)))
        return ids
    def valid_option(self,option):
        ids=option.get('line_ids',[])
        if len(ids) not in (1,2):return False
        with self.connect() as db:
            if education.ready(db):
                for lid in ids:
                    if db.execute('SELECT 1 FROM education_inactive_lines WHERE line_id=?',(lid,)).fetchone():return False
        return bool(self.detail(ids[0])) if len(ids)==1 else bool(self.adjacent(*ids))
    def option(self,rows,score=1.0,auto=True):
        meta=self.detail(rows[0]['id']) if len(rows)==1 else self.adjacent(rows[0]['id'],rows[1]['id'])
        if not meta:return None
        return {**meta,'line_ids':[r['id'] for r in rows],'clauses':[r['text'] for r in rows],'text':'，'.join(r['text'] for r in rows)+('。' if len(rows)==2 else ''),'score':score,'auto':auto}
    def fuzzy(self,text,char=None):
        if len(text)<5:return []
        if char:rows=[r for r in self.pool(char) if abs(r['n']-len(text))<=2]
        else:
            # Retrieve by characters shared with the input, without forcing the target into a correction.
            with self.connect() as db:
                chars=list(dict.fromkeys(text))[:24]
                rows=[dict(r) for r in db.execute(f'SELECT DISTINCT l.* FROM chars c JOIN lines l ON c.line_id=l.id WHERE c.char IN ({",".join("?" for _ in chars)}) AND l.n BETWEEN ? AND ?',(*chars,max(5,len(text)-2),len(text)+2))]
        scored=[]
        choices={r['id']:r['text'] for r in rows}
        for candidate,distance,lid in process.extract(text,choices,scorer=Levenshtein.distance,score_cutoff=2,limit=40):
            s=similarity(text,candidate)
            if s and s['score']>=.80:
                scored.append(({'id':lid,'text':candidate},s))
        return sorted(scored,key=lambda x:(-x[1]['score'],x[0]['id']))
    @lru_cache(maxsize=4096)
    def neighbors(self,lid,direction):
        with self.connect() as db:
            condition=' AND NOT EXISTS(SELECT 1 FROM education_invalid_occurrences x WHERE x.poem_id=a.poem_id AND x.pos IN (a.pos,b.pos))' if education.ready(db) else ''
            return [dict(r) for r in db.execute('SELECT DISTINCT l.* FROM occurrences a JOIN occurrences b ON b.poem_id=a.poem_id AND b.pos=a.pos+? JOIN lines l ON l.id=b.line_id WHERE a.line_id=?'+condition,(direction,lid))]
    def pair_options(self,left,right,char):
        options=[]
        # Anchor on an exact or target-containing clause, then inspect only its real neighbors.
        # This avoids broad scans of millions of unrelated lines for the other half.
        a,b=self.exact(left),self.exact(right)
        anchors=[]
        if a:anchors=[(a,{'score':1,'phonetic':1},1,right)]
        elif b:anchors=[(b,{'score':1,'phonetic':1},-1,left)]
        else:
            anchors=[(r,s,1,right) for r,s in self.fuzzy(left,char)]
            anchors.extend((r,s,-1,left) for r,s in self.fuzzy(right,char))
        for anchor,score,direction,raw in anchors:
            for neighbor in self.neighbors(anchor['id'],direction):
                if len(raw)<5 and raw!=neighbor['text']:continue
                match=similarity(raw,neighbor['text'])
                if not match or match['score']<.80:continue
                rows=[anchor,neighbor] if direction==1 else [neighbor,anchor]
                if not any(char in r['text'] for r in rows):continue
                opt=self.option(rows,min(score['score'],match['score']),score['phonetic']==match['phonetic']==1)
                if opt:options.append(opt)
        return options
    def judge(self,raw,char):
        if not raw.strip() or len(raw)>200:return {'kind':'invalid','message':'请输入一句或相邻上下句诗词'}
        cleaned=re.sub(r'^(?:嗯[，,\s]+|我说的是[：:\s]+)','',simplify(raw.strip()))
        if re.search(r'[A-Za-z0-9<>]',cleaned):return {'kind':'invalid','message':'请输入完整的古诗词原句'}
        text=normalize(cleaned)
        one=self.exact(text)
        pair=None
        if not one:
            for i in range(2,min(40,len(text)-1)):
                left,right=self.exact(text[:i]),self.exact(text[i:])
                if left and right and self.adjacent(left['id'],right['id']):pair=[left,right];break
        if one or pair:
            rows=[one] if one else pair
            if not any(char in x['text'] for x in rows):return {'kind':'missing_word','message':f'原句不含令字「{char}」'}
            return {'kind':'accepted','option':self.option(rows),'corrected':False}
        parts=[normalize(s) for s in re.split('[，。！？；,!?;\n]',cleaned) if normalize(s)]
        options=[]
        if len(parts)==2:
            options=self.pair_options(parts[0],parts[1],char)
        elif len(parts)<=1:
            for row,score in self.fuzzy(text,char):
                opt=self.option([row],score['score'],score['phonetic']==1)
                if opt:options.append(opt)
            if not options and 9<=len(text)<=80:
                for i in range(max(2,len(text)-40),min(40,len(text)-2)+1):
                    options.extend(self.pair_options(text[:i],text[i:],char))
        options=list({tuple(o['line_ids']):o for o in options}.values())
        options.sort(key=lambda o:(-o['score'],o['line_ids']))
        if not options:return {'kind':'invalid','message':'暂未找到可信的标准诗句，请检查后重试'}
        best=options[0]
        if best['auto'] and (len(options)==1 or best['score']-options[1]['score']>=.035):return {'kind':'accepted','option':best,'corrected':True}
        return {'kind':'candidates','message':'请确认你想说的诗句','options':options[:3]}
