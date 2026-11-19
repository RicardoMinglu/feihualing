"""Local activity records, feedback, and a durable Feishu notification outbox.

No network request is made while recording a user's action. Delivery is performed
by the worker, after the transaction has committed. Retries deliberately carry
the same notification ID because a timed-out remote send can already have arrived.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


FUNCTIONS = frozenset({
    'visit', 'search', 'poem_view', 'read_aloud', 'game_create', 'game_answer',
    'game_hint', 'game_end', 'feedback', 'support_view', 'support_save',
})
BROWSER_ACTIONS = frozenset({'read_aloud', 'support_view', 'support_save'})
FEEDBACK_KINDS = {'bug': '功能问题', 'suggestion': '建议', 'content': '诗词内容', 'other': '其他'}
ACTION_LABELS = {'support_view': '查看赞赏码', 'support_save': '点击保存赞赏码'}
RETENTION_SECONDS = 90 * 86400
SUPPORT_INTERVAL = 30 * 60
LEASE_SECONDS = 30
SEND_INTERVAL = 1.0
MAX_ATTEMPTS = 8


class CommunityError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', value):
        raise CommunityError('请求编号无效')
    return value


def _activity_receipt(user_id, function, request_id):
    # A receipt contains no readable identity, function or timestamp. Keeping it
    # after detail expiry prevents old network retries from creating new activity.
    return hashlib.sha256(json.dumps([user_id, function, request_id], separators=(',', ':')).encode()).hexdigest()


def record_activity(db, user_id, function, outcome='success', request_id=None, now=None):
    """Record inside the caller's business transaction; return False on replay."""
    if function not in FUNCTIONS:
        raise CommunityError('操作类型无效')
    if not isinstance(outcome, str) or not outcome or len(outcome) > 32:
        raise CommunityError('操作结果无效')
    now = time.time() if now is None else now
    if request_id is not None:
        # Business request IDs may include a game ID prefix. They are never sent
        # to Feishu and never contain search terms or a user's answer.
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise CommunityError('请求编号无效')
        added = db.execute('INSERT OR IGNORE INTO activity_requests(request_hash) VALUES(?)',
                           (_activity_receipt(user_id, function, request_id),)).rowcount
        if not added:
            return False
    db.execute('''INSERT INTO activity_events(user_id,function,outcome,created_at,request_id)
        VALUES(?,?,?,?,?)''', (user_id, function, outcome, now, request_id))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
        db.execute('UPDATE users SET last_active_at=MAX(COALESCE(last_active_at,0),?) WHERE id=?',
                   (now, user_id))
    return True


def _user_identity(user):
    user_id = user.get('id') or user.get('user_id')
    if not isinstance(user_id, str) or not user_id:
        raise CommunityError('请先刷新页面以恢复身份', 401)
    name = user.get('username') or user.get('display_name') or user.get('name') or '游客'
    return user_id, str(name)


def _timestamp(value):
    # Modern Beijing time is UTC+8; avoid requiring a system tzdata package in
    # the slim production container for formatting newly created notices.
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S 北京时间')


