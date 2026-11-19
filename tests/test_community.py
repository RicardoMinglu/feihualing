import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.community import Community, CommunityError, MAX_ATTEMPTS, RETENTION_SECONDS, record_activity


USER = {'id': 'user-one', 'username': None, 'display_name': '游客一'}


@pytest.fixture
def community(tmp_path, monkeypatch):
    monkeypatch.delenv('FEIHUA_FEISHU_WEBHOOK', raising=False)
    monkeypatch.delenv('FEIHUA_FEISHU_SECRET', raising=False)
    return Community(tmp_path / 'business.sqlite3', clock=lambda: 1_800_000_000.0)


def rows(community, table):
    with community.tx() as db:
        return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


def feedback(community, key='feedback-001', body='这里有一条测试反馈'):
    return community.submit_feedback(USER, 'suggestion', body, 'test@example.com', key)


def test_feedback_persisted_before_notification_and_atomic(community, monkeypatch):
    result = feedback(community)
    assert result['status'] == 'received'
    assert result['notification_status'] == 'pending'
    assert len(rows(community, 'feedback')) == 1
    assert len(rows(community, 'notification_jobs')) == 1
    assert rows(community, 'activity_events')[0]['function'] == 'feedback'
    assert not community.process_once()  # No configured webhook does not consume attempts.
    assert rows(community, 'notification_jobs')[0]['attempts'] == 0
    monkeypatch.setattr(community, '_enqueue', lambda *args: (_ for _ in ()).throw(RuntimeError('database error')))
    with pytest.raises(RuntimeError):
        feedback(community, 'feedback-002')
    assert len(rows(community, 'feedback')) == 1
    assert len(rows(community, 'activity_events')) == 1


def test_feedback_replays_and_rejects_payload_changes(community):
    result = feedback(community)
    assert feedback(community) == result
    with pytest.raises(CommunityError) as exc:
        feedback(community, body='不同的反馈文本')
    assert exc.value.status == 409
    assert len(rows(community, 'notification_jobs')) == 1
    assert len(rows(community, 'activity_events')) == 1


def test_concurrent_feedback_retries_only_create_one(community):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: feedback(community), range(12)))
    assert len({result['id'] for result in results}) == 1
    assert len(rows(community, 'feedback')) == 1
    assert len(rows(community, 'notification_jobs')) == 1


def test_card_has_fixed_ids_plain_text_user_input_and_no_payment_claim(community):
    body = '<at user_id="all">所有人</at> **粗体** https://example.invalid'
    result = feedback(community, body=body)
    job = rows(community, 'notification_jobs')[0]
    payload = json.loads(job['payload'])
    content = payload['card']['elements']
    assert all(element['text']['tag'] == 'plain_text' for element in content)
    text = '\n'.join(element['text']['content'] for element in content)
    assert body in text and result['id'] in text and job['id'] in text
    community.browser_event(USER, 'support_save', 'browser-001')
    card = json.loads(rows(community, 'notification_jobs')[1]['payload'])
    texts = '\n'.join(item['text']['content'] for item in card['card']['elements'])
    assert '点击保存赞赏码' in texts and '操作提醒，不代表实际付款' in texts
    assert '到账成功' not in texts and '金额' not in texts


def test_support_each_action_has_own_30_minute_window_and_idempotence(community):
    start = community.clock()
    one = community.browser_event(USER, 'support_view', 'browser-001')
    assert one['notification_id']
    assert community.browser_event(USER, 'support_view', 'browser-001')['status'] == 'duplicate'
    assert community.browser_event(USER, 'support_view', 'browser-002')['notification_id'] is None
    assert community.browser_event(USER, 'support_save', 'browser-003')['notification_id']
    assert len(rows(community, 'activity_events')) == 3
    assert len(rows(community, 'notification_jobs')) == 2
    community.clock = lambda: start + 1799
    assert community.browser_event(USER, 'support_view', 'browser-004')['notification_id'] is None
    community.clock = lambda: start + 1800
    assert community.browser_event(USER, 'support_view', 'browser-005')['notification_id']
    assert len(rows(community, 'activity_events')) == 5
    assert len(rows(community, 'notification_jobs')) == 3


def test_browser_allowlist_and_user_rate_limit(community):
    for action in ('feedback', 'visit', 'paid', 'game_answer'):
        with pytest.raises(CommunityError):
            community.browser_event(USER, action, 'browser-001')
    for index in range(60):
        community.browser_event(USER, 'read_aloud', f'browser-{index:03d}')
    assert community.browser_event(USER, 'read_aloud', 'browser-000')['status'] == 'duplicate'
    with pytest.raises(CommunityError) as exc:
        community.browser_event(USER, 'read_aloud', 'browser-100')
    assert exc.value.status == 429
    assert len(rows(community, 'activity_events')) == 60


