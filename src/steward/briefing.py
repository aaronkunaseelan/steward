"""The wake's decision page. Raw state is available through the CLI, not embedded."""
from datetime import datetime, timedelta, time
import hashlib
import json
from pathlib import Path
import re

from .state import StateError
from . import memory


def sections(text, spans=None):
    """Slice the archived render, including its original whitespace; never re-render."""
    if spans is not None:
        return {name: text[item['start']:item['end']] for name, item in spans.items()}
    # Older wakes have no recorded boundaries.
    headings = list(re.finditer(r'^## (.+?)(?: \[(?:fresh|stale|unknown)\])?$', text, re.M))
    return {heading[1]: text[heading.start():headings[i + 1].start() if i + 1 < len(headings) else len(text)]
            for i, heading in enumerate(headings)}


# A sense's observation time changes every tick; the verdict in the heading carries freshness.
AS_OF = re.compile(r'^[a-z_]+: (?:fresh|stale|unknown) — .*; as of .*\n?', re.M)


def section_info(text, receipt):
    spans = receipt['section_spans']
    return {name: dict(**spans[name], digest=hashlib.sha256(AS_OF.sub('', body).encode()).hexdigest(),
                       lines=len(body.splitlines()),
                       unknown=receipt.get('section_unknowns', {}).get(name) or
                       next((line.strip() for line in body.splitlines()
                             if re.search(r'(?:^|: )unknown(?:$|:)', line)), None))
            for name, body in sections(text, spans).items()}


# These are the inputs the existing moments explicitly ask the model to read.
MOMENT_SECTIONS = {
    'orientation': {'Schedule', 'Limits', 'Calendar', 'Movement', 'Blocks', 'Your list',
                    'Waiting on others', 'Deadlines', 'Compute', 'Finds waiting', 'Inbox'},
    'day_close': {'Schedule', 'Limits', 'Calendar', 'Ahead', 'Blocks', 'Deadlines', 'Compute',
                  'Finds waiting', 'Inbox'},
}


def wake_index(text, info, previous, trigger, moments):
    parts = sections(text, info)
    output = ['# Steward wake index', 'Trigger: ' + trigger, '', parts['Now'].rstrip(), '']
    if 'What I carry' in parts:
        output += [parts['What I carry'].rstrip(), '']
    output.append('## Sections (change since last model wake)')
    for name, item in info.items():
        if name == 'Now':
            continue
        old = previous.get(name)
        change = ('unknown (' + ' '.join(item['unknown'].split()) + ')' if item['unknown'] else
                  'no model baseline' if old is None else
                  'unchanged' if item['digest'] == old['digest'] else 'changed')
        required = [moment for moment in moments if name in MOMENT_SECTIONS.get(moment, ()) or
                    (moment == 'day_close' and name.startswith('Day close — '))]
        output.append(f"- {name}: {item['lines']} lines · {change}" +
                      (' · required for ' + ', '.join(required) if required else ''))
    return '\n'.join(output) + '\n'


def pull(data, wake_id=None, names=(), list_only=False):
    root = Path(data) / 'wakes'
    if wake_id is None:
        ids = [int(p.parent.name) for p in root.glob('*/briefing.md') if p.parent.name.isdecimal()]
        if not ids:
            raise StateError('no archived briefing')
        wake_id = max(ids)
    path = root / str(wake_id) / 'briefing.md'
    if not path.is_file():
        raise StateError(f'no briefing for wake {wake_id}')
    text = path.read_text()
    manifest = path.with_name('briefing-sections.json')
    parts = sections(text, json.loads(manifest.read_text()) if manifest.is_file() else None)
    if list_only:
        return '\n'.join(parts) + '\n'
    missing = [name for name in names if name not in parts]
    if missing:
        raise StateError('unknown briefing section: ' + ', '.join(missing))
    return ''.join(parts[name] for name in names) if names else text

from .senses import freshness
from .settings import local_zone
from .timing import owner_time


