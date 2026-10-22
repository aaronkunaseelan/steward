"""Load shipped contracts and report mismatches without echoing payload values."""
import json
from pathlib import Path

from .contract import problems

DIRECTORY = Path(__file__).resolve().parents[2] / 'contracts'


class ContractError(ValueError):
    pass


def load(identity, vendor=False):
    return json.loads((DIRECTORY / ('vendor' if vendor else '') / (identity + '.json')).read_text())


def issues(schema, value):
    # The reference validator includes the rejected value in enum errors; never retain it.
    return [p.split(': ', 1)[0] + ': unexpected enum value' if ' not in ' in p else p
            for p in problems(schema, value)]


def read(identity, output):
    try:
        value = json.loads(output)
    except ValueError:
        raise ContractError(identity + ': invalid JSON') from None
    errors = issues(load(identity, vendor=True), value)
    if errors:
        raise ContractError(identity + ': ' + '; '.join(errors[:3]))
    return value
