"""Validate reserved record keys; a bad key never discards other valid costs."""
import json
import math

from .contracts import issues, load

SCHEMA = load('steward.record.v1')


def reflections(manifest):
    """Read job-authored words through the record contract, not job internals."""
    from pathlib import Path
    if not manifest:
        return []
    path = Path(manifest).with_name('records.jsonl')
    if not path.exists():
        return []
    result = []
    try:
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if issues(SCHEMA, record):
                return 'unknown: run record violates steward.record.v1'
            result.extend(record.get('reflections', []))
    except (OSError, ValueError, UnicodeError):
        return 'unknown: run records unreadable'
    return result


def inspect(directory):
    tokens, errors = [], []
    path = directory / 'records.jsonl'
    if not path.exists():
        return tokens, errors
    try:
        with path.open() as stream:
            for number, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                except ValueError:
                    found = ['invalid JSON']
                else:
                    found = issues(SCHEMA, record)
                    if isinstance(record, dict) and 'weighted_tokens' in record:
                        value = record['weighted_tokens']
                        bad_cost = issues(SCHEMA['properties']['weighted_tokens'], value)
                        if not bad_cost and type(value) is float and not math.isfinite(value):
                            found.append('$.weighted_tokens: expected finite number')
                            bad_cost = True
                        if not bad_cost:
                            tokens.append(value)
                errors.extend(f'line {number}: {problem}' for problem in found[:max(0, 20 - len(errors))])
    except (OSError, UnicodeError):
        if len(errors) < 20:
            errors.append('line 1: records.jsonl unreadable')
    return tokens, errors
