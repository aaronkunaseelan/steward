"""Live policy files and their local git history."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import subprocess

from .state import StateError

_settings = ContextVar('mind_settings', default=None)


@contextmanager
def use(settings):
    token = _settings.set(settings)
    try:
        yield
    finally:
        _settings.reset(token)


def directory(settings=None):
    if settings is None:
        settings = _settings.get()
    if settings is None:
        from .runtime import config
        settings = config()
    return Path(settings.get('mind_dir', '~/.local/share/steward/mind')).expanduser()


def read(name, source=None):
    path = (Path(source) if source is not None else directory()) / name
    try:
        return path.read_text()
    except OSError as exc:
        raise StateError(f'cannot read mind file {path}: {exc.strerror}') from None


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def history(settings=None, commit=False):
    root = directory(settings)
    try:
        if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root.resolve():
            raise ValueError('mind must be its own repo')
        if commit:
            paths = git(root, 'status', '--porcelain')
            if paths:
                git(root, 'add', '-A')
                names = git(root, 'diff', '--cached', '--name-only')
                git(root, 'commit', '-m', 'mind: ' + ', '.join(names.splitlines()))
        sha, subject, when = git(root, 'log', '-1', '--format=%h%n%s%n%cI').splitlines()
        return dict(status='ok', sha=sha, subject=subject, when=when)
    except (OSError, subprocess.SubprocessError, ValueError):
        return dict(status='unknown', error=f'mind git {"commit/history" if commit else "history"} failed: {root}')
