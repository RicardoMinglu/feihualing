"""Permission-checked administration; no public administrator creation route."""
import datetime
import math
import secrets
import sqlite3
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .security import limited, password_slot


class Disable(BaseModel):
    model_config = ConfigDict(extra='forbid')
    disabled: bool


class FeedbackStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['pending', 'processed']


def instant(value):
    if value is None or value == '':
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            parsed = datetime.datetime.fromisoformat(value)
            result = parsed.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        except (TypeError, ValueError):
            raise HTTPException(400, '时间范围无效')
    if not math.isfinite(result):
        raise HTTPException(400, '时间范围无效')
    return result


def event_filter(start, end, function, alias='e'):
    start, end = instant(start), instant(end)
    if start is not None and end is not None and start >= end:
        raise HTTPException(400, '结束时间须晚于开始时间')
    where, values = [], []
    for op, value in [('>=', start), ('<', end)]:
        if value is not None:
            where.append(f'{alias}.created_at {op} ?'); values.append(value)
    if function:
        where.append(f'{alias}.function=?'); values.append(function)
    return where, values


def pagination(offset, limit):
    if not 0 <= offset <= 1000000 or not 1 <= limit <= 100:
        raise HTTPException(400, '分页参数无效')


def build_router(get_accounts, get_community, require_admin):
    router = APIRouter(prefix='/api/admin', dependencies=[Depends(require_admin)])

    def connect():
        db = sqlite3.connect(get_accounts().path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def audit(db, request, target, action):
        db.execute('INSERT INTO admin_audit(id,actor_id,target_id,action,result,created_at) VALUES(?,?,?,?,?,?)',
                   (secrets.token_hex(16), request.state.owner, target, action, 'success', time.time()))

    @router.get('/overview')
    def overview(start: str | None = None, end: str | None = None):
        where, params = event_filter(start, end, None)
        where.append("EXISTS(SELECT 1 FROM users u WHERE u.id=e.user_id AND u.role='user')")
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with connect() as db:
            totals = dict(db.execute('SELECT kind,count(*) FROM users WHERE role=\'user\' GROUP BY kind'))
            active, events = db.execute('SELECT count(DISTINCT user_id),count(*) FROM activity_events e' + clause, params).fetchone()
            notices = dict(db.execute('SELECT status,count(*) FROM notification_jobs GROUP BY status'))
            return {'users_total': sum(totals.values()), 'guests_total': totals.get('guest', 0),
                    'accounts_total': totals.get('account', 0), 'active_users': active, 'events_total': events,
                    'feedback_pending': db.execute("SELECT count(*) FROM feedback WHERE status='pending'").fetchone()[0],
                    'notifications': {key: notices.get(key, 0) for key in ('pending', 'sending', 'sent', 'retrying', 'failed')},
                    'retention_days': 90}

    @router.get('/users')
    def users(kind: str = '', q: str = '', start: str | None = None, end: str | None = None,
              function: str = '', offset: int = 0, limit: int = 30):
        pagination(offset, limit)
        if kind not in ('', 'guest', 'account') or len(q) > 128:
            raise HTTPException(400, '筛选条件无效')
        clauses, params = ["u.role='user'"], []
        if kind:
            clauses.append('u.kind=?'); params.append(kind)
        if q:
            clauses.append("(u.id LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\')")
            match = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            params.extend([match, match])
        ewhere, eparams = event_filter(start, end, function)
        if ewhere:
            clauses.append('EXISTS(SELECT 1 FROM activity_events e WHERE e.user_id=u.id AND ' + ' AND '.join(ewhere) + ')')
            params.extend(eparams)
        clause = ' WHERE ' + ' AND '.join(clauses)
        with connect() as db:
            total = db.execute('SELECT count(*) FROM users u' + clause, params).fetchone()[0]
            rows = db.execute('SELECT u.id,(SELECT count(*) FROM activity_events e WHERE e.user_id=u.id' +
                              (' AND ' + ' AND '.join(ewhere) if ewhere else '') + ') event_count FROM users u' + clause +
                              ' ORDER BY u.last_active_at DESC,u.created_at DESC LIMIT ? OFFSET ?', eparams + params + [limit, offset]).fetchall()
        return {'items': [get_accounts().get(r['id']) | {'event_count': r['event_count']} for r in rows], 'total': total}

    @router.get('/users/{user_id}/events')
    def events(user_id: str, function: str = '', start: str | None = None, end: str | None = None, offset: int = 0, limit: int = 30):
        pagination(offset, limit)
        user = get_accounts().get(user_id)
        if not user:
            raise HTTPException(404, '用户不存在')
        where, params = event_filter(start, end, function)
        where.insert(0, 'e.user_id=?'); params.insert(0, user_id)
        clause = ' WHERE ' + ' AND '.join(where)
        with connect() as db:
            total = db.execute('SELECT count(*) FROM activity_events e' + clause, params).fetchone()[0]
            items = [dict(r) for r in db.execute('SELECT e.id,e.user_id,e.function,e.outcome,e.created_at FROM activity_events e' + clause +
                                                ' ORDER BY e.created_at DESC,e.id DESC LIMIT ? OFFSET ?', params + [limit, offset])]
            counts = [dict(r) for r in db.execute('SELECT function,count(*) count FROM activity_events e' + clause + ' GROUP BY function', params)]
        return {'items': items, 'total': total, 'counts': counts, 'user': user}

    @router.post('/users/{user_id}/disabled')
    def disabled(user_id: str, body: Disable, request: Request):
        limited(request, 'admin-write', 30, user=True)
        get_accounts().set_disabled(request.state.owner, user_id, body.disabled)
        return {'user': get_accounts().get(user_id)}

    @router.post('/users/{user_id}/reset-password')
    def reset(user_id: str, request: Request):
        limited(request, 'admin-reset', 10, user=True)
        with password_slot():
            temporary = get_accounts().reset_password(request.state.owner, user_id)
        return {'temporary_password': temporary}

    @router.get('/feedback')
    def feedback(status: str = '', offset: int = 0, limit: int = 30):
        pagination(offset, limit)
        if status not in ('', 'pending', 'processed'):
            raise HTTPException(400, '反馈状态无效')
        clause, params = (' WHERE f.status=?', [status]) if status else ('', [])
        with connect() as db:
            total = db.execute('SELECT count(*) FROM feedback f' + clause, params).fetchone()[0]
            items = [dict(r) for r in db.execute('''SELECT f.*,u.username,n.status notification_status FROM feedback f
                LEFT JOIN users u ON u.id=f.user_id LEFT JOIN notification_jobs n ON n.id=f.notification_id''' + clause +
                ' ORDER BY f.created_at DESC LIMIT ? OFFSET ?', params + [limit, offset])]
        for item in items:
            item['display_name'] = item['username'] or ('游客·' + item['user_id'][-6:])
            item.pop('fingerprint', None)
        return {'items': items, 'total': total}

    @router.patch('/feedback/{feedback_id}')
    def update_feedback(feedback_id: str, body: FeedbackStatus, request: Request):
        limited(request, 'admin-write', 30, user=True)
        try:
            with connect() as db:
                if not db.execute('UPDATE feedback SET status=?,updated_at=? WHERE id=?', (body.status, time.time(), feedback_id)).rowcount:
                    raise HTTPException(404, '反馈不存在')
                audit(db, request, feedback_id, 'feedback.' + body.status)
        except HTTPException:
            get_accounts().audit(request.state.owner, feedback_id, 'feedback.' + body.status, 'failed')
            raise
        return {'status': body.status}

    @router.get('/notifications')
    def notifications(status: str = '', offset: int = 0, limit: int = 30):
        pagination(offset, limit)
        if status not in ('', 'pending', 'sending', 'sent', 'retrying', 'failed'):
            raise HTTPException(400, '通知状态无效')
        clause, params = (' WHERE status=?', [status]) if status else ('', [])
        with connect() as db:
            total = db.execute('SELECT count(*) FROM notification_jobs' + clause, params).fetchone()[0]
            items = [dict(r) for r in db.execute('''SELECT id,kind,user_id,reference_id,status,attempts,created_at,
                next_attempt_at,sent_at,last_error FROM notification_jobs''' + clause + ' ORDER BY created_at DESC LIMIT ? OFFSET ?', params + [limit, offset])]
        return {'items': items, 'total': total}

    @router.post('/notifications/{notification_id}/retry')
    def retry(notification_id: str, request: Request):
        limited(request, 'admin-retry', 10, user=True)
        result = get_community().retry_notification(notification_id, actor_id=request.state.owner)
        return result or {'status': 'pending'}

    @router.get('/audit')
    def audits(offset: int = 0, limit: int = 30):
        pagination(offset, limit)
        with connect() as db:
            return {'items': [dict(r) for r in db.execute('SELECT * FROM admin_audit ORDER BY created_at DESC LIMIT ? OFFSET ?', (limit, offset))],
                    'total': db.execute('SELECT count(*) FROM admin_audit').fetchone()[0]}

    return router
