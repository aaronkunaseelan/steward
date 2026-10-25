"""Shared fake host, with a fail-closed backstop below the fake boundaries."""
from contextvars import ContextVar
from functools import wraps
import inspect
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
CONTRACT = ContextVar('installed_contract', default=None)
RUN = subprocess.run
HOME = Path.home()


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
        (runtime.tick, {'sense_runner': unavailable, 'rollout_root': SOURCE / 'tests/no-rollouts'}),
        (senses.collect, {'rollout_root': SOURCE / 'tests/no-rollouts'}),
    ):
        parameters = list(inspect.signature(function).parameters)
        names = parameters[-len(function.__defaults__):]
        defaults = tuple(replacements.get(name, value) for name, value in zip(names, function.__defaults__))
        case.enterContext(patch.object(function, '__defaults__', defaults))
    case.enterContext(patch('os.getloadavg', return_value=(0.1, 0.1, 0.1)))
    case.enterContext(patch('shutil.disk_usage', return_value=SimpleNamespace(free=20 * 1024**3)))
    read_text = Path.read_text
    def host_text(path, *args, **kwargs):
        if path == Path('/proc/meminfo'):
            return 'MemAvailable: 8388608 kB\n'
        if path == Path('/proc/self/cgroup'):
            return ''
        return read_text(path, *args, **kwargs)
    case.enterContext(patch.object(Path, 'read_text', host_text))
