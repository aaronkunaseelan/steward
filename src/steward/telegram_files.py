"""Bounded in-memory Telegram attachment download; no extraction."""
import http.client
import json
import signal
import time
from urllib.parse import urlsplit

LIMIT = 20 * 1024 * 1024


class Failed(Exception):
    pass


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise Failed('timeout')
    return seconds


def request(url, deadline, body=None, headers=None, limit=LIMIT):
    """No redirects; share one deadline across connection, headers and body."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise Failed('rejected')
    conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=remaining(deadline))
    try:
        conn.request('POST' if body is not None else 'GET',
                     parts.path + ('?' + parts.query if parts.query else ''), body, headers or {})
        sock = conn.sock
        sock.settimeout(remaining(deadline))
        response = conn.getresponse()
        if response.status >= 400:
            raise Failed('rejected')
        if response.status >= 300:
            raise Failed('rejected')
        if int(response.getheader('Content-Length', '0')) > limit:
            raise Failed('too_large')
        chunks, size = [], 0
        while not response.isclosed():
            sock.settimeout(remaining(deadline))
            chunk = response.read1(min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise Failed('too_large')
        return b''.join(chunks), dict(response.getheaders())
    finally:
        conn.close()


def download(file, deadline):
    from .notify import bot_token
    try:
        if (file.get('file_size') or 0) > LIMIT:
            raise Failed('too_large')
        token = bot_token()
        body = json.dumps({'file_id': file['file_id']}).encode()
        raw, _ = request('https://api.telegram.org/bot' + token + '/getFile', deadline,
                         body, {'Content-Type': 'application/json'})
        result = json.loads(raw)
        if result.get('ok') is not True:
            raise Failed('download')
        info = result['result']
        if info.get('file_size', 0) > LIMIT:
            raise Failed('too_large')
        path = info['file_path']
        if not isinstance(path, str) or path.startswith('/') or '..' in path or '?' in path or '#' in path:
            raise Failed('download')
        return request('https://api.telegram.org/file/bot' + token + '/' + path, deadline)[0]
    except Failed as exc:
        raise Failed(str(exc) if str(exc) in ('too_large', 'timeout') else 'download') from None
    except TimeoutError:
        raise Failed('timeout') from None
    except Exception:
        raise Failed('download') from None


def bounded(file, downloader=download):
    """A wall timer also bounds DNS and trickling response headers."""
    def timed_out(signum, frame):
        raise Failed('timeout')
    previous = signal.signal(signal.SIGALRM, timed_out)
    started = time.monotonic()
    timer = signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        if (file.get('file_size') or 0) > LIMIT:
            raise Failed('too_large')
        data = downloader(file, started + 60)
        if len(data) > LIMIT:
            raise Failed('too_large')
        return data
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0] or timer[1]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, timer[0] - (time.monotonic() - started)), timer[1])
