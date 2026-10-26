"""Data-only capabilities and versioned interests."""
import json
import os
import re
import shutil
import shlex
import subprocess

from . import contracts, jobs, mind, pages
from .state import StateError

# Async work runs on Sol by default, or on Haiku where a capability's work is mechanical
# (owner decision 2026-10-16). The Steward wake uses the same Opus configuration;
# opus runs a capability like the wake, at medium (owner direction 2026-10-18, consolidate).
AGENTS = {
    'sol': ['-a', 'codex', '-m', 'gpt-6.1-sol'],
    'haiku': ['-a', 'claude', '-m', 'claude-haiku-5-5'],
    'opus': ['-a', 'claude', '-m', 'claude-opus-5-5'],
}
# Sol and Haiku at high effort (owner decision 2026-10-16); the host's Codex default is medium.
VENDOR_ARGS = {
    'sol': ['--', '-c', 'model_reasoning_effort=high'],
    'haiku': ['--', '--permission-mode', 'bypassPermissions', '--effort', 'high'],
    'opus': ['--', '--permission-mode', 'bypassPermissions', '--effort', 'medium'],
}


PERMITS = {
    'draft': 'Also permitted: draft. Create a Gmail draft when you judge it serves the owner; never send.',
    'page': 'Also permitted: page put, using the same Steward command/state as above, within the charter.',
    'alert': 'Also permitted: notify Alerts, within the charter interrupt rule.',
    'health': 'Also permitted: state. Read-only producer status/telemetry CLI calls are allowed to verify post-fix health.',
    'fix': 'Also permitted: fix subcommands of the tools declared in calls. Their own code holds the allow-list and '
           'records each attempted/applied fix through the runtime CLI. Never bypass refusals or run raw upgrades. '
           'External effects are not reversible through runtime undo.',
}


# A permit brings the credential its verbs need; dispatch passes names, never values.
PERMIT_ENV = {
    'draft': ('GOG_KEYRING_PASSWORD',),
    'page': (pages.TOKEN_ENV,),
}
# Loaded at service start from the original files, not a second copy of each secret.
UNIT_CREDENTIALS = {
    'agent-secrets/credentials.env': PERMIT_ENV['draft'],
    'steward/pages.env': PERMIT_ENV['page'],
    'steward/telegram.env': ('STEWARD_TELEGRAM_BOT_TOKEN',),
}


def credential_names(name):
    if not name:
        return ()
    meta, _ = load(name)
    return tuple(dict.fromkeys(env for permit in meta.get('permits', [])
                               for env in PERMIT_ENV.get(permit, ())))


BASIC_TOOLS = ('ls', 'cat', 'head', 'tail', 'wc', 'sort', 'uniq', 'cut', 'grep', 'rg',
               'find', 'jq', 'sed', 'date', 'mkdir', 'cp', 'mv', 'tee', 'env', 'echo',
               'printf', 'true', 'false', 'test')


def shell_bin(directory, calls):
    """Per-run command discovery, not a sandbox against absolute-path execution."""
    bindir = directory / 'bin'
    bindir.mkdir(parents=True, exist_ok=True)
    for cli in dict.fromkeys([*BASIC_TOOLS, *calls]):
        target = shutil.which(cli)
        jobs.require(target is not None, 'missing CLI: ' + cli)
        jobs.require('/' not in cli and cli not in ('.', '..'), 'invalid CLI: ' + cli)
        if cli in BASIC_TOOLS:
            (bindir / cli).symlink_to(target)
            continue
        # A declared tool runs with the normal PATH (many start "#!/usr/bin/env python3");
        # only the model's own shell is limited to this directory.
        path = bindir / cli
        path.write_text('#!/bin/sh\nPATH=' + shlex.quote(os.environ.get('PATH', '/usr/bin:/bin')) +
                        '; export PATH\nexec ' + shlex.quote(target) + ' "$@"\n')
        path.chmod(0o755)
    return bindir


def shell_scope(model, directory, calls):
    """Restrict the vendor's tool shell, leaving tty and vendor startup PATH intact."""
    bindir = shell_bin(directory, calls)
    if model == 'sol':
        return ['-c', 'shell_environment_policy.inherit=all',
                '-c', 'shell_environment_policy.set.PATH=' + json.dumps(str(bindir)),
                '-c', 'allow_login_shell=false'], {}
    # Claude sources this file in Bash tools after its shell snapshot, before eval.
    # Unlike settings.env/PATH, it does not change the harness's subprocess lookup.
    path = directory / 'shell-env.sh'
    path.write_text('export PATH=' + shlex.quote(str(bindir)) + '\n')
    return [], {'CLAUDE_ENV_FILE': str(path)}


