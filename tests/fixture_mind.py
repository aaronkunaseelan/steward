import os
from pathlib import Path
import shutil
import tomllib
from steward.settings import zone

SOURCE = Path(__file__).resolve().parents[1]
TEST_CONFIG = (Path(__file__).parent / 'fixtures/config.toml').read_text()
TEST_ZONE = zone(tomllib.loads(TEST_CONFIG)['timezone'])
from unittest.mock import patch
from fixture_io import seal


def use_settings(case):
    seal(case)
    case.enterContext(patch.dict(os.environ, STEWARD_ACTOR='owner'))
    from steward.settings import _active
    token = _active.set(tomllib.loads(TEST_CONFIG))
    case.addCleanup(_active.reset, token)


def command(executable='/bin/true'):
    from steward.jobs import RELEASE_ROOT
    return str(RELEASE_ROOT / 'steward/releases/test' / Path(executable).name)


def install_release(case, root):
    use_settings(case)
    import sys
    release_root = root / '.local/lib'
    release = release_root / 'steward/releases/test'
    release.mkdir(parents=True, exist_ok=True)
    for executable in ('/bin/true', '/bin/false', '/bin/echo', sys.executable):
        target = release / Path(executable).name
        target.write_text('#!/bin/sh\nexec ' + executable + ' "$@"\n')
        target.chmod(0o755)
    guard = patch('steward.jobs.RELEASE_ROOT', release_root)
    guard.start()
    case.addCleanup(guard.stop)


def install(case, root):
    install_release(case, root)
    mind = root / 'mind'
    shutil.copytree(Path(__file__).parent / 'fixtures/mind', mind)
    config = root / 'config/steward'
    config.mkdir(parents=True)
    (config / 'config.toml').write_text(f'mind_dir = "{mind}"\n' + TEST_CONFIG)
    guard = patch.dict(os.environ, XDG_CONFIG_HOME=str(config.parent), STEWARD_DATA=str(root / 'data'))
    guard.start()
    case.addCleanup(guard.stop)
    from steward import settings
    token = settings._active.set(None)
    case.addCleanup(settings._active.reset, token)
    settings.config()
    guard = patch('steward.archive.completion_times', return_value={})
    guard.start()
    case.addCleanup(guard.stop)
    objects = {}
    def pages_request(method, path, **kw):
        if path.startswith('/_api/pages/'):
            if method == 'PUT':
                objects[path] = kw['data']
                return 204, b''
            if method == 'GET':
                return (200, objects[path]) if path in objects else (404, b'')
            if method == 'DELETE':
                objects.pop(path, None)
                return 204, b''
        return 200, b'{"keys":[],"next":null}'
    guard = patch('steward.pages.request', side_effect=pages_request)
    guard.start()
    case.addCleanup(guard.stop)
    # Explicit settings={} must resolve the fixture mind, not the host default.
    from steward import mind as module
    original = module.directory
    guard = patch.object(module, 'directory', side_effect=lambda settings=None: mind if settings == {} or (settings is not None and 'mind_dir' not in settings) else original(settings))
    guard.start()
    case.addCleanup(guard.stop)
    return mind
