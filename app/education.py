STAGES = {'all': '不限', 'primary': '小学', 'junior': '初中', 'senior': '高中'}


def checked_stage(stage):
    if stage not in STAGES:
        raise ValueError('学段无效')
    return stage


def ready(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='education_work_stage'").fetchone())


def version(db):
    row = db.execute("SELECT value FROM meta WHERE key='education'").fetchone()
    return row[0] if row else 'legacy'


def poem_metadata(db, pid, stage='all'):
    if not ready(db):
        return {'education_stages': [], 'stage_match': False, 'verification': 'unreviewed'}
    stages = [r[0] for r in db.execute('SELECT DISTINCT stage FROM education_work_stage WHERE poem_id=? ORDER BY stage', (pid,))]
    verified = bool(db.execute('SELECT 1 FROM education_verified WHERE poem_id=?', (pid,)).fetchone())
    return {'education_stages': stages, 'stage_match': stage in stages,
            'verification': 'verified' if verified else 'unreviewed'}
