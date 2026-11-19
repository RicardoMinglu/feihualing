"""Offline HTTP-protocol tests: no app import, database, socket, or Feishu call."""
import json
from http.cookies import SimpleCookie

import httpx
import pytest

from scripts.verify_community_deployment import verify


class Deployment:
    token = 'private-session-token-never-print-this-value'
    user_id = 'u_' + 'a' * 32
    game_id = 'b' * 32

    def __init__(self, *, broken_cookie=False, fail_detail=False):
        self.broken_cookie = broken_cookie
        self.fail_detail = fail_detail
        self.stage = 'all'
        self.created_guests = self.feedback_count = 0
        self.signed_in = True
        self.game_ended = False
        self.requests = []

    def profile(self):
        return {'id': self.user_id, 'kind': 'guest', 'role': 'user', 'education_stage': self.stage}

    def __call__(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        assert not path.startswith(('/api/events', '/api/visits', '/api/auth/register'))
        if request.method in ('POST', 'PATCH'):
            assert request.headers['content-type'] == 'application/json'
            body = json.loads(request.content)
            if request.headers['origin'] != 'https://qa.example':
                assert path == '/api/me' and body == {'education_stage': 'senior'}
                return httpx.Response(403, json={'detail': '请求来源无效'})
        cookies = SimpleCookie(request.headers.get('cookie', ''))
        authenticated = (not self.broken_cookie and self.signed_in
                         and cookies.get('feihua_session') is not None
                         and cookies['feihua_session'].value == self.token)
        if path == '/':
            return httpx.Response(200, text='<title>飞花令</title>')
        if path == '/health':
            return httpx.Response(200, json={'status': 'ok'})
        if path == '/api/config':
            return httpx.Response(200, json={'version': '1.5.0'})
        if path == '/api/auth/guest':
            if not authenticated:
                self.created_guests += 1
            return httpx.Response(200, json=self.profile(), headers={
                'set-cookie': f'feihua_session={self.token}; Path=/; Secure; HttpOnly; SameSite=lax',
            })
        if path == '/api/auth/logout':
            self.signed_in = False
            return httpx.Response(200, json={'status': 'signed_out'}, headers={
                'set-cookie': 'feihua_session=""; Max-Age=0; Path=/',
            })
        if not authenticated:
            return httpx.Response(401, json={'detail': '请刷新页面'})
        if path == '/api/me':
            if request.method == 'PATCH':
                assert set(body) == {'education_stage'}
                self.stage = body['education_stage']
                return httpx.Response(200, json={'education_stage': self.stage})
            return httpx.Response(200, json=self.profile())
        if path == '/api/search':
            assert dict(request.url.params) == {'word': '月', 'limit': '5', 'stage': 'all'}
            return httpx.Response(200, json={'items': [{'text': '床前明月光', 'poem_id': 'poem-1'}]})
        if path == '/api/poems/poem-1':
            return httpx.Response(200, json={'original': '床前明月光。', 'paragraphs': ['床前明月光。']})
        if path.startswith('/api/admin/'):
            return httpx.Response(403, json={'detail': '仅管理员可访问'})
        if path == '/api/games':
            assert body['word'] == '月' and body['difficulty'] == 'easy'
            assert len(body['request_id']) <= 80 and body['request_id'].startswith('qa-release-1.5-')
            return httpx.Response(200, json={'game': {'id': self.game_id, 'word': '月',
                'difficulty': 'easy', 'status': 'playing'}})
        if path == '/api/games/' + self.game_id:
            if self.fail_detail:
                return httpx.Response(500, text='secret response: ' + self.token)
            return httpx.Response(200, json={'game': {'id': self.game_id, 'round': 0}})
        if path == '/api/games/' + self.game_id + '/end':
            assert set(body) == {'request_id'} and len(body['request_id']) <= 80
            self.game_ended = True
            return httpx.Response(200, json={'game': {'status': 'abandoned'}})
        if path == '/api/feedback':
            assert body['kind'] == 'other' and body['body'].startswith('上线通知验证')
            assert body['contact'] == '' and len(body['request_id']) <= 128
            self.feedback_count += 1
            return httpx.Response(200, json={'id': 'FB-' + 'c' * 16, 'status': 'received',
                                            'notification_status': 'pending'})
        raise AssertionError('Unexpected request: ' + path)


def run(deployment, *, feedback=False):
    with httpx.Client(base_url='https://qa.example', transport=httpx.MockTransport(deployment),
                      headers={'Origin': 'https://qa.example'}) as client:
        return verify(client, 'https://qa.example', feedback=feedback)


@pytest.mark.parametrize('feedback', [False, True])
def test_single_guest_cleanup_and_feedback_opt_in(feedback):
    deployment = Deployment()
    report = run(deployment, feedback=feedback)
    assert report['ok'], report
    assert deployment.created_guests == 1
    assert deployment.feedback_count == int(feedback)
    assert deployment.game_ended and deployment.stage == 'all' and not deployment.signed_in
    assert ('feedback_id' in report) is feedback
    assert deployment.token not in json.dumps(report)


def test_bad_session_never_initializes_second_guest():
    deployment = Deployment(broken_cookie=True)
    report = run(deployment)
    assert not report['ok'] and report['error'] == 'authenticated_me'
    assert deployment.created_guests == 1 and deployment.feedback_count == 0
    assert deployment.token not in json.dumps(report)


def test_failure_still_cleans_up_and_redacts_response():
    deployment = Deployment(fail_detail=True)
    report = run(deployment, feedback=True)
    assert not report['ok'] and report['error'] == 'game_detail'
    assert deployment.game_ended and deployment.stage == 'all' and not deployment.signed_in
    assert deployment.feedback_count == 0
    assert deployment.token not in json.dumps(report)
