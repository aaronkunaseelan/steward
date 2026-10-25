"""Small, Worker-mediated evidence pages; no bucket credentials on the host."""
import base64
from datetime import timedelta
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .jobs import change, instant, now_utc, require
from .state import StateError
from . import transcripts, senses
from .settings import local_zone
from .memory import local_day
from .timing import owner_time

from .settings import pages_host
TOKEN_ENV = 'STEWARD_PAGES_TOKEN'
LIMIT = 10 * 1024 * 1024
SLUG = re.compile(r'[a-z0-9]+(-[a-z0-9]+)*\Z')
STOP = set('a an the and or of for to in on at with from is it this that'.split())


def valid_slug(slug):
    return isinstance(slug, str) and len(slug) <= 80 and SLUG.fullmatch(slug)


def make_slug(title):
    words = [w[:18] for w in re.findall('[a-z0-9]+', title.lower()) if w not in STOP][:3]
    suffix = base64.b32encode(secrets.token_bytes(8)).decode().lower()[:12]
    return '-'.join([*(words or ['page']), suffix])


def template(title, fragment):
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><title>' + html.escape(title) + '</title>'
            '<style>:root{color-scheme:light dark}body{font:17px/1.55 system-ui,sans-serif;'
            'max-width:48rem;margin:auto;padding:1.2rem;overflow-wrap:anywhere}'
            'table{border-collapse:collapse;display:block;overflow:auto}td,th{padding:.4rem;border:1px solid #888;overflow-wrap:normal}'
            'pre{overflow:auto}img{max-width:100%}</style><body>' + fragment + '</body></html>')


def markdown_inline(text):
    """Only our small Markdown vocabulary produces tags; all other input is text."""
    pattern = r'`([^`\n]+)`|(?<!!)\[([^\]\n]+)\]\(([^\s()]*(?:\([^\s()]*\)[^\s()]*)*)\)|\*\*(.+?)\*\*|(?<!\w)__(.+?)__(?!\w)|\*([^*\n]+)\*|(?<!\w)_([^_\n]+)_(?!\w)'
    parts, end = [], 0
    for match in re.finditer(pattern, text):
        parts.append(html.escape(text[end:match.start()]))
        code, label, url, bold, strong, italic, emphasis = match.groups()
        if code is not None:
            parts.append('<code>' + html.escape(code) + '</code>')
        elif label is not None:
            if re.match(r'https?://[^/\s]+', url, re.I):
                parts.append('<a href="' + html.escape(url, quote=True) + '">' + html.escape(label) + '</a>')
            else:
                parts.append(html.escape(match[0]))
        else:
            tag = 'strong' if bold is not None or strong is not None else 'em'
            value = next(v for v in (bold, strong, italic, emphasis) if v is not None)
            parts.append('<' + tag + '>' + markdown_inline(value) + '</' + tag + '>')
        end = match.end()
    parts.append(html.escape(text[end:]))
    return ''.join(parts)


def markdown(text):
    """Render flat headings, lists, pipe tables, paragraphs and fenced code."""
    lines, parts, paragraph = text.splitlines(), [], []
    list_tag, i = None, 0

    def flush():
        nonlocal list_tag
        if paragraph:
            parts.append('<p>' + markdown_inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
        if list_tag:
            parts.append('</' + list_tag + '>')
            list_tag = None

    def cells(line):
        return [cell.strip() for cell in line.strip().strip('|').split('|')]

    while i < len(lines):
        line = lines[i]
        fence = re.match(r'^\s*(`{3,}|~{3,})[^`]*$', line)
        heading = re.match(r'^(#{1,6})\s+(.+)$', line)
        item = re.match(r'^\s*(?:([-+*])|\d+[.)])\s+(.+)$', line)
        table = (i + 1 < len(lines) and '|' in line and
                 all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells(lines[i + 1])))
        if fence:
            flush()
            marker, code = fence[1], []
            i += 1
            while i < len(lines) and not re.fullmatch(r'\s*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*', lines[i]):
                code.append(lines[i])
                i += 1
            parts.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>')
        elif table:
            flush()
            parts.append('<table><thead><tr>' + ''.join('<th>' + markdown_inline(c) + '</th>' for c in cells(line)) + '</tr></thead><tbody>')
            i += 2
            while i < len(lines) and lines[i].strip() and '|' in lines[i]:
                parts.append('<tr>' + ''.join('<td>' + markdown_inline(c) + '</td>' for c in cells(lines[i])) + '</tr>')
                i += 1
            parts.append('</tbody></table>')
            continue
        elif heading:
            flush()
            tag = 'h' + str(len(heading[1]))
            parts.append('<' + tag + '>' + markdown_inline(heading[2]) + '</' + tag + '>')
        elif item:
            tag = 'ul' if item[1] else 'ol'
            if list_tag != tag:
                flush()
                parts.append('<' + tag + '>')
                list_tag = tag
            parts.append('<li>' + markdown_inline(item[2]) + '</li>')
        elif not line.strip():
            flush()
        else:
            if list_tag:
                flush()
            paragraph.append(line.strip())
        i += 1
    flush()
    return '\n'.join(parts)