def local(value):
    if value is None:
        return 'unknown'
    if isinstance(value, str):
        try:
            at = datetime.fromisoformat(value)
            if at.utcoffset() is not None:
                return owner_time(at)
        except ValueError:
            pass
        return value.replace('\n', ' / ')
    return str(value)


def lines(value, depth=0):
    """Labelled Markdown, retaining unknowns and every row, never JSON blobs."""
    indent = '  ' * depth
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                result.append(indent + '- ' + key.replace('_', ' ') + ':')
                result.extend(lines(item, depth + 1))
            else:
                result.append(indent + '- ' + key.replace('_', ' ') + ': ' + local(item))
        return result or [indent + '- none']
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(lines(item, depth))
        return result or [indent + '- none']
    return [indent + '- ' + local(value)]


def day_close(db, now):
    """Own-store evidence for the evening; never infer delivery from a successful run."""
    journal = memory.journal(db, day=memory.local_day(now).isoformat(), full=True)
    def hm(value):
        return owner_time(datetime.fromisoformat(value), '%H:%M') if value else 'unknown'

    return dict(truncated=journal['truncated'],
                changes=[f"{hm(row['at'])} {row['verb']}: {' '.join(row['summary'].split())[:100]}"
                         for row in journal['changes'] if row['verb'].split()[0] not in ('tick', 'release', 'mind')],
                runs=[f"{hm(row['ended_at'] or row['started_at'] or row['planned_for'])} {row['job']} {row['state']}"
                      for row in journal['runs']],
                owner_messages=f"{len(journal['inbox'])} received; {sum(bool(row['handled']) for row in journal['inbox'])} handled",
                deliveries=[f"{hm(row['sent_at'])} {row['topic']}: {row['text'].split(chr(10), 1)[0][:80]}"
                            for row in journal['messages']])


def blocks(b, gsd, live_pages, now, end):
    """Blocks through the next two days, with their items, prep page and evidence of use."""
    specs = {r['name']: json.loads(r['spec_json']) for r in b.get('demands', [])}
    from . import pages
    done = gsd.get('done')  # today's closures, each at gsd's own closure time
    closed = {row['id']: row['at'] for row in done or []}
    today = memory.local_day(now)
    rows = []
    for r in sorted(b.get('blocks', []), key=lambda r: json.loads(r['payload'])['start']):
        p = json.loads(r['payload'])
        start = datetime.fromisoformat(p['start'])
        if start >= end:
            continue
        row = dict(key=r['event_key'], title=p['title'], start=p['start'], end=p['end'], state=r['state'])
        items = specs.get(r['demand'], {}).get('items')
        if items:
            page = pages.prepared_page(live_pages, ('block', r['demand']), memory.local_day(start))
            row.update(
                items=items,
                prepared=dict(url=page['url'], opened_at=page['opened_at']) if page else 'no: no live prepared page',
                closed_today=('unknown: gsd done not supplied' if done is None else
                              [f"{datetime.fromisoformat(closed[i]).astimezone(local_zone()):%H:%M} {i}" for i in items if i in closed]),
                used=('unknown: gsd done not supplied' if done is None else
                      'yes' if memory.local_day(start) == today and any(i in closed for i in items) else
                      'open' if datetime.fromisoformat(p['end']) > now else
                      'not yet known: gsd learns closures at its runs'))
        rows.append(row)
    return rows


AHEAD_BYTES = 16384


