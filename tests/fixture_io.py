"""Shared fake host, with a fail-closed backstop below the fake boundaries."""
from contextvars import ContextVar
from functools import wraps
import inspect
import os
from pathlib import Path
import shlex
import shutil
from types import SimpleNamespace
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
CONTRACT = ContextVar('installed_contract', default=None)
RUN = subprocess.run
HOME = Path.home()


# Kept for the process, so the stubs outlive every test and go when it ends.
FIXTURE_HOME = tempfile.TemporaryDirectory()


def fixture_bin():
    """A temp PATH holding the CLIs this host has but a container does not.

    `steward` runs this checkout (`python -m steward`), never an installed release, so a test
    cannot pass on whatever is deployed. A stub only stands in for a CLI no test runs.
    """
    bindir = Path(FIXTURE_HOME.name)
    steward = bindir / 'steward'
    steward.write_text('#!/bin/sh\nexport PYTHONPATH=' + shlex.quote(str(SOURCE / 'src')) +
                       '\nexec ' + shlex.quote(sys.executable) + ' -m steward "$@"\n')
    steward.chmod(0o755)
    for name in ('jq',):
        if shutil.which(name) is not None:
            continue
        stub = bindir / name
        stub.write_text('#!/bin/sh\necho "fixture ' + name + ': not implemented" >&2\nexit 1\n')
        stub.chmod(0o755)
    os.environ['PATH'] = str(bindir) + ':' + os.environ['PATH']


fixture_bin()


def temporary(path):
    path = Path(path).resolve()
    return path.is_relative_to('/tmp') and len(path.parts) > 2 and path.parts[2].startswith('tmp')


def installed_contract(producer, identity):
    """Opt in only to this named immutable --help/contract read, not producer data."""
    def decorate(test):
        @wraps(test)
        def run(*args, **kwargs):
            token = CONTRACT.set((producer, identity))
            try:
                return test(*args, **kwargs)
            finally:
                CONTRACT.reset(token)
        return run
    return decorate


def no_live_calls(event, args):
    if event == 'subprocess.Popen':
        executable, argv, cwd, env = args
        argv = [os.fsdecode(a) for a in argv]
        executable = os.fsdecode(executable)
        resolved = shutil.which(executable, path=(env or os.environ).get('PATH')) or executable
        contract = CONTRACT.get()
        if contract and argv in ([contract[0], '--help'], [contract[0], 'contract', contract[1]]):
            return
        if temporary(resolved):
            return  # Synthetic executables made by TemporaryDirectory fixtures.
        if Path(resolved).name == 'git' and '-C' in argv:
            if temporary(argv[argv.index('-C') + 1]) or argv[1:] == ['-C', str(SOURCE), 'rev-parse', 'HEAD']:
                return
        if executable in (sys.executable, '/bin/bash') and ('-c' in argv):
            return  # Local process-group and restricted-shell integration tests.
        if executable == str(SOURCE / 'jobs/backup') and '--no-upload' in argv and temporary(argv[argv.index('--state') + 1]):
            return
        if executable == str(SOURCE / 'jobs/agent-run') and temporary((env or {}).get('STEWARD_STATE', '/')):
            return
        raise AssertionError('live external CLI forbidden; inject a fake: ' + Path(executable).name)
    if event in ('os.system', 'os.posix_spawn'):
        raise AssertionError('live external process forbidden; inject a fake')
    if event == 'socket.connect' and args[1][0] not in ('127.0.0.1', '::1'):
        raise AssertionError('live network forbidden; inject a fake transport')
    if event in ('open', 'pathlib.Path.glob', 'pathlib.Path.rglob'):
        if isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).absolute()
            installed = HOME / '.local/lib/mech-d/current/contracts/intent-1.json'
            if path == installed and CONTRACT.get() != ('mech-d', 'intent-1'):
                raise AssertionError('installed contract read requires named opt-in')
            roots = [Path('/proc'), HOME / '.codex/sessions', HOME / '.claude/projects',
                     HOME / '.local/state/steward', HOME / '.local/share/steward']
            if any(path.is_relative_to(root) for root in roots):
                raise AssertionError('live host read forbidden; inject a fake: ' + str(path))


# Audit hooks also cover classes without a mind/settings fixture and cannot be
# bypassed by a captured subprocess.run default. Mocks never reach the OS hook.
sys.addaudithook(no_live_calls)


def seal(case):
    from unittest.mock import patch
    from tempfile import TemporaryDirectory
    from steward import archive, intent, runtime, senses
    if getattr(case, '_io_sealed', False):
        return
    case._io_sealed = True
    data = TemporaryDirectory()
    case.addCleanup(data.cleanup)
    case.enterContext(patch.dict(os.environ, STEWARD_DATA=data.name))

    def unavailable(argv):
        raise OSError('fixture producer unavailable')

    def journal(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, '', '')

    def native(argv, **kwargs):
        if argv[:2] == ['tty', 'show'] and not temporary(shutil.which('tty') or '/'):
            raise ValueError('fixture native session unavailable')
        if argv[0] == 'journalctl':
            return journal(argv, **kwargs)
        return RUN(argv, **kwargs)

    # Keep the real collectors/readers: only their external inputs are fake.
    for module, run in ((intent, journal), (archive, native)):
        case.enterContext(patch.object(module, 'subprocess', SimpleNamespace(**(vars(subprocess) | {'run': run}))))
    for function, replacements in (
        (runtime.tick, {'sense_runner': unavailable}),
    ):
        parameters = list(inspect.signature(function).parameters)
        names = parameters[-len(function.__defaults__):]
        defaults = tuple(replacements.get(name, value) for name, value in zip(names, function.__defaults__))
        case.enterContext(patch.object(function, '__defaults__', defaults))
    case.enterContext(patch('os.getloadavg', return_value=(0.1, 0.1, 0.1)))
    case.enterContext(patch('shutil.disk_usage', return_value=SimpleNamespace(free=20 * 1024**3)))
    # A release identity is the checkout's HEAD. A container given only the working tree of a
    # git worktree cannot read its gitdir, so that one read falls back to a fixture name; every
    # test that asserts the identity patches it itself.
    from steward import mind
    git = mind.git
    def release_git(root, *args):
        try:
            return git(root, *args)
        except subprocess.CalledProcessError:
            if args == ('rev-parse', 'HEAD') and Path(root).resolve() == SOURCE:
                return 'release-fixture'
            raise
    case.enterContext(patch.object(mind, 'git', release_git))
    read_text = Path.read_text
    def host_text(path, *args, **kwargs):
        if path == Path('/proc/meminfo'):
            return 'MemAvailable: 8388608 kB\n'
        if path == Path('/proc/self/cgroup'):
            return ''
        return read_text(path, *args, **kwargs)
    case.enterContext(patch.object(Path, 'read_text', host_text))
