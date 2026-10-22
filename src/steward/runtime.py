"""Deterministic tick and isolated execution; external effects are injectable."""
from datetime import datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile

from .settings import local_zone
from .jobs import (UTC, append, change, current, duration, get, instant,
                   now_utc, plan, snapshot, COMPUTE_GRID_MINUTES)
from . import senses, mind, records, archive, timing
from .state import StateError, atomic_file, local_path, writer


# The Steward wake is Opus at medium effort (owner decision 2026-10-16): it is the one place
# where judgement about the owner's time is made. Async jobs use capabilities.AGENTS.
WAKE_AGENT = ['-a', 'claude', '-m', 'claude-opus-5-5']
WAKE_VENDOR_ARGS = ['--permission-mode', 'bypassPermissions', '--effort', 'medium',
                    '--exclude-dynamic-system-prompt-sections']


from .settings import config_dir, config, validate_settings


def data_root(value=None):
    return local_path(Path(value or os.environ.get('STEWARD_DATA', Path.home() / '.local/share/steward')))


def manifest_path(data, run):
    return data / 'runs' / run['job'] / run['run_id'] / 'manifest.json'


def failure_reason(code, directory):
    if code == 124:
        return 'exit 124: deadline exceeded'
    try:
        lines = (directory / 'stderr.txt').read_text(errors='replace').splitlines()
    except FileNotFoundError:
        lines = []
    cause = lines[-1].strip()[:160] if lines else ''
    return f'exit {code}' + (': ' + cause if cause else '')


def grid(dt, up):
    value = dt.timestamp() / (COMPUTE_GRID_MINUTES * 60)
    return datetime.fromtimestamp((math.ceil(value) if up else math.floor(value)) * COMPUTE_GRID_MINUTES * 60, UTC)


def expected_minutes(runs, name, spec):
    from .planner import expected
    return expected(runs, name, spec)


def placement(job, runs, now):
    spec = json.loads(job['spec_json'])
    needs = spec['needs']
    expected = timedelta(minutes=expected_minutes(runs, job['name'], spec))
    starts = [instant(r['started_at']) for r in runs if r['job'] == job['name'] and r['started_at']]
    from .planner import gap_floor, cadence_deadline
    deadline = cadence_deadline(job['name'], spec, runs)
    floor = gap_floor(runs, job['name'], spec)
    if 'every' in needs:
        at = grid(max(now, max(starts) + duration(needs['every']['min']) if starts else now), True)
        reason = 'default: every.min ' + needs['every']['min'] + ' after last start'
    elif 'max_staleness' in needs:
        ends = [instant(r['ended_at']) for r in runs if r['job'] == job['name'] and r['state'] == 'succeeded']
        at = max(now, grid(max(ends) + duration(needs['max_staleness']) - expected, False)) if ends else now
        reason = 'default: max_staleness ' + needs['max_staleness']
    else:
        candidates = []
        day = now.astimezone(local_zone()).date()
        # A deadline already served by a success in its freshness window (the 6 h before it,
        # as in the planner) needs no second run: plan for the next deadline instead.
        served = [instant(r['ended_at']) for r in runs if r['job'] == job['name'] and r['state'] == 'succeeded' and r['ended_at']]
        for offset in range(4):
            for clock in needs['ready_by']:
                from .timing import clock_on
                deadline = clock_on(day + timedelta(days=offset), clock)
                if deadline is None:
                    continue
                if timing.kept(served, deadline):
                    continue
                fresh, _ = timing.deadline_window(deadline)
                if any(r['job'] == job['name'] and r['state'] == 'failed' and r.get('retry_of')
                       and fresh <= instant(r['planned_for']) <= deadline for r in runs):
                    continue  # X-24: exhaustion must not become a fresh default for the same target.
                # Start early enough for one rerun, within the 6-h freshness window, so a
                # skipped or failed slot is retried at the next tick while the deadline can be met.
                fresh, latest = timing.deadline_window(deadline, expected)
                start = max(grid(timing.deadline_window(deadline, 2 * expected)[1], False), grid(fresh, True))
                if start < now and grid(now, True) <= latest:
                    start = grid(now, True)
                # min_gap may push a start later, but only while the deadline can still be met.
                if floor and start < floor:
                    start = grid(floor, True)
                    if start > latest:
                        continue
                if start >= now:
                    candidates.append(start)
        at = min(candidates)
        reason = 'default: ready_by minus twice the expected duration and 15m'
    if deadline and deadline < now:
        reason += '; recovering missed need at ' + deadline.isoformat()
    if floor and at <= grid(floor, True):
        at = grid(floor, True)
        reason += '; min_gap ' + spec['min_gap'] + ' after last success'
    if spec.get('after'):
        dependencies = [r for r in runs if r['job'] == spec['after'] and r['state'] in ('planned', 'started')]
        if not dependencies:
            return None, 'default: waiting for dependency reservation'
        dep = min(dependencies, key=lambda r: r['planned_for'])
        dep_spec = json.loads(dep['spec_json'])
        end = instant(dep['started_at'] or dep['planned_for']) + timedelta(minutes=expected_minutes(runs, dep['job'], dep_spec))
        at = max(at, end)
        reason += '; after ' + spec['after']
    # Default placement serializes heavy reservations, without moving existing ones.
    if spec['heavy']:
        for other in sorted(runs, key=lambda r: r['planned_for']):
            other_spec = json.loads(other['spec_json'])
            if other['state'] in ('planned', 'started') and other_spec['heavy']:
                start = instant(other['started_at'] or other['planned_for'])
                end = start + timedelta(minutes=other_spec['max_minutes'])
                if at < end and start < at + timedelta(minutes=spec['max_minutes']):
                    at = grid(end, True)
    return at, reason


