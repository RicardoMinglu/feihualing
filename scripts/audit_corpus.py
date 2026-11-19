"""Read-only corpus audit. Differences are leads for review, never automatic edits."""
import argparse
import collections
import json
import pathlib
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import CORPUS, ROOT
from app.text import normalize, simplify

BAD = re.compile(r'[□�\uFFFD]|[A-Za-z]|\{[^}]+\}|\([^)]{1,40}\)|（[^）]{1,40}）')


def audit(source, output, curriculum=ROOT/'data/curriculum-poems.json'):
    source, output, curriculum = map(pathlib.Path, (source,output,curriculum))
    if output.exists():raise ValueError('报告目录已存在')
    output.mkdir(parents=True)
    started=time.monotonic();counts=collections.Counter();examples=collections.Counter()
    with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as db, (output/'issues.jsonl').open('w') as stream:
        db.row_factory=sqlite3.Row
        def issue(kind,row,detail):
            counts[kind]+=1
            stream.write(json.dumps({'kind':kind,'poem_id':row['id'],'title':row['title'],
                'author':row['author'],'source':row['source'],'detail':detail},ensure_ascii=False)+'\n')
        for row in db.execute('SELECT id,title,author,paragraphs,source,original FROM poems'):
            counts['poems_scanned']+=1
            for field in ('title','author','original'):
                if not row[field]:issue('missing_metadata',row,field)
            text=row['original'] or ''
            for match in BAD.finditer(text):
                issue('suspect_character',row,{'offset':match.start(),'text':match.group(0)})
        previous_key=None;previous_body=None;previous_id=None
        for row in db.execute('SELECT id,title,author,paragraphs,source FROM poems ORDER BY author,title,id'):
            key=(row['author'],row['title'])
            body=normalize(''.join(json.loads(row['paragraphs'])))
            if key==previous_key and body!=previous_body:
                issue('same_title_variant',row,{'compared_with':previous_id})
            previous_key,previous_body,previous_id=key,body,row['id']
        entries=json.loads(curriculum.read_text())
        diffs=[]
        for entry in entries:
            matches=[dict(r) for r in db.execute('SELECT id,title,author,paragraphs,source FROM poems WHERE title=? AND author=?',(simplify(entry['title']),simplify(entry['author'])))]
            expected=normalize(''.join(entry.get('paragraphs') or []))
            exact=[x['id'] for x in matches if normalize(''.join(json.loads(x['paragraphs'])))==expected] if expected else []
            if expected and not exact:counts['curriculum_differences']+=1
            diffs.append({'entry_id':entry['id'],'status':entry['status'],'matched_poems':[x['id'] for x in matches],
                'exact_text_poems':exact,'difference':bool(expected and not exact)})
    summary={'source':str(source),'poems_scanned':counts.pop('poems_scanned',0),
        'issues':dict(counts),'curriculum_entries':len(entries),'elapsed_seconds':round(time.monotonic()-started,2)}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    (output/'curriculum-diff.json').write_text(json.dumps(diffs,ensure_ascii=False,indent=2))
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=pathlib.Path,default=CORPUS)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    args=parser.parse_args()
    print(json.dumps(audit(args.source,args.output),ensure_ascii=False))