def ahead_lines(calendar, now):
    """Date bands only: the model, not the renderer, judges what matters."""
    from .senses import calendar_instant
    far = calendar.get('ahead', {})
    through = far.get('to', 'unknown')
    coverage = f"through {through} (exclusive); beyond is unknown; far read {far.get('at', 'unknown')}"
    if far.get('status') != 'ok':
        return [coverage, 'unknown: ' + far.get('reason', 'not observed')]
    day = memory.local_day(now)
    first = datetime.combine(day + timedelta(days=3), time.min, local_zone())
    end = datetime.fromisoformat(through)
    # Prefer the per-tick observation wherever the near window overlaps the daily view.
    events = {(e['calendar'], e['id']): e for e in far.get('events', [])}
    near_from, near_to = calendar.get('from'), calendar.get('to')
    if near_from and near_to:
        lo, hi = datetime.fromisoformat(near_from), datetime.fromisoformat(near_to)
        events = {key: e for key, e in events.items()
                  if not (calendar_instant(e['start']) < hi and calendar_instant(e['end']) > lo)}
    events.update({(e['calendar'], e['id']): e for e in calendar.get('events', [])})
    selected = [e for e in events.values() if e.get('status') != 'cancelled'
                and calendar_instant(e['start']) < end and calendar_instant(e['end']) > first
                and not (e['calendar'] == calendar.get('schedule_calendar_id') and e.get('event_key'))]
    selected.sort(key=lambda e: (calendar_instant(e['start']), e['calendar'], e['id']))
    entries, series = [], {}
    def title(event):
        return ' '.join((event.get('summary') or '(untitled)').split())
    def stamp(value):
        return calendar_instant(value).astimezone(local_zone()).strftime('%Y-%m-%d' if 'date' in value else '%Y-%m-%d %H:%M %Z')
    for event in selected:
        at = calendar_instant(event['start'])
        if (at.astimezone(local_zone()).date() - day).days <= 30 or not event.get('recurring_event_id'):
            entries.append((at, f"{stamp(event['start'])} – {stamp(event['end'])}: {title(event)}" +
                            (' (all-day; end exclusive)' if event.get('all_day') else '')))
        else:
            series.setdefault((event['calendar'], event['recurring_event_id']), []).append(event)
    for group in series.values():
        event = group[0]
        cadence = '; '.join(event.get('recurrence') or []) or 'cadence unknown'
        entries.append((calendar_instant(event['start']),
                        f"{title(event)} · {cadence} · first {stamp(event['start'])}; "
                        f"last {stamp(group[-1]['start'])}; {len(group)} instances"))
    entries.sort(key=lambda item: item[0])
    result = [coverage]
    for i, (_, line) in enumerate(entries):
        marker = f'truncated: {len(entries) - i} more through {through}'
        # Account for Markdown bullets/newlines, including the omission marker.
        candidate = result + [line]
        reserve = [f'truncated: {len(entries) - i - 1} more through {through}'] if i + 1 < len(entries) else []
        if len(''.join('- ' + row + '\n' for row in candidate + reserve).encode()) > AHEAD_BYTES:
            result.append(marker)
            break
        result.append(line)
    return result if entries else result + ['none in the observed window']