def test_feedback_rate_limit_does_not_count_replays(community):
    for index in range(5):
        feedback(community, f'feedback-{index:03d}')
    assert feedback(community, 'feedback-000')['status'] == 'received'
    with pytest.raises(CommunityError) as exc:
        feedback(community, 'feedback-006')
    assert exc.value.status == 429


def test_retention_preserves_replay_protection_other_records_and_users(community):
    start = community.clock()
    with community.tx() as db:
        db.execute('CREATE TABLE users(id TEXT PRIMARY KEY,last_active_at REAL)')
        db.execute('INSERT INTO users VALUES(?,NULL)', (USER['id'],))
        record_activity(db, USER['id'], 'game_create', request_id='game-001', now=start)
    feedback(community)
    assert rows(community, 'users')[0]['last_active_at'] == start
    assert all(set(row) == {'request_hash'} and len(row['request_hash']) == 64
               for row in rows(community, 'activity_requests'))
    community.clock = lambda: start + RETENTION_SECONDS + 1
    assert community.purge_activity() == 2
    assert not community.record(USER['id'], 'game_create', request_id='game-001')
    assert len(rows(community, 'activity_events')) == 0
    assert len(rows(community, 'feedback')) == 1
    assert len(rows(community, 'notification_jobs')) == 1
    assert rows(community, 'users')[0]['last_active_at'] == start