def prepare(title, data, filename, as_html=False):
    require(isinstance(title, str) and bool(title.strip()) and len(title) <= 500, 'page title required (at most 500 characters)')
    require(len(data) <= LIMIT, 'page exceeds 10 MiB')
    content_type = 'text/html' if as_html or filename.lower().endswith('.html') else mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    text = None
    if content_type == 'application/octet-stream' and b'\0' not in data:
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            pass
    if text is not None or content_type.startswith('text/') or content_type in ('application/json', 'application/xml', 'application/javascript', 'image/svg+xml') or content_type.endswith(('+json', '+xml')):
        text = transcripts.redact(data.decode('utf-8'))
        if content_type == 'text/markdown':
            text = markdown(text)
            content_type = 'text/html'
        if content_type == 'text/html':
            text = template(transcripts.redact(title), text)
        data = text.encode('utf-8')
        content_type += '; charset=utf-8'
    require(len(data) <= LIMIT, 'page exceeds 10 MiB after wrapping')
    return data, content_type


def token():
    value = os.environ.get(TOKEN_ENV)
    require(bool(value), 'pages token unavailable')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # Never forward a credential or count a redirected HEAD as verified.


def request(method, path, data=None, content_type=None, timeout=10, public=False):
    headers = {'User-Agent': 'steward/pages'}
    if path.startswith('/_api/'):
        headers['Authorization'] = 'Bearer ' + token()
    if public:
        headers['X-Steward-Public'] = '1'
    if content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(pages_host() + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            return response.status, response.read(LIMIT + 1 if path.startswith('/_api/pages/') else 256 * 1024) if method == 'GET' else b''
    except urllib.error.HTTPError as exc:
        return exc.code, b''


def meta(db, key, value):
    db.execute('INSERT INTO message_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, json.dumps(value)))


def read_meta(db, key, default=None):
    row = db.execute('SELECT value FROM message_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def cut_line(cut):
    at = cut.get('until')
    return 'gsd as of ' + (owner_time(instant(at), '%d %b %Y %H:%M') + ' (Athens)' if at else 'unknown (no landed cut)')


def item_line(item, project, notion_url):
    title = ' '.join(item['title'].split())
    label = '[' + title + '](' + notion_url + ')' if notion_url else title
    due = owner_time(instant(item['due_at']), '%d %b %Y %H:%M') + ' (Athens)' if item['due_at'] else 'not dated'
    return '- ' + label + ' — ' + (project or 'no project') + ' — due ' + due


def prep_header(db, items, block, now, runner):
    """Only block context and live gsd links, never the prose on Notion."""
    lines = []
    if block:
        from .calendar_b import latest
        demand = next((r for r in latest(db, 'demands') if r['name'] == block), None)
        require(demand is not None, 'unknown block demand')
        spec = json.loads(demand['spec_json'])
        today = local_day(now)
        applied = [json.loads(r['payload']) for r in latest(db, 'blocks')
                   if r['demand'] == block and r['state'] == 'applied']
        applied = sorted((p for p in applied if local_day(instant(p['start'])) == today),
                         key=lambda p: p['start'])
        lines.append('## ' + (applied[0]['title'] if applied else spec['title']))
        for p in applied:
            lines.append(owner_time(instant(p['start']), '%d %b %H:%M') + '–' +
                         owner_time(instant(p['end']), '%H:%M') + ' (Athens)')
        if not applied:
            lines.append('Block time: unknown (no applied block today)')
        lines.append('Why together: ' + spec['justification'])
        lines.append('')
    try:
        raw = senses.worklist(runner)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, senses.ContractError) else 'read failed: ' + type(exc).__name__
        return '\n'.join(lines + ['gsd: unknown (' + reason + ')', 'gsd as of unknown']) + '\n\n'
    projects = {p['id']: p['name'] for p in raw['projects']}
    open_items = {i['id']: i for i in raw['items']}
    for identity in items:
        item = open_items.get(identity)
        lines.append(item_line(item, projects.get(item['project_id']), item['notion_url']) if item else
                     '- ' + identity + ': no longer open in gsd')
    return '\n'.join(lines + ['', cut_line(raw['cut'])]) + '\n\n'


def sync_your_list(db, sense, now, transport=None):
    """Each tick: render and hash locally; only changed content makes a PUT/read-back."""
    if not sense:
        return your_list(db)  # gsd not read this tick (interval not due): the last page stands.
    if sense.get('status') != 'ok':
        text = '# Your list\n\ngsd: unknown (' + sense.get('reason', 'not observed') + ')\n'
    else:
        data = sense['data']
        lines = ['# Your list', '', cut_line(data['cut']), '']
        for item in data['waiting_on_you']:
            lines.append(item_line(item, item['project'], data.get('notion_urls', {}).get(item['id'])))
        if not data['waiting_on_you']:
            lines.append('No open items of yours.')
        text = '\n'.join(lines) + '\n'
    rendered, _ = prepare('Your list', text.encode(), 'your-list.md')
    digest = hashlib.sha256(rendered).hexdigest()
    slug = read_meta(db, 'your_list_slug')
    if slug is None:
        slug = make_slug('Your list')
        with db:
            meta(db, 'your_list_slug', slug)
    row = db.execute('SELECT sha256,state FROM pages WHERE slug=?', (slug,)).fetchone()
    if row and row['sha256'] == digest and row['state'] == 'live':
        return dict(slug=slug, url=pages_host() + '/' + slug, state='live', changed=False)
    return put(db, 'Your list', text.encode(), 'your-list.md', slug=slug,
               now=now, actor='tick', transport=transport)


def your_list(db):
    slug = read_meta(db, 'your_list_slug')
    row = db.execute('SELECT state FROM pages WHERE slug=?', (slug,)).fetchone() if slug else None
    return dict(slug=slug, url=pages_host() + '/' + slug, state=row['state']) if row else 'unknown: not rendered yet'


def put(db, title, data, filename='', slug=None, as_html=False, now=None, actor='owner', transport=None, public=False,
        items=None, block=None, runner=senses.run):
    now, transport = now or now_utc(), transport or request
    require(not block or items is not None, '--block requires --items')
    if items is not None:
        require(bool(items) and all(isinstance(i, str) and i.strip() for i in items), '--items needs gsd ids')
        require(not as_html and filename.lower().endswith('.md'), '--items requires Markdown (.md)')
        require(len(data) <= LIMIT, 'page exceeds 10 MiB')
        data = (prep_header(db, items, block, now, runner) + data.decode('utf-8')).encode('utf-8')
    data, content_type = prepare(title, data, filename, as_html)
    if slug is not None:
        require(valid_slug(slug), 'invalid page slug')
        # A named slug creates or overwrites; a removed page stays removed.
        row = db.execute('SELECT state FROM pages WHERE slug=?', (slug,)).fetchone()
        require(row is None or row['state'] != 'removed', '--slug names a removed page')
    else:
        slug = make_slug(title)
        require(not db.execute('SELECT 1 FROM pages WHERE slug=?', (slug,)).fetchone(), 'page slug collision; retry')
    expires = (now + timedelta(days=28)).isoformat()
    # Persist intent before any outward effect. A crash leaves an honest failed row.
    with db:
        cid = change(db, 'page put', now, actor)
        db.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?) ON CONFLICT(slug) DO UPDATE SET '
                   'title=excluded.title,content_type=excluded.content_type,sha256=excluded.sha256,bytes=excluded.bytes,'
                   'change_id=excluded.change_id,wake_id=excluded.wake_id,created_at=excluded.created_at,'
                   'expires_at=excluded.expires_at,opened_at=NULL,opens=0,state=excluded.state,public=excluded.public',
                   (slug, transcripts.redact(title), content_type, hashlib.sha256(data).hexdigest(), len(data), cid,
                    os.environ.get('STEWARD_WAKE_ID'), now.isoformat(), expires, None, 'failed', int(public)))
    code = 'upload_unknown'
    try:
        status, _ = transport('PUT', '/_api/pages/' + slug, data=data, content_type=content_type, public=public)
        code = 'put_' + str(status)
        if 200 <= status < 300:
            status, stored = transport('GET', '/_api/pages/' + slug)
            code = ('read_' + str(status) if status != 200 else
                    None if hashlib.sha256(stored).digest() == hashlib.sha256(data).digest() else 'content_mismatch')
    except (OSError, StateError, ValueError):
        code = 'network_or_config'
    state = 'failed' if code else 'live'
    with db:
        db.execute('UPDATE pages SET state=? WHERE slug=?', (state, slug))
        db.execute('UPDATE changes SET summary=? WHERE id=?', ('page put ' + slug + ': ' + (code or 'live'), cid))
        if code:
            meta(db, 'pages_put_error', dict(code=code, at=now.isoformat()))
    return dict(slug=slug, url=pages_host() + '/' + slug, state=state, expires_at=expires, public=public)


