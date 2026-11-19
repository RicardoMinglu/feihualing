"""Verify selected poem bodies against detail pages and prepare an import manifest."""
import argparse
import datetime
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.text import normalize

CANDIDATES = ROOT/'data/curriculum-gushici-candidates.json'
DECISIONS = ROOT/'data/curriculum-review-decisions.json'
STAGE_REPORT = ROOT/'docs/current-stage-build-20261004.json'
OUTPUT = ROOT/'data/curriculum-gushici-import.json'
REPORT = ROOT/'docs/gushici-import-review.json'

NOTE = re.compile(r'[（(]([^（）()]{1,80}(?:一作|同|通|又作|一云)[^（）()]{0,80})[）)]')


class PoemText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.done = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            if tag == 'br':
                self.parts.append('\n')
                return
            if tag in ('img', 'hr', 'input', 'meta', 'link'):
                return
            self.depth += 1
        elif not self.done and tag == 'div' and 'gushici-box-text' in dict(attrs).get('class', '').split():
            self.depth = 1

    def handle_startendtag(self, tag, attrs):
        if self.depth and tag == 'br':
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.done = True

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def pick(item):
    candidates = item['matches']
    if not candidates:
        return None, 'no_match'
    first = normalize(item.get('first_line') or '')
    if first:
        starting = [candidate for candidate in candidates if normalize(candidate['raw_text']).startswith(first)]
        if starting:
            candidates = starting
    literal = [candidate for candidate in candidates if candidate['title'].strip() == item['title'].strip()]
    if len(literal) == 1:
        return literal[0], 'exact_site_title'
    title = normalize(item['title'])
    exact = [candidate for candidate in candidates if normalize(candidate['title']) == title]
    if exact:
        candidates = exact
    if len(candidates) == 1:
        return candidates[0], 'unique_title_and_first_line' if first else 'unique_title'
    bodies = {normalize(NOTE.sub('', candidate['raw_text'])) for candidate in candidates}
    if len(bodies) == 1:
        return min(candidates, key=lambda candidate: candidate['site_id']), 'identical_text'
    return None, 'multiple_different_versions'


def detail_text(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Feihualing curriculum review (public-domain poem text)'})
    with urllib.request.urlopen(request, timeout=18) as response:
        if response.geturl().split('/')[2] != 'www.gushici.net':
            raise ValueError('作品页重定向至未知站点')
        document = response.read(2_000_000).decode('utf-8', 'replace')
    parser = PoemText()
    parser.feed(document)
    if not parser.done:
        raise ValueError('作品页正文未找到')
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def paragraphs(text):
    lines = []
    for row in text.splitlines():
        row = re.sub(r'\s+', '', row)
        lines.extend(piece for piece in re.split(r'(?<=[。！？])', row) if piece)
    return lines


def prepare(candidates_path=CANDIDATES, decisions_path=DECISIONS, output=OUTPUT, report_path=REPORT):
    candidates_path, decisions_path, output, report_path = map(pathlib.Path,
        (candidates_path, decisions_path, output, report_path))
    source = json.loads(candidates_path.read_text())
    decisions = json.loads(decisions_path.read_text())
    if decisions['schema_version'] != 1 or len(decisions['decisions']) != 5:
        raise ValueError('校对决定数量或格式无效')
    if decisions['report_sha256'] != hashlib.sha256(STAGE_REPORT.read_bytes()).hexdigest():
        raise ValueError('校对决定与学段报告版本不符')
    previous = {}
    if output.exists():
        previous = {item['id']: item for item in json.loads(output.read_text()).get('items', [])}
    selected = []
    held = []
    for index, item in enumerate(source['items'], 1):
        candidate, reason = pick(item)
        if candidate is None:
            held.append({'title': item['title'], 'volume': item['volume'], 'reason': reason,
                'candidates': [row['source_url'] for row in item['matches']]})
            continue
        existing = previous.get('gushici-'+str(candidate['site_id']))
        if existing and existing['source_url'] == candidate['source_url'] and \
                existing['book_id'] == item['book_id'] and existing['title'] == item['title']:
            selected.append(existing)
            print(f'{index}/{len(source["items"])} {item["title"]}: reused', flush=True)
            continue
        time.sleep(.6)
        try:
            original = detail_text(candidate['source_url'])
            if normalize(original) != normalize(candidate['raw_text']):
                raise ValueError('详情页与搜索索引正文不同')
            notes = NOTE.findall(original)
            clean = NOTE.sub('', original)
            if any(char in clean for char in '()（）□�'):
                raise ValueError('原文中仍有括注或残字')
            parts = paragraphs(clean)
            if not parts or any(not normalize(part) for part in parts):
                raise ValueError('诗文分段无效')
            selected.append({
                'id': 'gushici-'+str(candidate['site_id']),
                'book_id': item['book_id'], 'volume': item['volume'], 'stage': item['stage'],
                'title': item['title'], 'author': item['author'],
                'site_title': candidate['title'], 'site_author': candidate['author'],
                'dynasty': candidate['dynasty'], 'source_url': candidate['source_url'],
                'source_notes': notes, 'selection_reason': reason,
                'original': original, 'paragraphs': parts,
                'status': 'pending',
            })
            print(f'{index}/{len(source["items"])} {item["title"]}: ready', flush=True)
        except (ValueError, OSError) as error:
            held.append({'title': item['title'], 'volume': item['volume'],
                'reason': str(error), 'source_url': candidate['source_url']})
            print(f'{index}/{len(source["items"])} {item["title"]}: held {error}', flush=True)
    output.write_text(json.dumps({'source': 'https://so.gushici.net/',
        'prepared_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'items': selected}, ensure_ascii=False, indent=2)+'\n')
    report = {'requested': len(source['items']), 'prepared': len(selected), 'held': held,
        'decisions': [{'title': x['title'], 'kind': x['kind'],
            'version_id': x.get('version_id')} for x in decisions['decisions']]}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidates', type=pathlib.Path, default=CANDIDATES)
    parser.add_argument('--decisions', type=pathlib.Path, default=DECISIONS)
    parser.add_argument('--output', type=pathlib.Path, default=OUTPUT)
    parser.add_argument('--report', type=pathlib.Path, default=REPORT)
    args = parser.parse_args()
    report = prepare(args.candidates, args.decisions, args.output, args.report)
    print(json.dumps({'prepared': report['prepared'], 'held': len(report['held'])}, ensure_ascii=False))
