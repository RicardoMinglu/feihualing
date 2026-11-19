import os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.getenv('FEIHUA_DATA_DIR',ROOT/'data'))
CORPUS=DATA/'poetry.sqlite3'
GAMES=DATA/'games.sqlite3'
CORPUS_CACHE_SIZE=max(1,min(16,int(os.getenv('FEIHUA_CORPUS_CACHE_SIZE','16'))))
LEVELS={'easy':{'label':'简单','rounds':5,'seconds':None,'hints':2},'normal':{'label':'正常','rounds':10,'seconds':None,'hints':0},'hard':{'label':'困难','rounds':15,'seconds':None,'hints':0},'endless':{'label':'无限','rounds':None,'seconds':None,'hints':None}}
