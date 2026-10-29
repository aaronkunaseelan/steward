"""ELTA public tracking: fresh cookie session, ephemeral headers, token-free results."""
import argparse
import datetime as dt
import html.parser
import http.cookiejar
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = 'https://itemsearch.elta.gr/en-GB'
SOURCE_ZONE = ZoneInfo('Europe/Athens')


class EltaError(RuntimeError):
    """A named lookup failure, never a provider response or credential."""


class Rows(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
        self.tables = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.tables += 1
        elif tag == 'tr':
            self.row = []
        elif tag == 'td' and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def parse_history(view, code):
    if not re.search(r'Tracking Number\s*:\s*' + re.escape(code) + r'\b', view):
        raise EltaError('ELTA DirectList: identity mismatch (requested tracking number not identified)')
    parser = Rows()
    parser.feed(view)
    if not parser.tables:
        raise EltaError('ELTA DirectList: changed markup (tracking table missing)')
    events = []
    for row in parser.rows:
        if len(row) != 3 or not row[1]:
            raise EltaError('ELTA DirectList: changed markup (expected date, status, location)')
        try:
            date = dt.datetime.strptime(row[0], '%d/%m/%Y %H:%M').replace(tzinfo=SOURCE_ZONE)
        except ValueError:
            raise EltaError('ELTA DirectList: changed markup (invalid event date)') from None
        events.append({'time': date.isoformat(timespec='minutes'), 'timezone': SOURCE_ZONE.key,
                       'status': row[1], 'location': row[2] or None})
    return sorted(events, key=lambda event: dt.datetime.fromisoformat(event['time']), reverse=True)


def lookup(code, timeout=12, proxy=None):
    if not re.fullmatch(r'[A-Z]{2}[0-9]{9}[A-Z]{2}', code):
        raise EltaError('ELTA: expected two letters, nine digits, two letters')
    jar = http.cookiejar.CookieJar()
    proxies = {'http': proxy, 'https': proxy} if proxy else {}
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                        urllib.request.ProxyHandler(proxies))
    direct = BASE + '/Query/Direct/' + code

    def request(path, data=None, headers=None):
        req = urllib.request.Request(BASE + path, data=data, headers={
            'User-Agent': 'elta-track/1.0', 'Referer': direct,
            'X-Requested-With': 'XMLHttpRequest', **(headers or {})})
        stage = path.rsplit('/', 1)[-1]
        try:
            with opener.open(req, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    raise EltaError(f'ELTA {stage}: HTTP {response.status}')
                return response.read().decode('utf-8-sig')
        except urllib.error.HTTPError as error:
            raise EltaError(f'ELTA {stage}: HTTP {error.code}') from None
        except TimeoutError:
            raise EltaError(f'ELTA {stage}: request timed out') from None
        except urllib.error.URLError as error:
            reason = 'request timed out' if isinstance(error.reason, TimeoutError) else 'network request failed'
            raise EltaError(f'ELTA {stage}: {reason}') from None
        except OSError:
            raise EltaError(f'ELTA {stage}: network request failed') from None
        except UnicodeError:
            raise EltaError(f'ELTA {stage}: invalid response encoding') from None

    def reply(path, data, headers=None):
        body = request(path, data, headers)
        stage = path.rsplit('/', 1)[-1]
        try:
            result = json.loads(body)
        except ValueError:
            raise EltaError(f'ELTA {stage}: invalid JSON response') from None
        if not isinstance(result, dict) or result.get('Success') is not True:
            raise EltaError(f'ELTA {stage}: Success false or missing')
        return result

    request('/Query/Direct/' + code)
    tokens = reply('/Security/GetToken', b'').get('Tokens')
    if not isinstance(tokens, dict) or not tokens or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in tokens.items()):
        raise EltaError('ELTA GetToken: missing token mapping')
    result = reply('/query/DirectList', urllib.parse.urlencode({'codes[]': code}).encode(),
                   {**tokens, 'Content-Type': 'application/x-www-form-urlencoded'})
    if not isinstance(result.get('View'), str):
        raise EltaError('ELTA DirectList: changed markup (View missing)')
    events = parse_history(result['View'], code)
    latest = events[0] if events else None
    return {'schema': 'parcel-tracking.v1', 'carrier': 'ELTA', 'tracking_number': code,
            'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'source_url': direct,
            'route': 'explicit-proxy' if proxy else 'direct', 'lookup_ok': True,
            'history_present': bool(events), 'latest_event': latest, 'events': events,
            'final': bool(latest and latest['status'].casefold() == 'delivered'),
            'time_note': 'ELTA local Greek timestamps interpreted as Europe/Athens.',
            'interpretation_note': 'Statuses are verbatim. An export scan does not confirm departure, Greek arrival, or delivery.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tracking_number', help='13-character international postal tracking number')
    parser.add_argument('--timeout', type=float, default=12, help='seconds per HTTP request (three requests)')
    parser.add_argument('--proxy', help='explicit HTTP proxy; no direct fallback')
    parser.add_argument('--out', type=Path, help='save the same token-free JSON receipt to this path')
    args = parser.parse_args()
    code = args.tracking_number.strip().upper()
    if not re.fullmatch(r'[A-Z]{2}[0-9]{9}[A-Z]{2}', code):
        parser.error('expected two letters, nine digits, two letters')
    if not 0 < args.timeout <= 60:
        parser.error('--timeout must be > 0 and <= 60')
    started = time.monotonic()
    try:
        result = lookup(code, args.timeout, args.proxy)
    except Exception as error:
        # Never echo arbitrary provider bodies, headers, cookies or proxy credentials.
        result = {'schema': 'parcel-tracking.v1', 'tracking_number': code, 'lookup_ok': False,
                  'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'error_type': type(error).__name__,
                  'error': str(error) if isinstance(error, EltaError) else 'ELTA: unexpected lookup failure; no parcel-status inference'}
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered)
    sys.stdout.write(rendered)
    return 0 if result['lookup_ok'] else 1
