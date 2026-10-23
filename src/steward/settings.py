"""Strict host configuration and shared local timezone."""
from contextvars import ContextVar
from functools import lru_cache
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .state import StateError

_active = ContextVar("steward_settings", default=None)


@lru_cache(maxsize=None)
def zone(name):
    return ZoneInfo(name)


def config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'steward'


def validate_settings(settings):
    strings = {'mind_dir', 'gmail_account', 'schedule_calendar_id', 'timezone', 'pages_host', 'backup_remote'}
    lists = {'main_calendar_ids', 'qmd_watch_sources', 'wake_times'}
    allowed = strings | lists | {'wake_model', 'load_max', 'interrupt_cap'}
    if not isinstance(settings, dict):
        raise StateError('invalid settings table')
    for key, value in settings.items():
        if key not in allowed:
            raise StateError('unknown setting: ' + key)
        valid = True
        if key in strings:
            valid = isinstance(value, str) and bool(value.strip())
        elif key in lists:
            valid = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
            if valid and key == 'wake_times':
                import re
                valid = all(re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', v) for v in value)
        elif key == 'wake_model':
            valid = type(value) is bool
        elif key == 'load_max':
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        else:
            valid = type(value) is int and value >= 0
        if not valid:
            raise StateError('invalid setting: ' + key)
    if 'timezone' in settings:
        try:
            zone(settings['timezone'])
        except (ZoneInfoNotFoundError, ValueError):
            raise StateError('invalid setting: timezone') from None
    if 'pages_host' in settings:
        try:
            host = urlsplit(settings['pages_host'])
            valid = (host.scheme == 'https' and host.hostname and not host.username
                     and not host.password and not host.path and not host.query
                     and not host.fragment and host.port in (None, 443))
        except ValueError:
            valid = False
        if not valid:
            raise StateError('invalid setting: pages_host')
    if 'timezone' in settings:
        _active.set(settings)
    return settings


def config(path=None):
    path = path or config_dir() / 'config.toml'
    try:
        settings = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise StateError('missing settings file: ' + str(path)) from None
    except tomllib.TOMLDecodeError:
        raise StateError('invalid settings TOML') from None
    settings = validate_settings(settings)
    _active.set(settings)
    return settings


def required(key, settings=None):
    settings = settings if settings is not None else _active.get()
    if settings is None:
        settings = config()
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StateError('missing setting: ' + key)
    return value


def local_zone():
    return zone(required('timezone'))


def pages_host():
    return required('pages_host')