def test_legacy_activity_receipts_migrate_without_readable_details(tmp_path):
    path = tmp_path / 'business.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE activity_requests(user_id TEXT,function TEXT,
            request_id TEXT,created_at REAL,PRIMARY KEY(user_id,function,request_id))''')
        db.execute('INSERT INTO activity_requests VALUES(?,?,?,?)', ('legacy-user', 'visit', 'legacy-request', 1))
    community = Community(path)
    assert not community.record('legacy-user', 'visit', request_id='legacy-request')
    assert len(rows(community, 'activity_events')) == 0
    assert set(rows(community, 'activity_requests')[0]) == {'request_hash'}
    repeated = Community(path)
    assert not repeated.record('legacy-user', 'visit', request_id='legacy-request')


def test_support_copies_expire_with_details_but_feedback_remains(community):
    start = community.clock()
    view = community.browser_event(USER, 'support_view', 'support-view-001')
    community.browser_event(USER, 'support_save', 'support-save-001')
    feedback(community)
    community.clock = lambda: start + RETENTION_SECONDS + 1
    with community.tx() as db:
        db.execute("UPDATE notification_jobs SET status='sending',lease_until=? WHERE id=?",
                   (community.clock() + 30, view['notification_id']))
    assert community.purge_activity() == 3
    remaining = rows(community, 'notification_jobs')
    assert len(remaining) == 2  # Feedback retained; the live sender can finish.
    assert len(rows(community, 'support_notifications')) == 0
    assert community.browser_event(USER, 'support_save', 'support-save-001')['status'] == 'duplicate'
    assert len(rows(community, 'notification_jobs')) == 2
    assert len(rows(community, 'activity_events')) == 0
    community.clock = lambda: start + RETENTION_SECONDS + 31
    assert community.purge_activity() == 0
    assert [row['kind'] for row in rows(community, 'notification_jobs')] == ['feedback']
    assert len(rows(community, 'feedback')) == 1


def test_worker_does_not_claim_expired_support_notice_before_cleanup(community):
    start = community.clock()
    community.browser_event(USER, 'support_view', 'support-view-001')
    community.clock = lambda: start + RETENTION_SECONDS + 1
    assert not community.process_once(lambda _: pytest.fail('expired support notice sent'))


@pytest.mark.parametrize('response', [{'code': 0}, {'StatusCode': 0}, {'code': 0, 'StatusCode': 0}])
def test_worker_accepts_success_variants_and_no_resend(community, response):
    feedback(community)
    sent = []
    assert community.process_once(lambda payload: sent.append(payload) or response)
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'sent' and job['attempts'] == 1
    assert not community.process_once(lambda _: pytest.fail('already sent'))
    with pytest.raises(CommunityError) as exc:
        community.retry_notification(job['id'])
    assert exc.value.status == 409


@pytest.mark.parametrize('response', [{}, {'code': 1}, {'StatusCode': 1}, {'code': 0, 'StatusCode': 1}, {'code': False}, {'code': '0'}, []])
def test_http_success_without_business_success_retries(community, response):
    feedback(community)
    assert community.process_once(lambda _: response)
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'retrying'
    assert job['last_error'] == 'platform_rejected'


def test_failure_backoff_restart_recovery_fixed_notification_id(community):
    feedback(community)
    initial = community.clock()
    sent = []
    def timeout(payload):
        sent.append(payload)
        raise TimeoutError('https://secret.example/webhook-token should not be persisted')
    assert community.process_once(timeout)
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'retrying' and job['next_attempt_at'] == initial + 15
    assert 'secret' not in job['last_error']
    restarted = Community(community.path, clock=lambda: initial + 14)
    assert not restarted.process_once(lambda _: pytest.fail('backoff not elapsed'))
    restarted.clock = lambda: initial + 15
    assert restarted.process_once(lambda payload: sent.append(payload) or {'code': 0})
    assert sent[0] == sent[1]
    assert rows(restarted, 'notification_jobs')[0]['status'] == 'sent'


def test_interrupted_lease_recovers_after_restart(community):
    feedback(community)
    now = community.clock()
    with community.tx() as db:
        db.execute("UPDATE notification_jobs SET status='sending',lease_until=?,lease_token='old',attempts=1", (now + 30,))
    assert not community.process_once(lambda _: pytest.fail('lease still held'))
    community.clock = lambda: now + 30
    assert community.process_once(lambda _: {'code': 0})
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'sent' and job['attempts'] == 2 and job['lease_token'] is None


def test_repeated_process_interruptions_eventually_require_manual_retry(community):
    feedback(community)
    with community.tx() as db:
        db.execute("UPDATE notification_jobs SET status='sending',lease_until=?,attempts=?",
                   (community.clock() - 1, MAX_ATTEMPTS))
    assert not community.process_once(lambda _: pytest.fail('attempts exhausted'))
    assert rows(community, 'notification_jobs')[0]['status'] == 'failed'


def test_missing_webhook_recovers_stale_lease_without_consuming_attempts(community):
    feedback(community)
    with community.tx() as db:
        db.execute("UPDATE notification_jobs SET status='sending',lease_until=?,attempts=1",
                   (community.clock() - 1,))
    assert not community.process_once()
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'retrying' and job['attempts'] == 1 and job['lease_until'] is None


def test_late_sender_cannot_overwrite_new_worker_completion(community):
    feedback(community)
    now = community.clock()
    def stale_sender(payload):
        second = Community(community.path, clock=lambda: now + 31)
        assert second.process_once(lambda _: {'code': 0})
        raise TimeoutError('old sender timed out after lease recovered')
    assert community.process_once(stale_sender)
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'sent' and job['attempts'] == 2


def test_global_send_rate_shared_between_workers(community):
    feedback(community, 'feedback-001')
    feedback(community, 'feedback-002')
    assert community.process_once(lambda _: {'code': 0})
    other = Community(community.path, clock=community.clock)
    assert not other.process_once(lambda _: pytest.fail('rate limit bypassed'))
    now = community.clock()
    other.clock = lambda: now + 1
    assert other.process_once(lambda _: {'code': 0})


def test_attempt_exhaustion_and_manual_retry(community):
    feedback(community)
    for attempt in range(MAX_ATTEMPTS):
        current = rows(community, 'notification_jobs')[0]
        community.clock = lambda when=current['next_attempt_at']: when
        assert community.process_once(lambda _: {'code': 19024, 'msg': 'secret response'})
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'failed' and job['attempts'] == MAX_ATTEMPTS
    assert job['last_error'] == 'platform_rejected'
    assert not community.process_once(lambda _: pytest.fail('failed job should stay failed'))
    assert community.retry_notification(job['id'])['status'] == 'pending'
    community.clock = lambda: job['next_attempt_at'] + 1
    assert community.process_once(lambda _: {'code': 0})
    assert rows(community, 'notification_jobs')[0]['status'] == 'sent'


def test_sending_cannot_be_manually_retried(community):
    feedback(community)
    with community.tx() as db:
        db.execute("UPDATE notification_jobs SET status='sending',lease_until=?", (community.clock() + 30,))
    with pytest.raises(CommunityError) as exc:
        community.retry_notification(rows(community, 'notification_jobs')[0]['id'])
    assert exc.value.status == 409


def admin_tables(community):
    with community.tx() as db:
        db.execute('''CREATE TABLE users(id TEXT PRIMARY KEY,last_active_at REAL,
            role TEXT,disabled INTEGER,must_change_password INTEGER)''')
        db.execute("INSERT INTO users VALUES('admin',0,'admin',0,0)")
        db.execute("INSERT INTO users VALUES('ordinary',0,'user',0,0)")
        db.execute('''CREATE TABLE admin_audit(id TEXT PRIMARY KEY,actor_id TEXT,target_id TEXT,
            action TEXT,result TEXT,created_at REAL)''')


def test_manual_retry_checks_admin_and_audits_success_and_failure(community):
    admin_tables(community)
    feedback(community)
    community.process_once(lambda _: {'code': 1})
    job = rows(community, 'notification_jobs')[0]
    with pytest.raises(CommunityError) as exc:
        community.retry_notification(job['id'], actor_id='ordinary')
    assert exc.value.status == 403
    assert rows(community, 'notification_jobs')[0]['status'] == 'retrying'
    assert rows(community, 'admin_audit')[0]['result'] == 'failed'
    community.retry_notification(job['id'], actor_id='admin')
    audit = rows(community, 'admin_audit')[1]
    assert audit['actor_id'] == 'admin' and audit['target_id'] == job['id']
    assert audit['action'] == 'notification.retry' and audit['result'] == 'success'
    with pytest.raises(CommunityError):
        community.retry_notification(job['id'], actor_id='admin')
    assert rows(community, 'admin_audit')[2]['result'] == 'failed'


def test_retry_audit_failure_rolls_back_notification_change(community, monkeypatch):
    admin_tables(community)
    feedback(community)
    community.process_once(lambda _: {'code': 1})
    job = rows(community, 'notification_jobs')[0]
    def fail_audit(*args):
        raise sqlite3.OperationalError('audit not writable')
    monkeypatch.setattr(community, '_retry_audit', fail_audit)
    with pytest.raises(sqlite3.OperationalError):
        community.retry_notification(job['id'], actor_id='admin')
    unchanged = rows(community, 'notification_jobs')[0]
    assert unchanged['status'] == 'retrying' and unchanged['attempts'] == job['attempts']
    # State errors are preserved even if their separate failure audit cannot commit.
    with pytest.raises(CommunityError) as exc:
        community.retry_notification('NT-missing', actor_id='admin')
    assert exc.value.status == 404


def test_worker_stops_without_outbound_request(community):
    feedback(community)
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(community.run_notifications(stop))
        await asyncio.sleep(0.03)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    asyncio.run(run())
    assert rows(community, 'notification_jobs')[0]['attempts'] == 0


def test_http_sender_signature_and_no_redirect(monkeypatch):
    import base64
    import hashlib
    import hmac
    import app.community as module
    monkeypatch.setenv('FEIHUA_FEISHU_WEBHOOK', 'https://open.feishu.cn/open-apis/bot/v2/hook/test-only')
    monkeypatch.setenv('FEIHUA_FEISHU_SECRET', 'test-only-signing-secret')
    monkeypatch.setattr(module.time, 'time', lambda: 1800000000)
    requests = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{"code":0}'
    class Opener:
        def open(self, req, timeout):
            requests.append((req, timeout))
            return Response()
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda handler: Opener())
    assert Community._send_http({'msg_type': 'interactive'}) == {'code': 0}
    request, timeout = requests[0]
    payload = json.loads(request.data)
    expected = base64.b64encode(hmac.new(b'1800000000\ntest-only-signing-secret', b'', hashlib.sha256).digest()).decode()
    assert payload['timestamp'] == '1800000000' and payload['sign'] == expected
    assert timeout == 8
    assert module._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid') is None


@pytest.mark.parametrize('webhook', [
    'http://open.feishu.cn/open-apis/bot/v2/hook/private-token',
    'https://open.feishu.cn.evil.invalid/open-apis/bot/v2/hook/private-token',
    'https://open.feishu.cn@evil.invalid/open-apis/bot/v2/hook/private-token',
    'https://name:password@open.feishu.cn/open-apis/bot/v2/hook/private-token',
    'https://open.feishu.cn:4430/open-apis/bot/v2/hook/private-token',
    'https://open.feishu.cn/not-a-bot/private-token',
    'https://open.feishu.cn/open-apis/bot/v2/hook/private-token?token=secret',
    'https://open.feishu.cn/open-apis/bot/v2/hook/private-token#secret',
])
def test_bad_webhook_never_connects_or_persists_secret(community, monkeypatch, webhook):
    import app.community as module
    feedback(community)
    monkeypatch.setenv('FEIHUA_FEISHU_WEBHOOK', webhook)
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *args: pytest.fail('invalid URL connected'))
    assert community.process_once()
    job = rows(community, 'notification_jobs')[0]
    assert job['status'] == 'retrying' and job['last_error'] == 'delivery_failed'
    assert 'private-token' not in json.dumps(job)
