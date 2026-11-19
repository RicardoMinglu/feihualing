"""HTTP-level identity, permission, activity and durable-feedback integration."""
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, security
from app.accounts import Accounts
from app.community import Community, record_activity
from app.games import Games


@pytest.fixture
def site(tmp_path, monkeypatch):
    games = Games(main.corpus, tmp_path / 'community-api.sqlite3')
    accounts = Accounts(games.path)
    community = Community(games.path)
    games.event_recorder = record_activity
    monkeypatch.setattr(main, 'games', games)
    monkeypatch.setattr(main, 'accounts', accounts)
    monkeypatch.setattr(main, 'community', community)
    monkeypatch.setattr(security, 'limits', security.RateLimits())
    monkeypatch.delenv('FEIHUA_FEISHU_WEBHOOK', raising=False)
    clients = []

    def browser():
        # No lifespan worker: queue delivery is driven explicitly by a mock below.
        client = TestClient(main.app)
        clients.append(client)
        return client

    fixture = SimpleNamespace(client=browser(), browser=browser, games=games,
                              accounts=accounts, community=community, path=games.path)
    yield fixture
    for client in clients:
        client.close()


def bootstrap(client):
    response = client.post('/api/auth/guest', json={})
    assert response.status_code == 200, response.text
    return response.json()


def create_game(client, request_id='create-integration-1'):
    response = client.post('/api/games', json={'word': '月', 'difficulty': 'easy', 'request_id': request_id})
    assert response.status_code == 200, response.text
    return response.json()['game']


