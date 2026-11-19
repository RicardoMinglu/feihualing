"""Single-worker request limits. Forwarded headers are trusted only by Uvicorn."""
import hashlib
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from contextlib import contextmanager
from fastapi import HTTPException


class RateLimits:
    def __init__(self):
        self._lock = threading.Lock()
        self._buckets = OrderedDict()

    def check(self, key, limit, seconds):
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            self._buckets.move_to_end(key)
            while bucket and bucket[0] <= now - seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                raise HTTPException(429, '操作太频繁，请稍后再试', headers={'Retry-After': str(seconds)})
            bucket.append(now)
            while len(self._buckets) > 10000:
                self._buckets.popitem(last=False)


limits = RateLimits()
_password_slots = threading.BoundedSemaphore(2)


@contextmanager
def password_slot():
    if not _password_slots.acquire(timeout=1):
        raise HTTPException(429, '账号服务繁忙，请稍后重试')
    try:
        yield
    finally:
        _password_slots.release()


def limited(request, group, limit=60, seconds=60, user=False):
    # Never read X-Forwarded-For here: it is freely forgeable outside the trusted proxy.
    identity = request.state.owner if user else (request.client.host if request.client else 'unknown')
    limits.check((group, identity), limit, seconds)


def username_limit(username):
    key = hashlib.sha256(unicodedata.normalize('NFKC',username).strip().casefold().encode()).hexdigest()
    limits.check(('login-name', key), 20, 900)


def check_origin(request):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '请求来源无效')
    if request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none'):
        raise HTTPException(403, '请求来源无效')
    if request.method in ('POST', 'PATCH', 'PUT') and not request.headers.get('content-type', '').lower().startswith('application/json'):
        raise HTTPException(415, '请使用 JSON 提交')
