"""Bounded failure reasons made only from exception structure, never private text."""
import errno
import json
import subprocess
from urllib.error import HTTPError, URLError


def safe_reason(error):
    kind = type(error).__name__[:64]
    detail = None
    if isinstance(error, HTTPError):
        # Never copy arbitrary header values (or parameters) into stored reasons.
        media = (error.headers.get('Content-Type', '').split(';')[0].strip().lower()
                 if error.headers else '')
        media = media if media in ('text/html', 'text/plain', 'application/json',
                                    'application/xml', 'text/xml', 'application/octet-stream') else 'unknown content type'
        detail = f'HTTP {int(error.code)} {media}'
    elif isinstance(error, subprocess.TimeoutExpired):
        detail = 'command timed out'
    elif isinstance(error, subprocess.CalledProcessError):
        detail = f'exit {int(error.returncode)}'
    elif isinstance(error, json.JSONDecodeError):
        detail = ('empty output' if not error.doc.strip() else
                  f'non-JSON output ({len(error.doc.encode("utf-8"))} bytes)')
    elif isinstance(error, URLError):
        detail = safe_reason(error.reason) if isinstance(error.reason, Exception) else 'network request failed'
    elif isinstance(error, TimeoutError):
        detail = 'timed out'
    elif isinstance(error, OSError) and error.errno is not None:
        detail = errno.errorcode.get(error.errno, 'unknown errno')
    return (kind + (': ' + detail if detail else ''))[:200]