def register(client, username='poet', password='original-password'):
    response = client.post('/api/auth/register', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    return response.json()


def admin_browser(site):
    user = site.accounts.create_admin('operator', 'operator-password')
    client = site.browser()
    assert client.post('/api/auth/login', json={'username': 'operator', 'password': 'operator-password'}).status_code == 200
    return client, user


def scalar(site, query, params=()):
    with sqlite3.connect(site.path) as db:
        return db.execute(query, params).fetchone()[0]


def test_no_identity_for_health_assets_html_or_unauthenticated_polling(site):
    client = site.client
    for url in ('/', '/health', '/api/config', '/assets/live.js', '/admin'):
        assert client.get(url).status_code == 200
    for url in ('/api/me', '/api/games', '/api/games/missing', '/api/games/missing/messages'):
        assert client.get(url).status_code == 401
    assert not client.cookies
    assert scalar(site, 'SELECT count(*) FROM users') == 0
    assert scalar(site, 'SELECT count(*) FROM activity_events') == 0


def test_bootstrap_refresh_shared_tab_and_cookie_clearing_isolation(site):
    client = site.client
    first = bootstrap(client)
    token = client.cookies.get(main.COOKIE)
    assert token and token != first['id']
    assert bootstrap(client)['id'] == first['id']
    assert client.cookies.get(main.COOKIE) == token
    assert client.get('/api/me').json()['id'] == first['id']
    other_tab = site.browser()
    other_tab.cookies.set(main.COOKIE, token)
    assert bootstrap(other_tab)['id'] == first['id']
    game = create_game(client)
    client.cookies.clear()
    assert client.get('/api/games/' + game['id']).status_code == 401
    second = bootstrap(client)
    assert second['id'] != first['id']
    assert client.get('/api/games').json()['items'] == []
    assert client.get('/api/games/' + game['id']).status_code == 404
    assert other_tab.get('/api/games/' + game['id']).status_code == 200
    assert scalar(site, 'SELECT count(*) FROM users') == 2
    assert scalar(site, 'SELECT token_hash FROM sessions WHERE user_id=?', (first['id'],)) == hashlib.sha256(token.encode()).hexdigest()


def test_upgrade_keeps_history_stage_and_never_accepts_public_identity(site):
    client = site.client
    original = bootstrap(client)
    original_token = client.cookies.get(main.COOKIE)
    game = create_game(client)
    assert client.patch('/api/me', json={'education_stage': 'primary'}).status_code == 200
    upgraded = register(client)
    assert upgraded['id'] == original['id'] and upgraded['kind'] == 'account'
    assert upgraded['education_stage'] == 'primary' and upgraded['has_records']
    assert client.cookies.get(main.COOKIE) != original_token
    assert site.accounts.resolve(original_token) is None
    assert client.get('/api/games/' + game['id']).status_code == 200
    attacker = site.browser()
    attacker.cookies.set(main.COOKIE, original['id'])
    attacker.cookies.set('feihua_visitor', original['id'])
    assert attacker.get('/api/me').status_code == 401
    assert bootstrap(attacker)['id'] != original['id']
    assert attacker.get('/api/games/' + game['id']).status_code == 404
    assert attacker.patch('/api/me', json={'education_stage': 'all', 'role': 'admin'}).status_code == 422


def test_legacy_cookie_migrates_existing_game_once_and_loses_access_on_upgrade(site, monkeypatch):
    legacy = 'c' * 64
    site.games.event_recorder = None
    game = site.games.create(legacy, '月', 'easy', 'legacy-created-game')
    site.games.set_profile(legacy, 'junior')
    site.games.event_recorder = record_activity
    migrated = Accounts(site.path)
    monkeypatch.setattr(main, 'accounts', migrated)
    client = site.client
    client.cookies.set('feihua_visitor', legacy)
    user = bootstrap(client)
    assert user['id'] != legacy and user['education_stage'] == 'junior'
    assert client.get('/api/games/' + game['id']).status_code == 200
    assert scalar(site, 'SELECT owner FROM game_summaries WHERE game_id=?', (game['id'],)) == user['id']
    register(client)
    old_browser = site.browser()
    old_browser.cookies.set('feihua_visitor', legacy)
    assert bootstrap(old_browser)['id'] != user['id']
    assert old_browser.get('/api/games/' + game['id']).status_code == 404
    Accounts(site.path)
    assert scalar(site, 'SELECT count(*) FROM legacy_identities') == 1


def test_login_requires_guest_switch_confirmation_and_does_not_merge(site):
    account_client = site.browser()
    account_id = bootstrap(account_client)['id']
    register(account_client)
    guest_id = bootstrap(site.client)['id']
    guest_game = create_game(site.client)
    payload = {'username': 'poet', 'password': 'original-password'}
    assert site.client.post('/api/auth/login', json=payload).status_code == 409
    assert site.client.get('/api/me').json()['id'] == guest_id
    response = site.client.post('/api/auth/login', json={**payload, 'confirm_switch': True})
    assert response.status_code == 200 and response.json()['id'] == account_id
    assert site.client.get('/api/games/' + guest_game['id']).status_code == 404
    assert scalar(site, 'SELECT owner FROM games WHERE id=?', (guest_game['id'],)) == guest_id


def test_cross_device_password_change_revokes_sessions_and_logout(site):
    user = bootstrap(site.client)
    register(site.client)
    device = site.browser()
    assert device.post('/api/auth/login', json={'username': 'poet', 'password': 'original-password'}).json()['id'] == user['id']
    assert site.client.get('/api/me').status_code == 200
    assert site.client.post('/api/auth/credentials', json={'current_password': 'wrong-password', 'password': 'changed-password'}).status_code == 403
    changed = site.client.post('/api/auth/credentials', json={'current_password': 'original-password', 'username': '新诗人', 'password': 'changed-password'})
    assert changed.status_code == 200 and changed.json()['username'] == '新诗人'
    assert device.get('/api/me').status_code == 401
    assert device.post('/api/auth/login', json={'username': 'poet', 'password': 'original-password'}).status_code == 401
    assert device.post('/api/auth/login', json={'username': '新诗人', 'password': 'changed-password'}).status_code == 200
    assert device.post('/api/auth/logout', json={}).status_code == 200
    assert device.get('/api/me').status_code == 401
    assert site.client.get('/api/me').status_code == 200


def test_admin_permissions_disable_reset_and_mandatory_password_change(site):
    client = site.client
    user = bootstrap(client)
    register(client)
    for url in ('/api/admin/overview', '/api/admin/users', '/api/admin/feedback', '/api/admin/notifications', '/api/admin/audit'):
        assert client.get(url).status_code == 403
    assert client.post('/api/admin/users/' + user['id'] + '/disabled', json={'disabled': True}).status_code == 403
    admin, admin_user = admin_browser(site)
    assert admin.post('/api/admin/users/' + admin_user['id'] + '/disabled', json={'disabled': True}).status_code == 403
    assert admin.post('/api/admin/users/' + user['id'] + '/disabled', json={'disabled': True}).status_code == 200
    assert client.get('/api/me').status_code == 401
    assert client.post('/api/auth/guest', json={}).status_code == 403
    assert client.post('/api/auth/login', json={'username': 'poet', 'password': 'original-password'}).status_code == 403
    assert admin.post('/api/admin/users/' + user['id'] + '/disabled', json={'disabled': False}).status_code == 200
    assert client.get('/api/me').status_code == 401
    temp = admin.post('/api/admin/users/' + user['id'] + '/reset-password', json={}).json()['temporary_password']
    assert client.post('/api/auth/login', json={'username': 'poet', 'password': 'original-password'}).status_code == 401
    logged_in = client.post('/api/auth/login', json={'username': 'poet', 'password': temp})
    assert logged_in.status_code == 200 and logged_in.json()['must_change_password']
    assert client.get('/api/me').json()['must_change_password']
    assert client.get('/api/search', params={'word': '月'}).status_code == 403
    assert client.post('/api/games', json={'word': '月', 'difficulty': 'easy', 'request_id': 'temporary-restricted'}).status_code == 403
    another = site.browser()
    assert another.post('/api/auth/login', json={'username': 'poet', 'password': temp}).status_code == 401
    assert client.post('/api/auth/credentials', json={'current_password': temp, 'username': '新账号'}).status_code == 400
    changed = client.post('/api/auth/credentials', json={'current_password': temp, 'password': 'replacement-password'})
    assert changed.status_code == 200 and not changed.json()['must_change_password']
    assert client.get('/api/search', params={'word': '月'}).status_code == 200
    audit = admin.get('/api/admin/audit').json()['items']
    assert any(item['action'] == 'reset_password' and item['result'] == 'success' for item in audit)
    assert temp not in json.dumps(audit)
    user_json = admin.get('/api/admin/users').text
    assert 'password_hash' not in user_json and 'token_hash' not in user_json


def test_csrf_checks_and_server_owned_browser_event_fields(site):
    client = site.client
    bootstrap(client)
    payload = {'request_id': 'visit-csrf-safe'}
    assert client.post('/api/visits', json=payload, headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert client.post('/api/visits', json=payload, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.post('/api/visits', content=json.dumps(payload), headers={'Content-Type': 'text/plain'}).status_code == 415
    assert client.post('/api/visits', json=payload, headers={'Origin': 'http://testserver'}).status_code == 200
    for action in ('game_answer', 'paid', 'payment_success'):
        assert client.post('/api/events', json={'action': action, 'request_id': 'invalid-action-id'}).status_code == 422
    assert client.post('/api/events', json={'action': 'support_view', 'request_id': 'forged-browser-id', 'user_id': 'someone-else', 'created_at': 1}).status_code == 422
    assert scalar(site, 'SELECT count(*) FROM activity_events') == 1


def test_activity_is_idempotent_and_polling_does_not_mark_users_active(site):
    client = site.client
    user = bootstrap(client)
    for _ in range(2):
        assert client.post('/api/visits', json={'request_id': 'initial-page-visit'}).status_code == 200
    game = create_game(client)
    assert create_game(client)['id'] == game['id']
    path = '/api/games/' + game['id']
    answer = {'text': '床前明月光', 'request_id': 'active-answer-id'}
    first = client.post(path + '/answers', json=answer)
    assert first.status_code == 200 and first.json()['verdict']['kind'] == 'accepted'
    assert client.post(path + '/answers', json=answer).status_code == 200
    for _ in range(2):
        assert client.post(path + '/hint', json={'request_id': 'active-hint-once'}).status_code == 200
    headers = {'X-Activity-Id': 'search-activity-once'}
    for _ in range(2):
        search = client.get('/api/search', params={'word': '月'}, headers=headers)
        assert search.status_code == 200
    poem_id = search.json()['items'][0]['poem_id']
    for _ in range(2):
        assert client.get('/api/poems/' + poem_id, headers={'X-Activity-Id': 'poem-view-once'}).status_code == 200
    for _ in range(2):
        assert client.post('/api/events', json={'action': 'read_aloud', 'request_id': 'read-aloud-once'}).status_code == 200
    for _ in range(2):
        assert client.post(path + '/end', json={'request_id': 'end-active-once'}).status_code == 200
    with sqlite3.connect(site.path) as db:
        counts = dict(db.execute('SELECT function,count(*) FROM activity_events GROUP BY function'))
    assert counts == {key: 1 for key in ('visit', 'game_create', 'game_answer', 'game_hint', 'game_end', 'search', 'poem_view', 'read_aloud')}
    last_active = site.accounts.get(user['id'])['last_active_at']
    for _ in range(2):
        assert client.get(path, params={'after': 0}).status_code == 200
        assert client.get(path + '/messages').status_code == 200
        assert client.get('/api/games').status_code == 200
        assert client.get('/api/me').status_code == 200
        assert client.get('/api/search', params={'word': '月'}).status_code == 200
        assert client.get('/api/poems/' + poem_id).status_code == 200
        assert bootstrap(client)['id'] == user['id']
    assert site.accounts.get(user['id'])['last_active_at'] == last_active
    assert scalar(site, 'SELECT count(*) FROM activity_events') == 8
    with sqlite3.connect(site.path) as db:
        columns = [row[1] for row in db.execute('PRAGMA table_info(activity_events)')]
        raw = json.dumps(db.execute('SELECT * FROM activity_events').fetchall(), ensure_ascii=False)
    assert 'text' not in columns and 'query' not in columns and '床前明月光' not in raw


def test_feedback_is_idempotent_atomic_and_admin_can_retry_mock_delivery(site, monkeypatch):
    client = site.client
    user = bootstrap(client)
    payload = {'kind': 'suggestion', 'body': '<at id="all">希望增加诗词收藏功能</at>', 'contact': 'parent@example.invalid', 'request_id': 'feedback-api-duplicate'}
    response = client.post('/api/feedback', json=payload)
    assert response.status_code == 200
    feedback_id = response.json()['id']
    assert client.post('/api/feedback', json=payload).json()['id'] == feedback_id
    assert client.post('/api/feedback', json={**payload, 'body': '另一条不同的反馈内容'}).status_code == 409
    assert scalar(site, 'SELECT count(*) FROM feedback') == 1
    assert scalar(site, 'SELECT count(*) FROM notification_jobs') == 1
    assert scalar(site, "SELECT count(*) FROM activity_events WHERE function='feedback'") == 1
    admin, _ = admin_browser(site)
    feedback = admin.get('/api/admin/feedback', params={'status': 'pending'}).json()
    assert feedback['total'] == 1
    item = feedback['items'][0]
    assert item['id'] == feedback_id and item['user_id'] == user['id']
    assert item['body'] == payload['body'] and item['notification_status'] == 'pending'
    notice_id = item['notification_id']
    assert admin.patch('/api/admin/feedback/' + feedback_id, json={'status': 'processed'}).status_code == 200
    assert admin.get('/api/admin/feedback', params={'status': 'pending'}).json()['total'] == 0
    assert admin.get('/api/admin/feedback', params={'status': 'processed'}).json()['total'] == 1
    assert site.community.process_once(sender=lambda card: {'code': 19021}, now=time.time() + 1)
    notices = admin.get('/api/admin/notifications', params={'status': 'retrying'}).json()
    assert notices['total'] == 1 and notices['items'][0]['last_error'] == 'platform_rejected'
    assert client.post('/api/admin/notifications/' + notice_id + '/retry', json={}).status_code == 403
    assert admin.post('/api/admin/notifications/' + notice_id + '/retry', json={}).json()['status'] == 'pending'
    cards = []
    assert site.community.process_once(sender=lambda card: cards.append(card) or {'code': 0}, now=time.time() + 3)
    assert admin.get('/api/admin/notifications', params={'status': 'sent'}).json()['total'] == 1
    assert all(element['text']['tag'] == 'plain_text' for element in cards[0]['card']['elements'])
    assert notice_id in json.dumps(cards[0])
    assert admin.post('/api/admin/notifications/' + notice_id + '/retry', json={}).status_code == 409
    enqueue = site.community._enqueue

    def cannot_enqueue(*args, **kwargs):
        raise RuntimeError('simulated persistence failure')

    monkeypatch.setattr(site.community, '_enqueue', cannot_enqueue)
    failing = TestClient(main.app, raise_server_exceptions=False)
    try:
        failing.cookies.update(client.cookies)
        assert failing.post('/api/feedback', json={**payload, 'request_id': 'atomic-rollback-feedback'}).status_code == 500
    finally:
        failing.close()
    assert scalar(site, 'SELECT count(*) FROM feedback') == 1
    assert scalar(site, 'SELECT count(*) FROM notification_jobs') == 1
    monkeypatch.setattr(site.community, '_enqueue', enqueue)
    assert client.post('/api/feedback', json={**payload, 'request_id': 'atomic-rollback-feedback'}).status_code == 200


def test_support_actions_separate_throttle_admin_filters_and_no_payment_claim(site):
    client = site.client
    guest = bootstrap(client)
    now = [time.time()]
    site.community.clock = lambda: now[0]
    bodies = [
        {'action': 'support_view', 'request_id': 'support-view-first'},
        {'action': 'support_save', 'request_id': 'support-save-first'},
        {'action': 'support_view', 'request_id': 'support-view-second'},
    ]
    responses = [client.post('/api/events', json=body).json() for body in bodies]
    assert responses[0]['notification_id'] and responses[1]['notification_id']
    assert responses[2]['notification_id'] is None
    assert client.post('/api/events', json=bodies[0]).json()['status'] == 'duplicate'
    assert scalar(site, 'SELECT count(*) FROM activity_events') == 3
    assert scalar(site, 'SELECT count(*) FROM notification_jobs') == 2
    now[0] += 1801
    assert client.post('/api/events', json={'action': 'support_view', 'request_id': 'support-view-later'}).json()['notification_id']
    with sqlite3.connect(site.path) as db:
        cards = [json.loads(row[0]) for row in db.execute('SELECT payload FROM notification_jobs')]
    text = json.dumps(cards, ensure_ascii=False)
    assert '操作提醒，不代表实际付款' in text
    assert '点击保存赞赏码' in text and '查看赞赏码' in text
    assert '到账成功' not in text and '元' not in text
    admin, _ = admin_browser(site)
    results = admin.get('/api/admin/users', params={'kind': 'guest', 'function': 'support_view', 'start': now[0] - 1}).json()
    assert results['total'] == 1 and results['items'][0]['id'] == guest['id']
    assert results['items'][0]['event_count'] == 1
    assert admin.get('/api/admin/users', params={'kind': 'account'}).json()['total'] == 0
    assert admin.get('/api/admin/users', params={'q': "%' OR 1=1 --"}).json()['total'] == 0
    timeline = admin.get('/api/admin/users/' + guest['id'] + '/events', params={'function': 'support_save'}).json()
    assert timeline['total'] == 1 and timeline['counts'] == [{'function': 'support_save', 'count': 1}]
    overview = admin.get('/api/admin/overview').json()
    assert overview['users_total'] == 1 and overview['events_total'] == 4
    for params in ({'start': 'nan'}, {'start': '2', 'end': '1'}, {'start': 'invalid-date'}):
        assert admin.get('/api/admin/users', params=params).status_code == 400


def test_forwarded_header_spoofing_does_not_bypass_ip_limits(site):
    client = site.client
    for i in range(60):
        assert client.post('/api/auth/guest', json={}, headers={'X-Forwarded-For': '192.0.2.' + str(i)}).status_code == 200
    response = client.post('/api/auth/guest', json={}, headers={'X-Forwarded-For': '198.51.100.1'})
    assert response.status_code == 429 and response.headers.get('Retry-After')
    assert scalar(site, 'SELECT count(*) FROM users') == 1