def render(state, now, receipt):
    senses = state.get('senses', {})
    verdicts = {name: freshness(name, sense, now) for name, sense in senses.items()}
    unknown = []
    section_unknowns = {}
    starts = {}
    output = ['# Steward wake · ' + local_zone().key]

    def section(title, value, names=()):
        facts = [verdicts.get(n, dict(verdict='unknown', reason='not observed', as_of=None)) for n in names]
        verdict = ('unknown' if any(f['verdict'] == 'unknown' for f in facts) else
                   'stale' if any(f['verdict'] == 'stale' for f in facts) else 'fresh')
        if verdict == 'unknown':
            unknown.append(title)
            section_unknowns[title] = '; '.join(f['reason'] for f in facts if f['verdict'] == 'unknown')
        starts[title] = sum(len(line) + 1 for line in output) + 1
        output.append('\n## ' + title + ' [' + verdict + ']')
        for name, fact in zip(names, facts):
            output.append(f"{name}: {fact['verdict']} — {fact['reason']}; as of {local(fact['as_of'])}")
        output.extend(lines(value))

    def data(name):
        return senses.get(name, {}).get('data', {})

    # Own-state fields have known absence semantics; missing fields remain unknown.
    skips = state.get('tick_skips')
    if isinstance(skips, dict):
        skips = dict(skips, current=skips['current'] if skips.get('current') is not None else 'none')
    section('Now', dict(time=now.isoformat(), gate=state.get('gate_reason', 'unknown'),
                        moments=state.get('moments', []), experiments=state.get('experiments', []),
                        provocation=state.get('provocation', 'unknown: not supplied'), tick_skips=skips))
    from .notify import TODAY_CHARACTERS
    section('Schedule', [dict(name=m['name'], start=m['start'], deadline=m['deadline'],
                              releases=m['releases'] or 'none',
                              notify='--moment ' + m['name'])
                         for m in state.get('moment_schedule', [])])
    section('Limits', dict(today_characters=TODAY_CHARACTERS, longer_detail='Put the detail on a page.'))
    section('Inbox', dict(unhandled=state.get('inbox', []), poll=state.get('inbound_poll'),
                          live_pages=state.get('live_pages', [])))
    start = datetime.combine(memory.local_day(now), time.min, local_zone())
    end = start + timedelta(days=3)

    def instant(value):
        if isinstance(value, dict):
            value = value.get('dateTime') or value.get('date')
        at = datetime.fromisoformat(value)
        return at if at.utcoffset() is not None else at.replace(tzinfo=local_zone())

    events = [e for e in data('calendars').get('events', [])
              if e.get('status') != 'cancelled' and instant(e['start']) < end and instant(e['end']) > start]
    events.sort(key=lambda e: instant(e['start']))
    calendar_reading = senses.get('calendars', {})
    section('Calendar', ('unknown: ' + (calendar_reading.get('reason') or 'not observed'))
            if calendar_reading.get('status') != 'ok' else [dict(title=e.get('summary'), start=instant(e['start']).isoformat(),
                              end=instant(e['end']).isoformat(), calendar=e.get('calendar'),
                              all_day=e.get('all_day'), event_key=e.get('event_key'),
                              transparency=e.get('transparency'), kind=e.get('eventType')) for e in events], ('calendars',))
    ahead = data('calendars').get('ahead', {})
    ahead_stale = ahead.get('day') is not None and ahead['day'] != memory.local_day(now).isoformat()
    verdicts['calendar_ahead'] = dict(verdict=('stale' if ahead_stale else 'fresh') if ahead.get('status') == 'ok' else 'unknown',
                                    reason=('daily read overdue' if ahead_stale else 'daily read succeeded') if ahead.get('status') == 'ok' else ahead.get('reason', 'not observed'),
                                    as_of=ahead.get('at'))
    section('Ahead', ahead_lines(data('calendars'), now), ('calendars', 'calendar_ahead'))
    b = state.get('calendar_b', {})
    section('Movement', dict(ranked_open_doses=b.get('movement'), fulfilment=b.get('fulfilment')), ('health', 'calendars'))
    block_rows = blocks(b, data('gsd'), state.get('live_pages', []), now, end)
    section('Blocks', block_rows, ('calendars', 'gsd'))
    section('Your list', dict(page=state.get('your_list_page', 'unknown: not rendered yet'),
                               items=data('gsd').get('waiting_on_you', 'unknown: refresh gsd sense')), ('gsd',))
    section('Waiting on others', data('gsd').get('waiting_on_others', 'unknown: refresh gsd sense'), ('gsd',))
    section('Deadlines', dict(items=data('gsd').get('deadlines'), older=data('gsd').get('past_due_older')), ('gsd',))
    runs = state.get('runs', state.get('last_runs', []))
    today = [r for r in runs if start <= instant(r.get('started_at') or r['planned_for']) < start + timedelta(days=1)]
    section('Compute', dict(plan=state.get('calendar_c', []), jobs=state.get('jobs', []),
                            today=[{k: r.get(k) for k in ('run_id', 'job', 'state', 'planned_for', 'started_at', 'ended_at', 'reason')} for r in today],
                            failures=[{k: r.get(k) for k in ('run_id', 'job', 'ended_at', 'reason')} for r in runs if r['state'] == 'failed'],
                            sol=data('sol')), ('sol',))
    section('Finds waiting', state.get('finds_waiting', []))
    if 'day_close' in state.get('moments', []):
        evidence = state.get('day_close', {})
        section('Day close — Done today', dict(
            gsd_done=[f"{owner_time(instant(row['at']), '%H:%M')} done: {' '.join(row['title'].split())[:100]}"
                      for row in data('gsd')['done']] if data('gsd').get('done') is not None else 'unknown: gsd done not supplied',
            canceled_today=[f"{owner_time(instant(row['at']), '%H:%M')} canceled: {' '.join(row['title'].split())[:100]}"
                            for row in data('gsd')['canceled_today']]
                           if data('gsd').get('canceled_today') is not None else 'unknown: cancellation history not supplied',
            completion_coverage=f"gsd.worklist.v1 closure events from its 48-hour history, filtered to today in {local_zone().key.rsplit('/', 1)[-1]}; "
                                'a completion event is not proof the item is still closed.',
            blocks=[f"{r['title']} {datetime.fromisoformat(r['start']).astimezone(local_zone()):%H:%M}: {r['used']}"
                    for r in block_rows if 'items' in r
                    and memory.local_day(datetime.fromisoformat(r['start'])) == memory.local_day(now)]
                   or 'no blocks with items today',
            journal=evidence or 'unknown: day journal not supplied',
            interpretation='Run success is not proof of delivery; correlate records and sent messages. '
                           'Inbox handled is not proof of an answer; correlate replies. '
                           'Changes summarize keeps, moves and deletions; use steward journal for detail or ids.'), ('gsd',))
        section('Day close — Next two days', dict(
            through=end.isoformat(), calendar='See Calendar above; re-plan with current senses.',
            blocks='See Blocks above; explain moves rather than repeat Done today.'), ('calendars',))
        section('Day close — Horizon', dict(
            calendar='See Ahead for future events and its observed coverage.',
            gsd_deadlines=data('gsd').get('horizon_deadlines', 'unknown: gsd horizon not supplied'),
            visibility='Calendar sense covers only its stated window; outside it is unknown. '
                       'gsd covers dated worklist items, not unrecorded or undated obligations. '
                       'Nearer deadlines are in Deadlines above.'), ('calendars', 'gsd'))
        section('Day close — Long form', dict(
            sources='See Finds waiting, Inbox live pages, and Done today deliveries above.',
            action='Link the day’s Finds and capability pages; never paste long content. '
                   'Put day-close detail on a page with steward page put and link it from Today.'))
    health = data('health')
    section('Health', dict(use='Scheduling input only; never include health or sleep in the orientation.',
                           **{k: v for k, v in health.items() if k not in ('digest', 'week', 'stale')}), ('health',))
    section('Place', data('place'), ('place',))
    qmd = data('qmd')
    sources = {name: dict(current=s.get('current'), fresh_through=(datetime.fromtimestamp(s['fresh_through'], local_zone()).isoformat()
                if s.get('fresh_through') is not None else None)) for name, s in qmd.get('sources', {}).items()}
    verify = dict(qmd.get('verify') or {})
    if verify.get('at') is not None:
        verify['at'] = datetime.fromtimestamp(verify['at'], local_zone()).isoformat()
    section('Machine and qmd', dict(machine=data('machine'), qmd=dict(alive=qmd.get('alive'),
                                   verify=verify, sources=sources, new_items=qmd.get('new_items'))), ('machine', 'qmd'))
    output.append('\nRaw state remains behind `steward state`.')
    text = '\n'.join(output) + '\n'
    boundaries = list(starts.values()) + [len(text)]
    receipt.update(bytes=len(text.encode()), unknown_sections=unknown, section_unknowns=section_unknowns,
                   section_spans={name: dict(start=start, end=boundaries[i + 1])
                                  for i, (name, start) in enumerate(starts.items())},
                   verdicts=verdicts, dropped_keys=[])
    return text
