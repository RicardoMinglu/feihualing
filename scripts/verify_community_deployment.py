"""Verify a 1.5 deployment with one disposable QA guest; feedback is opt-in.

Print a JSON report without cookies or credentials. This creates one game and
ends it, restores the guest's stage, and revokes its session before exiting.
--feedback submits one real notification-triggering feedback record.
"""
import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import quote, urlsplit

import httpx


class VerificationFailure(Exception):
    pass


def site_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise argparse.ArgumentTypeError('Use an HTTP(S) site origin without credentials, query, or path.')
    return value.rstrip('/')


def verify(client, url, *, feedback=False):
    run_id = uuid.uuid4().hex
    label = 'qa-release-1.5-' + run_id
    report = {'url': url, 'qa_label': label, 'started_at': datetime.now(timezone.utc).isoformat(),
              'ok': False, 'feedback_requested': feedback, 'checks': []}
    user_id = game_id = original_stage = None
    stage_changed = False

    def check(name, condition, **details):
        report['checks'].append({'name': name, 'ok': bool(condition), **details})
        if not condition:
            raise VerificationFailure(name)

    def request(name, method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        check(name, response.status_code == expected, status_code=response.status_code)
        return response

    def data(name, method, path, **kwargs):
        return request(name, method, path, **kwargs).json()

    def fail(exc):
        # Never emit HTTP exception text, response bodies, headers, or cookie values.
        report.setdefault('error', str(exc) if isinstance(exc, VerificationFailure) else type(exc).__name__)

    try:
        request('home', 'GET', '/')
        request('anonymous_me_rejected', 'GET', '/api/me', expected=401)
        check('no_implicit_session', client.cookies.get('feihua_session') is None)
        health = data('health', 'GET', '/health')
        check('healthy', health.get('status') == 'ok')
        config = data('config', 'GET', '/api/config')
        check('version_1_5', isinstance(config.get('version'), str) and config['version'].startswith('1.5.'))
        report['version'] = config['version']

        response = request('guest_initialize', 'POST', '/api/auth/guest', json={})
        profile = response.json()
        user_id = profile.get('id')
        check('qa_guest', isinstance(user_id, str) and bool(re.fullmatch(r'u_[0-9a-f]{32}', user_id))
              and profile.get('kind') == 'guest' and profile.get('role') == 'user')
        report['user_id'] = user_id
        cookies = SimpleCookie()
        for header in response.headers.get_list('set-cookie'):
            cookies.load(header)
        cookie = cookies.get('feihua_session')
        check('session_cookie_attributes', cookie is not None and bool(cookie['httponly'])
              and cookie['samesite'].lower() == 'lax' and cookie['path'] == '/')
        if urlsplit(url).scheme == 'https':
            check('secure_cookie', bool(cookie['secure']))
        token = client.cookies.get('feihua_session')
        check('session_cookie_stored', bool(token))
        # Confirm the cookie works before retrying bootstrap: a broken cookie
        # must fail this run instead of creating a second identity.
        me = data('authenticated_me', 'GET', '/api/me')
        check('me_identity', me.get('id') == user_id)
        retried = data('guest_initialize_retry', 'POST', '/api/auth/guest', json={})
        check('same_identity_on_retry', retried.get('id') == user_id
              and client.cookies.get('feihua_session') == token)

        original_stage = me.get('education_stage')
        check('initial_stage', original_stage in ('all', 'primary', 'junior', 'senior'))
        stage_changed = True
        updated = data('stage_update', 'PATCH', '/api/me', json={'education_stage': 'primary'})
        check('stage_update_response', updated.get('education_stage') == 'primary')
        me = data('stage_readback', 'GET', '/api/me')
        check('stage_persisted', me.get('education_stage') == 'primary' and me.get('id') == user_id)
        request('foreign_origin_rejected', 'PATCH', '/api/me', expected=403,
                headers={'Origin': 'https://release-qa.invalid'}, json={'education_stage': 'senior'})
        me = data('stage_after_rejection', 'GET', '/api/me')
        check('rejected_write_unchanged', me.get('education_stage') == 'primary')

        search = data('search', 'GET', '/api/search', params={'word': '月', 'limit': 5, 'stage': 'all'})
        items = search.get('items', [])
        check('search_results', bool(items) and all('月' in row.get('text', '') and row.get('poem_id') for row in items))
        poem = data('poem', 'GET', '/api/poems/' + quote(str(items[0]['poem_id']), safe=''))
        check('poem_content', bool(poem.get('original')) and bool(poem.get('paragraphs')))

        for page in ('overview', 'users', 'feedback', 'notifications', 'audit'):
            request('non_admin_' + page + '_rejected', 'GET', '/api/admin/' + page, expected=403)

        game = data('game_create', 'POST', '/api/games',
                    json={'word': '月', 'difficulty': 'easy', 'request_id': label + ':game'})['game']
        game_id = game.get('id')
        check('game_created', isinstance(game_id, str) and bool(re.fullmatch(r'[0-9a-f]{32}', game_id))
              and game.get('word') == '月' and game.get('difficulty') == 'easy' and game.get('status') == 'playing')
        report['game_id'] = game_id
        detail = data('game_detail', 'GET', '/api/games/' + game_id)['game']
        check('game_restored', detail.get('id') == game_id and detail.get('round') == 0)

        if feedback:
            result = data('feedback_submit', 'POST', '/api/feedback', json={
                'kind': 'other', 'body': '上线通知验证：飞花令 1.5 部署验收。此条为 QA 测试反馈，可标记已处理。\n' + label,
                'contact': '', 'request_id': label + ':feedback',
            })
            check('feedback_saved', isinstance(result.get('id'), str)
                  and bool(re.fullmatch(r'FB-[0-9a-f]{16}', result['id'])) and result.get('status') == 'received')
            report['feedback_id'] = result['id']
            report['notification_delivery'] = 'queued; verify delivery separately in admin and Feishu'
    except Exception as exc:
        fail(exc)
    finally:
        if isinstance(game_id, str) and re.fullmatch(r'[0-9a-f]{32}', game_id):
            try:
                ended = data('game_cleanup', 'POST', '/api/games/' + game_id + '/end',
                             json={'request_id': label + ':end'})['game']
                check('game_ended', ended.get('status') == 'abandoned')
            except Exception as exc:
                fail(exc)
        if stage_changed:
            try:
                restored = data('stage_cleanup', 'PATCH', '/api/me', json={'education_stage': original_stage})
                check('stage_restored', restored.get('education_stage') == original_stage)
            except Exception as exc:
                fail(exc)
        if user_id:
            try:
                request('logout', 'POST', '/api/auth/logout', json={})
                request('logged_out_me_rejected', 'GET', '/api/me', expected=401)
            except Exception as exc:
                fail(exc)
    report['ok'] = 'error' not in report
    report['check_count'] = len(report['checks'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', type=site_url, default='https://feihualing.ltkeep.cn')
    parser.add_argument('--feedback', action='store_true', help='Submit one real QA feedback notification.')
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=25, follow_redirects=False, trust_env=False,
                      headers={'Origin': args.url, 'User-Agent': 'Feihualing-Release-QA/1.5'}) as client:
        report = verify(client, args.url, feedback=args.feedback)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
