"""Generic audit records for effects owned by external tools, not undo plans."""
from . import jobs


def record(db, subject, outcome, actor='owner'):
    for value in (subject, outcome):
        jobs.require(isinstance(value, str) and value.strip() and
                     not any(ord(c) < 32 or ord(c) == 127 for c in value),
                     'external change needs a nonempty single-line subject and outcome')
    with db:
        identity = jobs.change(db, 'external-change', jobs.now_utc(), actor=actor)
        db.execute('UPDATE changes SET summary=? WHERE id=?', (subject.strip()+': '+outcome.strip(), identity))
    return dict(change_id=identity, subject=subject.strip(), outcome=outcome.strip(), undoable=False)