def load(name):
    jobs.require(isinstance(name, str) and re.fullmatch('[a-z0-9-]{1,40}', name), 'invalid capability')
    path = mind.directory() / 'capabilities' / (name + '.md')
    jobs.require(path.is_file(), 'missing capability: ' + str(path))
    lines = path.read_text().splitlines()
    jobs.require(lines and lines[0] == '---' and '---' in lines[1:], 'invalid capability frontmatter')
    end = lines.index('---', 1)
    meta = {}
    try:
        for line in lines[1:end]:
            key, value = line.split(':', 1)
            jobs.require(key not in meta, 'duplicate capability field')
            meta[key] = json.loads(value)
    except ValueError:
        raise StateError('invalid JSON capability frontmatter') from None
    jobs.require(set(meta) - {'context', 'permits'} == {'name', 'reads', 'calls', 'record'} and meta['name'] == name, 'invalid capability fields')
    jobs.require(isinstance(meta.get('context', []), list) and all(c in ('memory_due', 'provocation') for c in meta.get('context', [])), 'invalid capability context')
    jobs.require(isinstance(meta.get('permits', []), list) and all(isinstance(p, str) and p in PERMITS for p in meta.get('permits', [])), 'invalid capability permits')
    for key in ('reads', 'calls', 'record'):
        jobs.require(isinstance(meta[key], list) and all(isinstance(v, str) and v for v in meta[key]), 'invalid capability ' + key)
    jobs.require(all(re.fullmatch(r'(qmd|cli):[a-zA-Z0-9_-]+', r) for r in meta['reads']), 'invalid capability read source')
    return meta, '\n'.join(lines[end + 1:]).strip()


def require_draft_permit(db):
    """Wakes/owner keep their authority; job authority comes from its captured run."""
    if not any(key in os.environ for key in ('STEWARD_RUN_ID', 'STEWARD_JOB', 'STEWARD_RUN_DIR')):
        return
    run_id, name = os.environ.get('STEWARD_RUN_ID'), os.environ.get('STEWARD_JOB')
    jobs.require(run_id and name, 'draft requires STEWARD_RUN_ID and STEWARD_JOB')
    run = jobs.get(db, 'runs', run_id)
    job = jobs.get(db, 'jobs', name)
    jobs.require(run['job'] == job['name'] and run['state'] == 'started', 'invalid draft job context')
    capability = json.loads(run['spec_json']).get('capability')
    jobs.require(capability, 'draft requires a capability job')
    meta, _ = load(capability)
    jobs.require('draft' in meta.get('permits', []), 'capability lacks draft permit')


def validate(name, qmd_status=None):
    meta, _ = load(name)
    for cli in meta['calls'] + [r[4:] for r in meta['reads'] if r.startswith('cli:')]:
        jobs.require(shutil.which(cli), 'missing CLI: ' + cli)
    sources = [r[4:] for r in meta['reads'] if r.startswith('qmd:')]
    if sources:
        try:
            if qmd_status is None:
                result = subprocess.run(['qmd', 'status', '--json'], capture_output=True, text=True, timeout=30, check=True)
                raw = result.stdout
            else:
                raw = qmd_status()
            known = contracts.read('qmd.status.v1', raw)['sources']
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise StateError('qmd sources unknown') from None
        for source in sources:
            jobs.require(source in known, 'unknown qmd source: ' + source)
    return meta


def interest(db, action, name, spec=None, actor='owner'):
    jobs.require(isinstance(name, str) and re.fullmatch('[a-z0-9-]{1,40}', name), 'invalid interest name')
    with db:
        prior = next((r for r in jobs.current(db, 'interests') if r['name'] == name), None)
        if action == 'set':
            jobs.require(isinstance(spec, dict) and set(spec) - {'stage'} == {'topic', 'evidence', 'source', 'capability', 'refreshed_at'}, 'invalid interest spec')
            jobs.require('stage' not in spec or spec['stage'] in ('current', 'new', 'emerging'), 'invalid interest stage')
            jobs.require(isinstance(spec['topic'], str) and spec['topic'].strip(), 'interest topic required')
            evidence = spec['evidence']
            jobs.require(isinstance(evidence, list) and evidence and all(isinstance(i, str) and i.strip() for i in evidence), 'interest needs non-empty evidence ids')
            meta, _ = load(spec['capability'])
            jobs.require(spec['source'] in meta['reads'], 'interest source not declared by capability')
            jobs.require(all(i.startswith(spec['source'][4:] + ':') for i in evidence) if spec['source'].startswith('qmd:') else True, 'evidence source mismatch')
            jobs.instant(spec['refreshed_at'])
            row = dict(name=name, version=prior['version'] if prior else 0, state='active', spec_json=json.dumps(spec, sort_keys=True))
        else:
            jobs.require(prior is not None and prior['state'] != 'removed', 'unknown interest')
            row = dict(prior, state='removed')
        cid = jobs.change(db, 'interest ' + action, jobs.now_utc(), actor)
        jobs.append(db, 'interests', row, cid)
    return dict(change_id=cid, interest=name, status=row['state'])


def told_check(db, keys):
    from .notify import canonical_key
    # Sent keys live in told; only pending (including ambiguous sends) reserve an unsent key.
    return {key: bool(db.execute("SELECT 1 FROM told WHERE key=? UNION SELECT 1 FROM outbox_keys k JOIN outbox o ON o.id=k.outbox_id WHERE k.key=? AND o.state = 'pending'", (canonical_key(key), canonical_key(key))).fetchone()) for key in keys}
