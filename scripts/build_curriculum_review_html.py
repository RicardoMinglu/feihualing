"""Generate a self-contained review page for unresolved curriculum matches."""
import argparse
import difflib
import hashlib
import html
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = ROOT / 'docs/current-stage-build-20261004.json'
CORPUS = ROOT / 'data/poetry.sqlite3'
TEMPLATE = ROOT / 'previews/curriculum-review.template.html'
OUTPUT = ROOT / 'previews/curriculum-review.html'

REFERENCES = {
    '芙蓉楼送辛渐': {
        'id': 'reference:pep-tang-2023:furong',
        'title': '芙蓉楼送辛渐',
        'author': '王昌龄',
        'dynasty': '唐',
        'source': '人教社转载《语文课本上的80首唐诗》',
        'source_url': 'https://www.pep.com.cn/rjdt/mtbd/202307/t20230718_1984406.shtml',
        'paragraphs': [
            '寒雨连江夜入吴，平明送客楚山孤。',
            '洛阳亲友如相问，一片冰心在玉壶。',
        ],
    },
}


def highlight_differences(text, comparison):
    matcher = difflib.SequenceMatcher(None, text, comparison, autojunk=False)
    parts = []
    for operation, start, end, _, _ in matcher.get_opcodes():
        segment = html.escape(text[start:end])
        if operation in ('replace', 'delete') and segment:
            parts.append('<mark>'+segment+'</mark>')
        else:
            parts.append(segment)
    return ''.join(parts)


def create_payload(corpus=CORPUS, report=REPORT):
    raw = pathlib.Path(report).read_bytes()
    review = json.loads(raw)
    books = json.loads((ROOT / 'data/curriculum-current-books.json').read_text())
    by_id = {book['id']: book for book in books}
    with sqlite3.connect(f'file:{corpus}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        issues = []
        for item in review['ambiguous']:
            book = by_id[item['book_id']]
            variants = []
            for poem_id in item['candidate_ids']:
                row = db.execute('SELECT id,title,author,dynasty,paragraphs,source FROM poems WHERE id=?',
                                 (poem_id,)).fetchone()
                if row is None:
                    raise ValueError('候选作品不存在: '+poem_id)
                variant = dict(row)
                variant['paragraphs'] = json.loads(variant['paragraphs'])
                variant['source_url'] = None
                variants.append(variant)
            if item['title'] in REFERENCES:
                variants.append(dict(REFERENCES[item['title']]))
            texts = ['\n'.join(variant['paragraphs']) for variant in variants]
            for index, variant in enumerate(variants):
                comparison = texts[1] if index == 0 else texts[0]
                variant['diff_html'] = highlight_differences(texts[index], comparison)
            issues.append({
                'key': f"{item['book_id']}:{item['title']}:{item['author']}",
                'book_id': item['book_id'],
                'volume': item['volume'],
                'stage': book['stage'],
                'title': item['title'],
                'author': item['author'],
                'book_url': f"https://book.pep.com.cn/{item['book_id']}/mobile/index.html",
                'variants': variants,
            })
    missing = []
    for item in review['missing']:
        book = by_id[item['book_id']]
        missing.append({
            'title': item['title'],
            'author': item['author'],
            'volume': item['volume'],
            'stage': book['stage'],
            'book_url': f"https://book.pep.com.cn/{item['book_id']}/mobile/index.html",
        })
    return {'version': 1, 'report_sha256': hashlib.sha256(raw).hexdigest(),
            'ambiguous': issues, 'missing': missing}


def build(output=OUTPUT, corpus=CORPUS, report=REPORT):
    payload = create_payload(corpus, report)
    document = TEMPLATE.read_text()
    marker = '<!-- REVIEW_DATA -->'
    if document.count(marker) != 1:
        raise ValueError('HTML 数据插槽缺失或重复')
    packed = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    document = document.replace(marker, packed)
    pathlib.Path(output).write_text(document)
    return {'output': str(pathlib.Path(output).resolve()),
            'ambiguous': len(payload['ambiguous']), 'missing': len(payload['missing'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=pathlib.Path, default=OUTPUT)
    parser.add_argument('--corpus', type=pathlib.Path, default=CORPUS)
    parser.add_argument('--report', type=pathlib.Path, default=REPORT)
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.corpus, args.report), ensure_ascii=False))