def remove(db, slug, now=None, actor='owner', transport=None):
    now, transport = now or now_utc(), transport or request
    require(valid_slug(slug), 'invalid page slug')
    require(db.execute('SELECT 1 FROM pages WHERE slug=?', (slug,)).fetchone(), 'unknown page')
    with db:
        cid = change(db, 'page rm', now, actor)
    status, _ = transport('DELETE', '/_api/pages/' + slug)
    require(200 <= status < 300, 'page delete failed: ' + str(status))
    with db:
        db.execute("UPDATE pages SET state='removed',change_id=? WHERE slug=?", (cid, slug))
    return dict(change_id=cid, slug=slug, state='removed')


def poll(db, now=None, transport=None):
    """One bounded list call per tick. Cycle scans: slug-first keys are NOT an arrival log.

    Seen-marker keys deduplicate rescans, including after a crash or late marker
    arrival. Reset at EOF: a later open can sort before the previous cursor.
    """
    now, transport = now or now_utc(), transport or request
    try:
        after = read_meta(db, 'pages_opens_cursor', '')
        status, body = transport('GET', '/_api/opens?' + urllib.parse.urlencode(dict(after=after, limit=1000)), timeout=10)
        require(status == 200, 'http_' + str(status))
        data = json.loads(body)
        require(isinstance(data, dict) and set(data) == {'keys', 'next'}, 'invalid_response')
        keys, next_key = data['keys'], data['next']
        require(isinstance(keys, list) and len(keys) <= 1000 and all(isinstance(k, str) for k in keys), 'invalid_response')
        require(keys == sorted(set(keys)) and all(k > after for k in keys), 'invalid_response')
        require(next_key is None or bool(keys) and next_key == keys[-1], 'invalid_response')
        markers = []
        for key in keys:
            parts = key.split('/')
            require(len(parts) == 3 and parts[0] == '_opens' and valid_slug(parts[1]), 'invalid_response')
            at = instant(parts[2]).isoformat()
            markers.append((key, parts[1], at))
        with db:
            for key, slug, at in markers:
                seen = 'pages_open_seen:' + key
                if read_meta(db, seen):
                    continue
                db.execute("UPDATE pages SET opens=opens+1,opened_at=CASE WHEN opened_at IS NULL OR opened_at>? THEN ? ELSE opened_at END WHERE slug=? AND state='live' AND created_at<=?", (at, at, slug, at))
                meta(db, seen, now.isoformat())
            # R2 expires markers after 28 days; retain dedup keys slightly longer.
            db.execute("DELETE FROM message_meta WHERE key GLOB 'pages_open_seen:*' AND value<?",
                       (json.dumps((now - timedelta(days=29)).isoformat()),))
            meta(db, 'pages_opens_cursor', next_key or '')
            meta(db, 'pages_opens_ok_at', now.isoformat())
            meta(db, 'pages_opens_error', None)
        return True
    except (OSError, StateError, ValueError, TypeError, KeyError, OverflowError):
        # Codes only: never a response body, URL query, credential or exception text.
        code = 'http_' + str(status) if 'status' in locals() and status != 200 else 'invalid_response' if 'body' in locals() else 'network_or_config'
        with db:
            meta(db, 'pages_opens_error', dict(code=code, at=now.isoformat()))
        return False


def live(db, now):
    return [dict(r, public=bool(r['public']), url=pages_host() + '/' + r['slug']) for r in db.execute("SELECT title,slug,created_at,expires_at,opened_at,opens,public FROM pages WHERE state='live' ORDER BY created_at") if instant(r['expires_at']) > now]


def status(db, now):
    rows = live(db, now)
    return dict(live=len(rows), opened=sum(r['opened_at'] is not None for r in rows), items=rows,
                last_poll_ok_at=read_meta(db, 'pages_opens_ok_at'), last_error=read_meta(db, 'pages_opens_error'))
