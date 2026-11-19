import re,unicodedata
from functools import lru_cache
from opencc import OpenCC
from pypinyin import lazy_pinyin
from rapidfuzz.distance import Levenshtein
cc=OpenCC('t2s')
HAN=re.compile(r'^[\u3400-\u9fff\U00020000-\U0002fa1f]+$')
def simplify(text): return cc.convert(unicodedata.normalize('NFKC',text))
def normalize(text): return ''.join(c for c in simplify(text) if HAN.fullmatch(c))
def word(text):
    value=simplify(text.strip())
    if len(value)!=1 or not HAN.fullmatch(value): raise ValueError('请输入一个汉字')
    return value
@lru_cache(maxsize=20000)
def pinyin(text): return tuple(lazy_pinyin(text,errors=lambda s:list(s)))
def allowance(n): return 0 if n<5 else 1 if n<=7 else 2

def similarity(raw,candidate):
    edits=Levenshtein.distance(raw,candidate)
    limit=allowance(len(candidate))
    if edits>limit or abs(len(raw)-len(candidate))>limit:return None
    literal=1-edits/max(len(raw),len(candidate))
    phonetic=1-Levenshtein.distance(pinyin(raw),pinyin(candidate))/max(len(raw),len(candidate))
    score=.65*literal+.35*phonetic
    return {'score':round(score,4),'edits':edits,'phonetic':phonetic,'literal':literal}
