"""Versioned quiet windows; the timer and promised work keep running."""
from datetime import timedelta
import uuid
from .jobs import append, change, current, instant, now_utc, require


def status(db, now):
    rows = sorted((r for r in current(db, 'tick_skips')
                   if r['state'] == 'active' and instant(r['to_at']) > now),
                  key=lambda r: instant(r['from_at']))
    return dict(current=next((r for r in rows if instant(r['from_at']) <= now), None),
                next=next((r for r in rows if instant(r['from_at']) > now), None))


def skip(db, from_at, to_at, reason, now=None, actor='owner'):
    now = now or now_utc()
    require(from_at and to_at, 'skip requires --from and --to')
    start, end = instant(from_at), instant(to_at)
    require(end > start, 'skip to must be after from')
    require(end > now, 'skip to must be in the future')
    require(end - start <= timedelta(hours=8), 'skip window exceeds 8 hours')
    require(isinstance(reason, str) and reason.strip(), 'skip reason required')
    with db:
        cid = change(db, 'tick skip', now, actor)
        row = append(db, 'tick_skips', dict(skip_id=uuid.uuid4().hex, state='active',
                     from_at=start.isoformat(), to_at=end.isoformat(), reason=reason), cid)
    return dict(change_id=cid, skip=row)
