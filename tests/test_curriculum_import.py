import sqlite3

from scripts.import_curriculum_choices import add_poem
from scripts.prepare_gushici_import import pick, PoemText


def test_annotated_poem_page_parser_stops_before_related_works():
    parser = PoemText()
    parser.feed('<div class="gushici-box-text">甲，乙。<br />丙，丁。</div>'
                '<div class="gushici-box-text">其他诗句。</div>')
    assert ''.join(parser.parts) == '甲，乙。\n丙，丁。'


def test_excerpt_selection_and_missing_one_character_positions():
    chosen, reason = pick({'title':'采薇','first_line':'昔我往矣','matches':[
        {'site_id':1,'title':'采薇','raw_text':'采薇采薇，薇亦作止。昔我往矣，杨柳依依。'},
        {'site_id':2,'title':'采薇(节选)','raw_text':'昔我往矣，杨柳依依。今我来思，雨雪霏霏。'},
    ]})
    assert chosen['site_id'] == 2
    assert reason == 'unique_title_and_first_line'
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE poems(id TEXT PRIMARY KEY,title,author,dynasty,paragraphs,original,source);
        CREATE TABLE lines(id INTEGER PRIMARY KEY,text UNIQUE,n,rank);
        CREATE TABLE occurrences(poem_id,pos,line_id,PRIMARY KEY(poem_id,pos));
        CREATE TABLE chars(char,line_id,PRIMARY KEY(char,line_id));
        CREATE TABLE education_entries(id TEXT PRIMARY KEY,poem_id,status,body);
    ''')
    report = {'new_poems':0,'reused_poems':0,'new_lines':0,'skipped_segments':[]}
    pid,next_id = add_poem(db,{'id':'sample','title':'山坡羊','author':'张养浩','dynasty':'元',
        'paragraphs':['兴，百姓苦；亡，百姓苦。'],'original':'兴，百姓苦；亡，百姓苦。',
        'source_ids':['sample-source']},1,report)
    assert next_id == 2
    assert [row[0] for row in db.execute('SELECT pos FROM occurrences ORDER BY pos')] == [1,3]
    assert [row['pos'] for row in report['skipped_segments']] == [0,2]
    assert db.execute('SELECT count(*) FROM poems WHERE id=?',(pid,)).fetchone()[0] == 1