def _card(notification_id, title, lines):
    # User content is always plain_text: no Markdown, mentions or link expansion.
    return {
        'msg_type': 'interactive',
        'card': {
            'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': 'blue'},
            'elements': [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': line}}
                         for line in [f'通知编号：{notification_id}', *lines]],
        },
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Community:
    def __init__(self, path, *, sender=None, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sender = sender
        self.clock = clock or time.time
        with self.tx() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS activity_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                    function TEXT NOT NULL, outcome TEXT NOT NULL,
                    created_at REAL NOT NULL, request_id TEXT);
                CREATE INDEX IF NOT EXISTS activity_user_time ON activity_events(user_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS activity_function_time ON activity_events(function,created_at DESC);
                CREATE INDEX IF NOT EXISTS activity_time ON activity_events(created_at);
                CREATE TABLE IF NOT EXISTS activity_requests(
                    request_hash TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS feedback(
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,kind TEXT NOT NULL,
                    body TEXT NOT NULL,contact TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,notification_id TEXT NOT NULL,
                    UNIQUE(user_id,request_id));
                CREATE INDEX IF NOT EXISTS feedback_time ON feedback(created_at DESC);
                CREATE TABLE IF NOT EXISTS notification_jobs(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,reference_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL,
                    lease_until REAL,lease_token TEXT,created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,sent_at REAL,last_error TEXT);
                CREATE INDEX IF NOT EXISTS notification_due ON notification_jobs(status,next_attempt_at);
                CREATE INDEX IF NOT EXISTS notification_kind_time ON notification_jobs(kind,created_at);
                CREATE TABLE IF NOT EXISTS support_notifications(
                    user_id TEXT NOT NULL,action TEXT NOT NULL,last_notified_at REAL NOT NULL,
                    PRIMARY KEY(user_id,action));
                CREATE TABLE IF NOT EXISTS notification_rate_state(
                    id INTEGER PRIMARY KEY CHECK(id=1),next_send_at REAL NOT NULL);
                INSERT OR IGNORE INTO notification_rate_state VALUES(1,0);
            ''')
        # Convert the initial development schema without retaining readable
        # activity details beyond the event retention period. This is atomic and
        # repeatable even if a process is interrupted during local migration.
        with self.tx() as db:
            columns = {row['name'] for row in db.execute('PRAGMA table_info(activity_requests)')}
            if 'request_hash' not in columns:
                db.execute('CREATE TABLE activity_request_hashes(request_hash TEXT PRIMARY KEY)')
                db.executemany('INSERT OR IGNORE INTO activity_request_hashes VALUES(?)',
                               ((_activity_receipt(row[0], row[1], row[2]),) for row in
                                db.execute('SELECT user_id,function,request_id FROM activity_requests')))
                db.execute('DROP TABLE activity_requests')
                db.execute('ALTER TABLE activity_request_hashes RENAME TO activity_requests')

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def record(self, user_id, function, outcome='success', request_id=None):
        with self.tx() as db:
            return record_activity(db, user_id, function, outcome, request_id, self.clock())

    @staticmethod
    def _enqueue(db, notification_id, kind, reference_id, user_id, payload, now):
        db.execute('''INSERT INTO notification_jobs
            (id,kind,reference_id,user_id,payload,next_attempt_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)''', (notification_id, kind, reference_id, user_id,
            json.dumps(payload, ensure_ascii=False, separators=(',', ':')), now, now, now))

    def submit_feedback(self, user, kind, body, contact, request_id):
        user_id, name = _user_identity(user)
        _request_id(request_id)
        if kind not in FEEDBACK_KINDS:
            raise CommunityError('请选择反馈类型')
        if not isinstance(body, str) or not 5 <= len(body.strip()) <= 3000:
            raise CommunityError('反馈内容请填写 5 至 3000 字')
        if contact is None:
            contact = ''
        if not isinstance(contact, str) or len(contact.strip()) > 200:
            raise CommunityError('联系方式不能超过 200 字')
        body, contact = body.strip(), contact.strip()
        fingerprint = hashlib.sha256(json.dumps([kind, body, contact], ensure_ascii=False).encode()).hexdigest()
        now = self.clock()
        with self.tx() as db:
            existing = db.execute('''SELECT f.*,n.status AS notification_status FROM feedback f
                JOIN notification_jobs n ON n.id=f.notification_id
                WHERE f.user_id=? AND f.request_id=?''', (user_id, request_id)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise CommunityError('请求编号已用于另一条反馈', 409)
                return {'id': existing['id'], 'status': 'received',
                        'notification_status': existing['notification_status']}
            recent, daily = db.execute('''SELECT
                SUM(CASE WHEN created_at>? THEN 1 ELSE 0 END),COUNT(*) FROM feedback
                WHERE user_id=? AND created_at>?''', (now - 600, user_id, now - 86400)).fetchone()
            if (recent or 0) >= 5 or daily >= 20:
                raise CommunityError('提交较频繁，请稍后再试', 429)
            feedback_id, notice_id = 'FB-' + secrets.token_hex(8), 'NT-' + secrets.token_hex(8)
            db.execute('''INSERT INTO feedback(id,user_id,kind,body,contact,created_at,updated_at,
                request_id,fingerprint,notification_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (feedback_id, user_id, kind, body, contact, now, now, request_id, fingerprint, notice_id))
            payload = _card(notice_id, '飞花令 · 用户反馈', [
                f'反馈编号：{feedback_id}', f'用户：{name}\n用户 ID：{user_id}',
                f'提交时间：{_timestamp(now)}', f'类型：{FEEDBACK_KINDS[kind]}',
                f'正文：\n{body}', f'联系方式：{contact or "未填写"}',
            ])
            self._enqueue(db, notice_id, 'feedback', feedback_id, user_id, payload, now)
            record_activity(db, user_id, 'feedback', request_id=request_id, now=now)
            return {'id': feedback_id, 'status': 'received', 'notification_status': 'pending'}

    def browser_event(self, user, action, request_id):
        user_id, name = _user_identity(user)
        if action not in BROWSER_ACTIONS:
            raise CommunityError('操作类型无效')
        _request_id(request_id)
        now = self.clock()
        with self.tx() as db:
            existing = db.execute('SELECT 1 FROM activity_requests WHERE request_hash=?',
                                  (_activity_receipt(user_id, action, request_id),)).fetchone()
            if existing:
                return {'status': 'duplicate', 'notification_id': None}
            count = db.execute('''SELECT COUNT(*) FROM activity_events WHERE user_id=?
                AND function IN ('read_aloud','support_view','support_save') AND created_at>?''',
                (user_id, now - 60)).fetchone()[0]
            if count >= 60:
                raise CommunityError('操作较频繁，请稍后再试', 429)
            record_activity(db, user_id, action, request_id=request_id, now=now)
            notice_id = None
            if action in ACTION_LABELS:
                last = db.execute('''SELECT last_notified_at FROM support_notifications
                    WHERE user_id=? AND action=?''', (user_id, action)).fetchone()
                if last is None or now - last[0] >= SUPPORT_INTERVAL:
                    notice_id = 'NT-' + secrets.token_hex(8)
                    payload = _card(notice_id, '飞花令 · 支持页面操作', [
                        f'用户：{name}\n用户 ID：{user_id}', f'操作时间：{_timestamp(now)}',
                        f'操作：{ACTION_LABELS[action]}', '操作提醒，不代表实际付款',
                        '无法确认长按保存或下载是否完成。',
                    ])
                    self._enqueue(db, notice_id, 'support', action, user_id, payload, now)
                    db.execute('''INSERT INTO support_notifications VALUES(?,?,?)
                        ON CONFLICT(user_id,action) DO UPDATE SET last_notified_at=excluded.last_notified_at''',
                        (user_id, action, now))
            return {'status': 'received', 'notification_id': notice_id}

    def purge_activity(self, now=None):
        """Keep the compact request ledger so an old retry cannot recreate activity."""
        now = self.clock() if now is None else now
        with self.tx() as db:
            removed = db.execute('DELETE FROM activity_events WHERE created_at<?',
                                 (now - RETENTION_SECONDS,)).rowcount
            db.execute('DELETE FROM support_notifications WHERE last_notified_at<?',
                       (now - RETENTION_SECONDS,))
            # Support cards are copies of usage details, including unsent cards.
            # Expire them under the same 90-day rule. A live sender keeps its
            # lease; once it finishes or its lease expires it can be removed.
            db.execute('''DELETE FROM notification_jobs WHERE kind='support' AND created_at<?
                AND (status!='sending' OR lease_until<=?)''', (now - RETENTION_SECONDS, now))
            return removed

    @staticmethod
    def _retry_audit(db, actor_id, notification_id, result, now):
        db.execute('''INSERT INTO admin_audit(id,actor_id,target_id,action,result,created_at)
            VALUES(?,?,?,?,?,?)''', ('a_' + secrets.token_hex(12), actor_id, notification_id,
                                   'notification.retry', result, now))

    def retry_notification(self, notification_id, actor_id=None):
        """An API retry supplies its admin ID so reset and audit commit together."""
        now = self.clock()
        try:
            with self.tx() as db:
                if actor_id is not None:
                    actor = db.execute('SELECT role,disabled,must_change_password FROM users WHERE id=?',
                                       (actor_id,)).fetchone()
                    if not actor or actor['role'] != 'admin' or actor['disabled'] or actor['must_change_password']:
                        raise CommunityError('需要管理员权限', 403)
                row = db.execute('SELECT status FROM notification_jobs WHERE id=?', (notification_id,)).fetchone()
                if not row:
                    raise CommunityError('通知不存在', 404)
                if row['status'] not in ('failed', 'retrying'):
                    raise CommunityError('仅可重试失败或重试中的通知', 409)
                db.execute('''UPDATE notification_jobs SET status='pending',attempts=0,next_attempt_at=?,
                    lease_until=NULL,lease_token=NULL,last_error=NULL,updated_at=? WHERE id=?''',
                    (now, now, notification_id))
                if actor_id is not None:
                    self._retry_audit(db, actor_id, notification_id, 'success', now)
                return {'id': notification_id, 'status': 'pending'}
        except CommunityError:
            if actor_id is not None:
                try:
                    with self.tx() as db:
                        self._retry_audit(db, actor_id, notification_id, 'failed', now)
                except sqlite3.Error:
                    # Preserve the original authorization/state error if the
                    # database is temporarily unable to append its failure audit.
                    pass
            raise

    @staticmethod
    def _send_http(payload):
        webhook = os.environ.get('FEIHUA_FEISHU_WEBHOOK', '').strip()
        parsed = urllib.parse.urlsplit(webhook)
        if (parsed.scheme != 'https' or parsed.hostname != 'open.feishu.cn'
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.path.startswith('/open-apis/bot/v2/hook/') or parsed.query or parsed.fragment):
            raise ValueError('notification_configuration')
        secret = os.environ.get('FEIHUA_FEISHU_SECRET', '')
        if secret:
            timestamp = str(int(time.time()))
            sign = base64.b64encode(hmac.new(f'{timestamp}\n{secret}'.encode(), b'', hashlib.sha256).digest()).decode()
            payload = {**payload, 'timestamp': timestamp, 'sign': sign}
        req = urllib.request.Request(webhook, json.dumps(payload, ensure_ascii=False).encode(),
                                     {'Content-Type': 'application/json'}, method='POST')
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=8) as response:
            if not 200 <= response.status < 300:
                raise ValueError('notification_http_status')
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError('notification_response_size')
            return json.loads(body)

    def process_once(self, sender=None, now=None):
        """Send at most one due job; safe for concurrent workers and restart recovery.

        An injected sender receives the JSON payload and returns Feishu's response
        dict. Tests must inject it; missing webhook leaves all jobs queued.
        """
        sender = sender or self.sender
        if sender is None and os.environ.get('FEIHUA_FEISHU_WEBHOOK', '').strip():
            sender = self._send_http
        explicit_now = now
        now = self.clock() if now is None else now
        lease_token = secrets.token_hex(16)
        with self.tx() as db:
            db.execute('''UPDATE notification_jobs SET
                status=CASE WHEN attempts>=? THEN 'failed' ELSE 'retrying' END,lease_until=NULL,
                lease_token=NULL,next_attempt_at=?,updated_at=?,last_error='delivery_interrupted'
                WHERE status='sending' AND lease_until<=?''', (MAX_ATTEMPTS, now, now, now))
            if sender is None:
                return False
            if db.execute('SELECT next_send_at FROM notification_rate_state WHERE id=1').fetchone()[0] > now:
                return False
            row = db.execute('''SELECT * FROM notification_jobs WHERE status IN ('pending','retrying')
                AND next_attempt_at<=? AND (kind!='support' OR created_at>=?)
                ORDER BY next_attempt_at,created_at,id LIMIT 1''', (now, now - RETENTION_SECONDS)).fetchone()
            if row is None:
                return False
            db.execute('''UPDATE notification_jobs SET status='sending',attempts=attempts+1,
                lease_until=?,lease_token=?,updated_at=? WHERE id=?''',
                (now + LEASE_SECONDS, lease_token, now, row['id']))
            db.execute('UPDATE notification_rate_state SET next_send_at=? WHERE id=1', (now + SEND_INTERVAL,))
        failure = None
        try:
            response = sender(json.loads(row['payload']))
            # Both API variants have been used by custom Feishu bots. Any present
            # status must indicate success; HTTP 200 alone does not confirm it.
            codes = [response[k] for k in ('code', 'StatusCode') if k in response] if isinstance(response, dict) else []
            if not codes or any(type(code) is not int or code != 0 for code in codes):
                failure = 'platform_rejected'
        except (TimeoutError, urllib.error.URLError):
            failure = 'delivery_timeout_or_network'
        except Exception:
            # Never persist raw exceptions: providers can include the webhook URL
            # or response text containing configuration and user content.
            failure = 'delivery_failed'
        finished = self.clock() if explicit_now is None else now
        attempts = row['attempts'] + 1
        with self.tx() as db:
            if failure is None:
                db.execute('''UPDATE notification_jobs SET status='sent',sent_at=?,updated_at=?,
                    lease_until=NULL,lease_token=NULL,last_error=NULL
                    WHERE id=? AND status='sending' AND lease_token=?''',
                    (finished, finished, row['id'], lease_token))
            else:
                status = 'failed' if attempts >= MAX_ATTEMPTS else 'retrying'
                delay = min(3600, 15 * (2 ** (attempts - 1)))
                db.execute('''UPDATE notification_jobs SET status=?,next_attempt_at=?,updated_at=?,
                    lease_until=NULL,lease_token=NULL,last_error=?
                    WHERE id=? AND status='sending' AND lease_token=?''',
                    (status, finished + delay, finished, failure, row['id'], lease_token))
        return True

    async def run_notifications(self, stop_event):
        """Lifespan worker; cancellation never erases a claimed job's lease."""
        last_purge = 0
        while not stop_event.is_set():
            try:
                if self.clock() - last_purge >= 3600:
                    await asyncio.to_thread(self.purge_activity)
                    last_purge = self.clock()
                await asyncio.to_thread(self.process_once)
            except Exception:
                # A transient database or delivery problem must not take down
                # feedback submission or free gameplay. Leases recover work.
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