def heavy_locked():
    try:
        with open('/run/lock/qmd-batch-heavy.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
    except OSError:
        return True  # unknown lock state cannot authorize a heavy start


def host_load():
    return os.getloadavg()[1], shutil.disk_usage('/').free


def launch(argv):
    # batch-run only waits for systemd's acknowledgement, not the job lifetime.
    result = subprocess.run(argv, capture_output=True, timeout=30)
    if result.returncode:
        raise StateError('batch launch failed')


def unit_alive(unit):
    result = subprocess.run(['systemctl', 'show', unit, '--property=ActiveState', '--value'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        return None  # an unreadable manager is unknown, not a disappeared unit
    return result.stdout.strip() in ('active', 'activating', 'reloading', 'deactivating')


def start_wake():
    subprocess.run(['systemctl', '--user', 'start', '--no-block', 'steward-wake.service'], capture_output=True, timeout=10, check=True)


def unseen_results(db, cursor):
    # A wake is for news: a run that succeeded went as Steward planned, and its output that
    # matters arrives through its own channel (gsd changed, Finds digest, the next briefing).
    return db.execute("SELECT 1 FROM runs r JOIN changes c ON c.id=r.change_id WHERE c.rowid>? AND r.state='failed' LIMIT 1", (cursor,)).fetchone() is not None


def gate(db, now=None):
    from . import messages
    if messages.inbox(db):
        return 'owner message'
    from . import calendar_b
    if now is not None and timing.due(db, now):
        return ', '.join(timing.due(db, now))
    previous = db.execute("SELECT * FROM wakes WHERE harness_run_id IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    cursor = previous['change_cursor'] if previous else 0
    rows = db.execute("SELECT verb,actor FROM changes WHERE rowid>?", (cursor,)).fetchall()
    reasons = []
    if unseen_results(db, cursor):
        reasons.append('run failures')
    # A manual run is the owner's own work, like its landed gsd changes (senses.save).
    if any(r['actor'] not in ('model', 'tick', 'migration') and r['verb'] != 'run now' for r in rows):
        reasons.append('non-model changes')
    reasons.extend(name + ' changed' for name in senses.events(db, cursor))
    return ', '.join(reasons) or NOTHING_NEW


NOTHING_NEW = 'nothing new'

DEFAULT_WAKE_TIMES = [f'{h:02}:00' for h in range(9, 22)] + ['01:00', '05:00']


def latest_wake(now, settings):
    """Compare real instants, including both occurrences of an autumn wall time."""
    slots = []
    today = now.astimezone(local_zone()).date()
    for days_ago in (0, 1, 2):
        day = today - timedelta(days=days_ago)
        for value in settings.get('wake_times', DEFAULT_WAKE_TIMES):
            for fold in (0, 1):
                candidate = timing.clock_on(day, value, fold)
                if candidate is not None and candidate <= now:
                    slots.append(candidate)
    return max(slots) if slots else None


def release_sha():
    """The loaded archive, not the mutable current symlink (or git in a checkout)."""
    source = Path(__file__).resolve().parents[2]
    if source.parent.name == 'releases':
        return source.name
    return mind.git(source, 'rev-parse', 'HEAD')


def log_revisions(db, policy, now):
    revisions = {'release': release_sha()}
    if policy and policy['status'] == 'ok':
        revisions['mind'] = policy['sha']
    for verb, sha in revisions.items():
        previous = db.execute('SELECT summary FROM changes WHERE verb=? ORDER BY rowid DESC LIMIT 1', (verb,)).fetchone()
        if previous is None or previous[0] != sha:
            cid = change(db, verb, now, 'tick')
            db.execute('UPDATE changes SET summary=? WHERE id=?', (sha, cid))


def tick(root, data=None, clock=now_utc, load_reader=host_load, lock_reader=heavy_locked,
         launcher=launch, unit_reader=unit_alive, wake_starter=start_wake,
         settings=None, sense_runner=senses.run, rollout_root=None,
         sender=None, update_fetcher=None, telegram=None, pages_transport=None):
    settings = config() if settings is None else validate_settings(settings)
    local_zone()  # Required before tick effects; the loader owns the zone object.
    now = clock()
    data = data_root(data)
    # 2 cores, batch jobs at CPUWeight 10: 3.0 marks a host that is busy, not one at its usual load.
    load_max = settings.get('load_max', 3.0)
    result = dict(at=now.isoformat(), started=[], refused=[], folded=[], mind=None)
    event_wake = False
    from .tick_skips import status
    # Senses and the mind commit stay outside the writer lock, as before skips existed.
    qmd_cache = []
    original_runner = sense_runner
    def cached_runner(argv):
        if argv != ['qmd', 'status', '--json']:
            return original_runner(argv)
        if not qmd_cache:
            try:
                qmd_cache.append(original_runner(argv))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                qmd_cache.append(exc)
        if isinstance(qmd_cache[0], Exception):
            raise qmd_cache[0]
        return qmd_cache[0]
    observations = senses.collect(root, settings, now, cached_runner, rollout_root)
    result['mind'] = mind.history(settings, commit=True)
    with writer(root) as store:
        db = store.db
        from .jobs import validate_spec
        invalid_specs = {}
        for job in current(db, 'jobs'):
            if job['state'] == 'active':
                try:
                    validate_spec(db, json.loads(job['spec_json']), job['name'],
                                  qmd_status=lambda: cached_runner(['qmd', 'status', '--json']))
                except (StateError, ValueError, TypeError, KeyError) as exc:
                    invalid_specs[job['name']] = str(exc).splitlines()[0]
        result['invalid_specs'] = invalid_specs
        from . import messages
        from .notify import telegram_sender, updates
        sender = sender or telegram_sender
        talk = messages.poll(db, now, settings, sender, update_fetcher or updates, telegram)
        from . import pages
        result['pages_poll_ok'] = pages.poll(db, now, pages_transport)
        # A skip suppresses wakes only; deterministic work continues normally.
        skipping = status(db, now)['current']
        with db:
            log_revisions(db, result['mind'], now)
            cid = change(db, 'tick', now, 'tick')
            senses.save(db, {'job_specs': dict(status='ok', data=invalid_specs)}, now, cid)
        if skipping:
            result['skipping'] = skipping['to_at']
        from . import memory
        memory.file_days(db, now)
        memory.provocations(db, now, source=mind.directory(settings))
        with db:
            senses.save(db, observations, now, cid)
            calendar = observations.get('calendars', {})
            if calendar.get('status') == 'ok':
                from . import calendar_b
                learned = calendar_b.reconcile(db, calendar['data'], settings, cid)
                result['calendar_learned'] = learned
                if learned:
                    db.execute("UPDATE senses SET event=1 WHERE name='calendars' AND change_id=?", (cid,))
            for row in current(db, 'runs'):
                if row['state'] != 'started':
                    continue
                path = manifest_path(data, row)
                spec = json.loads(row['spec_json'])
                manifest = None
                if path.exists():
                    try:
                        candidate = json.loads(path.read_text())
                        ended = instant(candidate['ended_at'])
                        started = instant(candidate['started_at'])
                        if (candidate['run_id'] == row['run_id'] and candidate['job'] == row['job']
                                and type(candidate['exit_code']) is int
                                and candidate['status'] == ('succeeded' if candidate['exit_code'] == 0 else 'failed')
                                and ended >= started):
                            manifest = candidate
                    except (ValueError, KeyError, TypeError):
                        pass  # invalid manifests remain unknown until unit reconciliation
                if manifest:
                    row.update(learned_cost(manifest, path.parent))
                    row.update(state=manifest['status'], exit_code=manifest['exit_code'], ended_at=ended.isoformat(), manifest=str(path))
                    if manifest['exit_code']:
                        row['reason'] = failure_reason(manifest['exit_code'], path.parent)
                elif now > instant(row['started_at']) + timedelta(minutes=spec['max_minutes'] + 5) and unit_reader(row['unit']) is False:
                    row.update(state='failed', ended_at=now.isoformat(), reason='batch unit gone without valid manifest')
                else:
                    continue
                append(db, 'runs', row, cid)
                result['folded'].append(row['run_id'])
                job = get(db, 'jobs', row['job'])
                if (row['state'] == 'failed' and not row['retry_of'] and job['state'] == 'active' and job['name'] not in invalid_specs
                        and json.loads(job['spec_json'])['needs'].get('ready_by')):
                    plan(db, row['job'], now, 'retry of run ' + row['run_id'], now, cid,
                         move=False, retry_of=row['run_id'])
            # Multiple passes allow dependencies added before their parent to obtain a reservation.
            jobs = current(db, 'jobs')
            for _ in range(len(jobs)):
                made = False
                for job in jobs:
                    if job['name'] in invalid_specs:
                        continue
                    spec = json.loads(job['spec_json'])
                    runs = current(db, 'runs')
                    if job['state'] != 'active' or not spec['recurring'] or any(r['job'] == job['name'] and r['state'] in ('planned', 'started') for r in runs):
                        continue
                    at, reason = placement(job, runs, now)
                    if at is not None:
                        from .planner import rule
                        if at > now + timedelta(hours=36):
                            # Not due inside the planning window: nothing is wrong, it is planned later.
                            result.setdefault('later', {})[job['name']] = at.isoformat()
                            continue
                        # The feasible time nearest the default, so a refused default never
                        # collapses to "now" and reruns a job that was just served.
                        slots = [grid(now, True) + timedelta(minutes=COMPUTE_GRID_MINUTES*i) for i in range(109)]
                        candidates = [at] + sorted(slots, key=lambda t: abs(t - at)) + [now]
                        default_at = at
                        at = next((t for t in candidates if rule(job, runs, t, now) is None), None)
                        if at is None:
                            result.setdefault('unplanned', {})[job['name']] = 'no feasible time'
                            continue
                        if at != default_at:
                            reason = 'default: feasible alternative; original placement violates calendar C constraints'
                        try:
                            plan(db, job['name'], at, reason, now, cid, churn_cap=settings.get('churn_cap', 6))
                            made = True
                        except StateError as exc:
                            result.setdefault('unplanned', {})[job['name']] = str(exc)
                if not made:
                    break
        # Commit started intent before crossing the process boundary. The lock serializes ticks.
        for row in current(db, 'runs'):
            if row['state'] != 'planned' or instant(row['planned_for']) > now:
                continue
            job = get(db, 'jobs', row['job'])
            reason = invalid_specs.get(row['job'])
            spec = {} if reason else json.loads(job['spec_json'])
            try:
                load, free = load_reader()
                if not math.isfinite(load) or not math.isfinite(free) or load > load_max or free < 8 * 1024**3:
                    reason = 'host-load guard'
            except OSError:
                reason = 'host-load guard unknown'
            if job['state'] != 'active':
                reason = 'job is not active'
            elif reason is None and spec['heavy'] and (any(r['state'] == 'started' and json.loads(r['spec_json'])['heavy'] for r in current(db, 'runs')) or lock_reader()):
                reason = 'heavy lane busy or unknown'
            with db:
                if reason:
                    row.update(state='refused', reason=reason, ended_at=now.isoformat())
                    append(db, 'runs', row, cid)
                    result['refused'].append(row['run_id'])
                    continue
                name = 'steward-' + row['job'] + '-' + row['run_id']
                row.update(state='started', started_at=now.isoformat(), unit='batch-' + name + '.service',
                           spec_json=json.dumps(spec, sort_keys=True))
                append(db, 'runs', row, cid)
            # run-exec owns the deadline (TERM, grace, KILL, manifest); the unit's limit
            # is only a backstop after it, or systemd would kill run-exec first.
            argv = ['batch-run'] + (['--heavy'] if spec['heavy'] else []) + ['--name', name, '--max', str(spec['max_minutes'] + 5) + 'm']
            if spec.get('env_file'):
                argv += ['--env-file', spec['env_file']]
            # batch-run does not preserve PYTHONPATH, so explicitly bind the current code root.
            argv += ['--', '/usr/bin/env', 'PYTHONPATH=' + str(Path(__file__).resolve().parents[1]), sys.executable, '-m', 'steward', 'run-exec', row['run_id'], '--state', str(root), '--data', str(data)]
            try:
                launcher(argv)
                result['started'].append(row['run_id'])
            except (OSError, StateError, subprocess.SubprocessError):
                with db:
                    row = get(db, 'runs', row['run_id'])
                    row.update(state='failed', ended_at=now.isoformat(), reason='batch launch failed')
                    append(db, 'runs', row, cid)
        if result.get('calendar_learned'):
            messages.notify(db, 'Changes', 'change', '\n'.join(result['calendar_learned']),
                            'calendar-learning:' + cid, sender, now, 'tick', settings=settings, change_id=cid)
        # Dispatcher effects are not told: placements and launches show in Today,
        # a skip is the guards working, and repeated failures reach Alerts.
        messages.alerts(db, now, sender, settings)
        messages.flush(db, sender, now, settings)
        previous = db.execute("SELECT started_at FROM wakes WHERE status!='refused' ORDER BY id DESC LIMIT 1").fetchone()
        last_model = db.execute('SELECT change_cursor FROM wakes WHERE harness_run_id IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
        unseen = unseen_results(db, last_model['change_cursor'] if last_model else 0)
        unseen = unseen or bool(senses.events(db, last_model['change_cursor'] if last_model else 0))
        event_wake = bool(unseen and (not previous or now - instant(previous['started_at']) >= timedelta(minutes=20)))
        moment_wake = bool(timing.due(db, now)) and (not previous or now - instant(previous['started_at']) >= timedelta(minutes=20))
        event_wake = event_wake or moment_wake or talk or bool(messages.inbox(db))
    result['archive'] = archive.sweep(root, data, now=now)
    scheduled = latest_wake(now, settings)
    event_wake = bool(not skipping and event_wake)
    scheduled_wake = bool(not skipping and scheduled is not None and (not previous or scheduled > instant(previous['started_at'])))
    result['scheduled_wake'] = scheduled_wake
    result['wake'] = 'scheduled' if scheduled_wake else 'event' if event_wake else None
    if event_wake or scheduled_wake:
        wake_starter()
    return dict(result, change_id=cid)


def stop(proc, grace=60):
    # TERM first so the job's finalizers run and it can write a last record;
    # KILL only what is still there after the grace.
    import signal
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def execute(root, run_id, data=None, clock=now_utc):
    # The launching tick may still hold writer.lock. Read its committed intent
    # without taking that lock; the run's captured input is immutable.
    db = sqlite3.connect(f'{(local_path(root) / "state.sqlite").as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = get(db, 'runs', run_id)
    finally:
        db.close()
    if row['state'] != 'started':
        raise StateError('run is not started')
    directory = manifest_path(data_root(data), row).parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (directory / 'exec.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_executing', 'run_id': run_id}
        path = directory / 'manifest.json'
        if path.exists():
            return {'status': 'already_executed', 'run_id': run_id}
        spec = json.loads(row['spec_json'])
        start = clock()
        env = dict(os.environ, STEWARD_RUN_DIR=str(directory), STEWARD_ACTOR='model', STEWARD_DELIVER=spec['deliver'], STEWARD_STATE=str(root), STEWARD_JOB=row['job'], STEWARD_RUN_ID=run_id)
        code = 127
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(spec['argv'], cwd=spec.get('cwd', str(Path.home())), env=env, stdout=out, stderr=err, start_new_session=True)
                try:
                    code = proc.wait(timeout=spec['max_minutes'] * 60)
                except subprocess.TimeoutExpired:
                    stop(proc)
                    code = 124
            except OSError:
                pass
            for stream, name in ((out, 'stdout.txt'), (err, 'stderr.txt')):
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 65536))
                atomic_file(directory / name, stream.read())
        end = clock()
        links = []
        if (directory / 'links.json').exists():
            try:
                links = json.loads((directory / 'links.json').read_text())
            except ValueError:
                links = []
        _, record_errors = records.inspect(directory)
        manifest = dict(job=row['job'], run_id=run_id, host=socket.gethostname(), argv=spec['argv'], cwd=spec.get('cwd', str(Path.home())), started_at=start.isoformat(), ended_at=end.isoformat(), exit_code=code, status='succeeded' if code == 0 else 'failed', duration=(end - start).total_seconds(), links=links, record_errors=record_errors)
        atomic_file(path, json.dumps(manifest, sort_keys=True).encode())
        archived = archive_attempt(archive.job, data_root(data), row, manifest=manifest, now=end)
        return {'run_id': run_id, 'status': manifest['status'], 'exit_code': code, 'archive': archived}


def harness(argv):
    # tty's recorded worker has a separate session from the foreground waiter.
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=dict(os.environ, STEWARD_ACTOR='model'))
    try:
        output, _ = process.communicate(timeout=38 * 60)
    except subprocess.TimeoutExpired:
        process.terminate()
        output, _ = process.communicate(timeout=10)
        tty_root = Path(os.environ.get('TTY_STATE', '~/.local/state/tty')).expanduser()
        run_id = None
        for path in (tty_root / 'runs').glob('*/meta.json'):
            meta = json.loads(path.read_text())
            if meta.get('cwd') == argv[argv.index('-C') + 1]:
                run_id = meta['run']
                subprocess.run(['tty', 'stop', run_id], capture_output=True, timeout=30)
                break
        error = StateError('wake harness timed out')
        error.run_id = run_id
        raise error from None
    try:
        result = json.loads(output)
        run_id = result.get('run') or result.get('run_id')
        if not isinstance(run_id, str) or not run_id:
            raise ValueError
        return run_id
    except (ValueError, AttributeError):
        raise StateError('wake harness did not return a run id') from None


def wake(root, clock=now_utc, runner=harness, settings=None, prompt=None, sender=None,
         attachment_downloader=None, qmd_runner=None):
    root = local_path(root)
    with (root / 'wake.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            now = clock()
            with writer(root, wait=120) as store:
                with store.db:
                    wake_id = store.db.execute(
                        "INSERT INTO wakes(started_at,status,checkpoint,gate_reason) VALUES (?,'refused',?,'wake lock busy')",
                        (now.isoformat(), json.dumps(dict(model_calls=0, ended_at=now.isoformat())))).lastrowid
            return {'wake_id': wake_id, 'status': 'refused', 'model_calls': 0}
        now = clock()
        settings = config() if settings is None else settings
        from . import messages, telegram_files
        messages.read_attachments(root, attachment_downloader or telegram_files.download,
                                  qmd_runner or subprocess.run)
        with writer(root) as store:
            db = store.db
            from .notify import telegram_sender
            sender = sender or telegram_sender
            ritual = 'orientation' in timing.undelivered(db, now)
            reason = gate(db, now)
            cursor = db.execute('SELECT coalesce(max(rowid),0) FROM changes').fetchone()[0]
            with db:
                wake_id = db.execute("INSERT INTO wakes(started_at,status,checkpoint,gate_reason,change_cursor) VALUES (?,'gated','{}',?,?)", (now.isoformat(), reason, cursor)).lastrowid
            state = snapshot(db, now, settings.get('interrupt_cap', 3))
            state['inbox'] = messages.inbox(db)
            from . import calendar_b
            state['rituals'] = timing.undelivered(db, now)
            state['calendar_b'] = calendar_b.briefing(db, now)
            state['finds_waiting'] = messages.queued(db)
            from . import pages
            state['live_pages'] = pages.live(db, now)
        directory = data_root() / 'wakes' / str(wake_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = Path(__file__).resolve().parents[2]
        prompt = prompt if prompt is not None else (source / 'prompts/wake.md').read_text()
        from . import memory
        briefing_info = {}
        with writer(root) as store:
            try:
                stable = memory.system_prompt(prompt, source=mind.directory(settings))
                text = memory.briefing(store.db, now, dict(state, gate_reason=reason), receipt=briefing_info)
                briefing_info['system_bytes'] = len(stable.encode())
            except StateError as exc:
                briefing_info.update(complete=False, error=str(exc))
                with store.db:
                    store.db.execute("UPDATE wakes SET status='failed',checkpoint=? WHERE id=?",
                                     (json.dumps({'briefing': briefing_info}), wake_id))
                raise
            with store.db:
                store.db.execute('UPDATE wakes SET checkpoint=? WHERE id=?',
                                 (json.dumps({'briefing': briefing_info}), wake_id))
        atomic_file(directory / 'briefing.md', text.encode())
        calls = 0
        status = 'skipped'
        run_id = None
        if settings.get('wake_model') is True and reason != NOTHING_NEW:
            calls = 1
            status = 'completed'
            try:
                run_id = runner(['tty', 'run', *WAKE_AGENT, '--no-accept', '-C', str(directory), str(directory / 'briefing.md'), '--', *WAKE_VENDOR_ARGS, '--append-system-prompt', stable])
            except (OSError, StateError, subprocess.SubprocessError) as exc:
                run_id = getattr(exc, 'run_id', None)
                status = 'failed'
        now = clock()
        with writer(root) as store:
            db = store.db
            with store.db:
                store.db.execute('UPDATE wakes SET status=?,checkpoint=?,harness_run_id=? WHERE id=?', (status, json.dumps({'model_calls': calls, 'ended_at': now.isoformat(), 'briefing': briefing_info}), run_id, wake_id))
            messages.changes(db, now, sender, settings, key=f'wake-changes:{wake_id}')
            if ritual and 'orientation' in timing.undelivered(db, now):
                # A pending orientation is already told (told_check), so a retry never enqueues a second.
                messages.notify(db, 'Today', 'result', messages.fallback(db, now), sender=sender,
                                now=now, actor='tick', settings=settings, moment='orientation')
            messages.alerts(db, now, sender, settings)
            messages.flush(db, sender, now, settings)
            row = dict(db.execute('SELECT * FROM wakes WHERE id=?', (wake_id,)).fetchone())
        archived = archive_attempt(archive.wake, data_root(), row, now=now)
        return dict(wake_id=wake_id, gate_reason=reason, status=status, model_calls=calls, harness_run_id=run_id, archive=archived)


def archive_attempt(archiver, *args, **kwargs):
    # A failed copy must not change the completed run's outcome. Tick retries it.
    try:
        return 'complete' if archiver(*args, **kwargs)['complete'] else 'pending'
    except (OSError, ValueError, KeyError, TypeError):
        return 'error'


def learned_cost(manifest, directory=None):
    minutes = (instant(manifest['ended_at']) - instant(manifest['started_at'])).total_seconds() / 60
    tokens = []
    root = Path(os.environ.get('TTY_STATE', '~/.local/state/tty')).expanduser() / 'runs'
    links = manifest.get('links', [])
    if isinstance(links, dict):
        links = [links]
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get('tty_run'), str):
            continue
        identity = link['tty_run']
        if Path(identity).name != identity or identity in ('.', '..'):
            continue
        try:
            result = json.loads((root / identity / 'result.json').read_text())
            usage = result['usage']
            values = [usage['input_tokens'], usage['output_tokens']]
            if all(type(v) is int and v >= 0 for v in values):
                tokens.append(sum(values))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Jobs without tty report weighted_tokens; validate each key, not the whole record's cost.
    if directory is not None:
        reported, _ = records.inspect(directory)
        tokens.extend(reported)
    return dict(cost_minutes=minutes, cost_tokens=sum(tokens) if tokens else None)
