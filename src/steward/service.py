"""Start a unit with only its listed credentials, read from their original files."""
import os
from pathlib import Path
import shlex
import sys

from .capabilities import UNIT_CREDENTIALS
from .settings import config_dir
from .state import StateError


def credentials(directory=None):
    directory = Path(directory) if directory is not None else config_dir().parent
    values = {}
    for filename, names in UNIT_CREDENTIALS.items():
        try:
            lines = (directory / filename).read_text().splitlines()
        except OSError:
            raise StateError('service credential file unavailable: ' + filename) from None
        for line in lines:
            name, sep, value = line.partition('=')
            name = name.strip()
            if not sep or name not in names:
                continue
            try:
                # Literal, single-line assignments, optionally quoted. Never source a
                # shell file or expand $variables/commands; ignore unlisted values.
                parts = shlex.split(value, comments=False)
                if len(parts) != 1 or not parts[0]:
                    raise ValueError()
            except ValueError:
                raise StateError('invalid service credential: ' + name) from None
            values[name] = parts[0]
        for name in names:
            if name not in values:
                raise StateError('missing service credential: ' + name)
    return values


def main():
    try:
        env = dict(os.environ, **credentials())
    except StateError as error:
        print(str(error), file=sys.stderr)
        return 1
    os.execvpe(sys.executable, [sys.executable, '-m', 'steward', *sys.argv[1:]], env)


if __name__ == '__main__':
    sys.exit(main())
