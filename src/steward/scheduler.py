"""Pure fixture scheduling. No clock, database, provider, or effect authority.

All arithmetic/comparisons of instants use UTC. Local dates only define horizon/week.
The checker validates physical constraints independently of slot enumeration.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfoNotFoundError
from .settings import zone as configured_zone

from .timing import planning_end
from .memory import week_start
from .fulfilment import MOVEMENT_MIN_MINUTES, MOVEMENT_QUIET_MINUTES

UTC = timezone.utc
MAX_CANDIDATES = 2048


class InvalidPlan(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidPlan(message)


def fields(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "invalid object schema")


def instant(value, zone):
    """Require an explicit offset and validate it against the selected IANA zone.

    This rejects nonexistent wall times and wrong offsets; ambiguous wall times
    require the caller to choose one of the two valid offsets explicitly.
    """
    require(isinstance(value, str), "time must be an offset ISO string")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidPlan("invalid ISO time") from exc
    require(dt.tzinfo is not None and dt.utcoffset() is not None, "naive time forbidden")
    local = dt.astimezone(zone)
    require(local.replace(tzinfo=None) == dt.replace(tzinfo=None)
            and local.utcoffset() == dt.utcoffset(), "wall time/zone offset mismatch")
    return dt.astimezone(UTC)


def interval(value, zone):
    start, end = instant(value["start"], zone), instant(value["end"], zone)
    require(start < end, "empty or reversed interval")
    return start, end


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def identifier(value):
    require(isinstance(value, str) and 0 < len(value) <= 100, "invalid identifier")


def bounded_int(value, lo, hi):
    require(type(value) is int and lo <= value <= hi, "invalid integer bound")


def context(data):
    fields(data, ("schema_version", "timezone", "timezone_confirmed", "now", "deadline",
                  "external_state", "horizon_end", "windows", "blocks", "tasks", "deleted_task_ids"))
    require(type(data["schema_version"]) is int and data["schema_version"] == 1, "schema version")
    require(data["timezone_confirmed"] is True, "confirmed timezone required")
    require(data["external_state"] == "known", "unknown external state")
    try:
        zone = configured_zone(data["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise InvalidPlan("invalid IANA zone") from exc
    now = instant(data["now"], zone)
    deadline = instant(data["deadline"], zone)
    require(now < deadline <= now + timedelta(minutes=40), "expired or unbounded wake deadline")
    end = instant(data["horizon_end"], zone)
    require(end >= planning_end(now, zone), "horizon shorter than planning minimum")
    for key, bound in (("windows", 16), ("blocks", 256), ("deleted_task_ids", 256)):
        require(isinstance(data[key], list) and len(data[key]) <= bound, "input collection bound")
    require(isinstance(data['tasks'], list), 'invalid tasks')
    deleted = data["deleted_task_ids"]
    for item in deleted:
        identifier(item)
    require(len(set(deleted)) == len(deleted), "duplicate deletion")
    windows = []
    for window in data["windows"]:
        fields(window, ("start", "end"))
        span = interval(window, zone)
        require(now <= span[0] < span[1] <= end, "window outside horizon")
        require(not any(overlaps(span, other) for other in windows), "overlapping windows")
        windows.append(span)
    tasks = {}
    for task in data["tasks"]:
        keys = ("id", "minutes", "deadline", "windows") if "windows" in task else ("id", "minutes", "deadline")
        fields(task, (*keys, "day_minutes") if "day_minutes" in task else keys)
        if 'day_minutes' in task:
            require(isinstance(task['day_minutes'], dict) and len(task['day_minutes']) <= 32,
                    'invalid daily durations')
            for day, minutes in task['day_minutes'].items():
                require(isinstance(day, str) and len(day) == 10, 'invalid duration date')
                require(minutes is None or type(minutes) is int and MOVEMENT_MIN_MINUTES <= minutes <= MOVEMENT_QUIET_MINUTES,
                        'invalid movement duration')
        if "windows" in task:
            require(isinstance(task["windows"], list), "invalid task windows")
            for window in task["windows"]:
                fields(window, ("start", "end"))
                a, b = interval(window, zone)
                require(now <= a < b <= end, "task window outside horizon")
        identifier(task["id"])
        require(task["id"] not in tasks, "duplicate task")
        bounded_int(task["minutes"], 15, 240)
        due = instant(task["deadline"], zone)
        require(due > now, "expired task deadline")
        tasks[task["id"]] = (task, due)
    blocks, spans, scheduled = {}, {}, {}
    for block in data["blocks"]:
        fields(block, ("id", "task_id", "kind", "pinned", "start", "end"))
        identifier(block["id"])
        require(block["id"] not in blocks, "duplicate block")
        require(block["kind"] in ("busy", "manual", "scheduled"), "unknown block kind")
        require(type(block["pinned"]) is bool, "pinned must be boolean")
        span = interval(block, zone)
        if block["kind"] == "scheduled":
            identifier(block["task_id"])
            require(block["task_id"] in tasks and block["task_id"] not in scheduled, "unknown/duplicate scheduled task")
            # A deleted task may retain its old block until a checked drop.
            # It gets keep/drop choices, but never a new placement.
            scheduled[block["task_id"]] = block
        else:
            require(block["task_id"] is None, "non-scheduled task reference")
        blocks[block["id"]], spans[block["id"]] = block, span
    # New meetings may overlap movable blocks; check() requires the final plan to resolve them.
    immutable = {key for key, block in blocks.items()
                 if block["kind"] != "scheduled" or block["pinned"]
                 or spans[key][0] < now or spans[key][1] > end}
    return zone, now, end, windows, tasks, blocks, spans, scheduled, immutable


def choice_id(choice):
    encoded = json.dumps(choice, sort_keys=True, separators=(",", ":")).encode()
    return "c_" + hashlib.sha256(encoded).hexdigest()[:24]


def choice(task, action, block_id=None, start=None, end=None):
    value = {"task_id": task, "action": action, "block_id": block_id,
             "start": start, "end": end}
    return {"id": choice_id(value), **value}


def generate(data):
    zone, now, end, windows, tasks, blocks, spans, scheduled, immutable = context(data)
    result = []
    fixed = [spans[key] for key in immutable]
    # Reserve existing geometry first, then share the presentation budget across
    # tasks. A distant demand must not crowd unrelated near-term work out.
    for task_id, old in sorted(scheduled.items()):
        result.append(choice(task_id, 'keep', old['id'], old['start'], old['end']))
        if old['id'] not in immutable:
            result.append(choice(task_id, 'drop', old['id']))
    quota = max(1, (MAX_CANDIDATES - len(result)) // max(1, len(tasks)))
    for task_id in sorted(tasks):
        task, due = tasks[task_id]
        old = scheduled.get(task_id)
        if old and (old['id'] in immutable or 'day_minutes' in task):
            continue  # Containers already placed keep/drop; only the next key places.
        limit = min(MAX_CANDIDATES, len(result) + quota)
        if task_id in data["deleted_task_ids"]:
            continue
        for window in ((interval(w, zone) for w in task["windows"]) if "windows" in task else windows):
            if len(result) >= limit:
                break
            start = max(window[0], now)
            # Movement needs coarse options, plus exact starts after obstacles.
            movement = 'day_minutes' in task
            step = 3600 if movement else 900
            stamp = int(start.timestamp())
            grid = datetime.fromtimestamp(((stamp + step - 1) // step) * step, UTC)
            minutes = task.get('day_minutes', {}).get(start.astimezone(zone).date().isoformat(), task['minutes'])
            if minutes is None:
                continue
            obstacles = list(spans.values()) if movement else fixed
            starts = {start, *(b for _, b in obstacles if start <= b < window[1])} if movement else set()
            while grid + timedelta(minutes=minutes) <= min(window[1], due):
                starts.add(grid)
                grid += timedelta(seconds=step)
            for start in sorted(starts):
                if len(result) >= limit:
                    break
                finish = start + timedelta(minutes=minutes)
                if finish > min(window[1], due):
                    continue
                if not any(overlaps((start, finish), span) for span in obstacles):
                    a, b = start.astimezone(zone).isoformat(), finish.astimezone(zone).isoformat()
                    if not old or (start, finish) != spans[old["id"]]:
                        result.append(choice(task_id, "place", old["id"] if old else None, a, b))

    return {"schema_version": 1, "week_key": week_start(now.astimezone(zone).date()).isoformat(),
            "horizon_end": end.astimezone(zone).isoformat(), "choices": result}


def check(data, candidates, proposal, checked_at):
    """Check schema, code-issued references and constraints; return effects-free plan.

    No generator call here: enumeration and feasibility checking are separate logic.
    Caller must retain the code-generated inventory, never replace it with model output.
    """
    zone, now, horizon, windows, tasks, blocks, spans, scheduled, immutable = context(data)
    current = instant(checked_at, zone)
    require(now <= current < instant(data["deadline"], zone), "check deadline exceeded")
    fields(candidates, ("schema_version", "week_key", "horizon_end", "choices"))
    week = week_start(now.astimezone(zone).date()).isoformat()
    require(type(candidates["schema_version"]) is int and candidates["schema_version"] == 1
            and candidates["week_key"] == week
            and candidates["horizon_end"] == horizon.astimezone(zone).isoformat()
            and isinstance(candidates["choices"], list)
            and len(candidates["choices"]) <= MAX_CANDIDATES, "invalid candidate inventory")
    inventory = {}
    for item in candidates["choices"]:
        fields(item, ("id", "task_id", "action", "block_id", "start", "end"))
        payload = {key: value for key, value in item.items() if key != "id"}
        require(item["id"] == choice_id(payload) and item["id"] not in inventory, "invalid candidate ID")
        inventory[item["id"]] = item
    fields(proposal, ("schema_version", "selections"))
    require(type(proposal["schema_version"]) is int and proposal["schema_version"] == 1, "proposal version")
    require(isinstance(proposal["selections"], list) and len(proposal["selections"]) <= len(tasks), "selection bound")
    selected, seen, reasons = [], set(), {}
    for selection in proposal["selections"]:
        fields(selection, ("candidate_id", "reason"))
        identifier(selection["candidate_id"])
        require(selection["candidate_id"] in inventory, "fabricated candidate")
        item = inventory[selection["candidate_id"]]
        task_id = item["task_id"]
        require(task_id in tasks and task_id not in seen, "unknown or duplicate task selection")
        seen.add(task_id)
        old = scheduled.get(task_id)
        require(item["block_id"] == (old["id"] if old else None), "invalid block reference")
        action = item["action"]
        require(action in ("keep", "drop", "place"), "invalid action")
        reason = selection["reason"]
        require(isinstance(reason, str) and len(reason) <= 500, "invalid reason")
        if action == "keep":
            require(old is not None and item["start"] == old["start"] and item["end"] == old["end"], "keep changed interval")
        else:
            require(not old or old["id"] not in immutable, "immutable block change")
            if old:
                require(bool(reason.strip()), "schedule change needs reason")
                reasons[old["id"]] = reason
            if action == "drop":
                require(old is not None and item["start"] is None and item["end"] is None, "invalid drop")
            else:
                require(task_id not in data["deleted_task_ids"], "user deletion wins")
                span = interval(item, zone)
                require(current <= span[0] < span[1] <= horizon, "placement outside mutable horizon")
                require(span[1] <= tasks[task_id][1], "task deadline exceeded")
                task = tasks[task_id][0]
                minutes = task.get('day_minutes', {}).get(span[0].astimezone(zone).date().isoformat(), task['minutes'])
                require(minutes is not None and span[1] - span[0] == timedelta(minutes=minutes), "invalid duration")
                require(any(a <= span[0] and span[1] <= b for a, b in windows), "outside working windows")
                task = tasks[task_id][0]
                if "windows" in task:
                    require(any(interval(w, zone)[0] <= span[0] and span[1] <= interval(w, zone)[1] for w in task["windows"]), "outside task windows")
        selected.append(item)
    # Omitted prior blocks are kept, not silently deleted. Unscheduled tasks may remain pending.
    replacements = {item["block_id"] for item in selected if item["action"] != "keep" and item["block_id"]}
    plan = [dict(block) for key, block in blocks.items() if key not in replacements]
    for item in selected:
        if item["action"] == "place":
            plan.append({"id": "proposed_" + item["id"], "task_id": item["task_id"], "kind": "scheduled",
                         "pinned": False, "start": item["start"], "end": item["end"]})
    require(len({block["id"] for block in plan}) == len(plan), "plan block ID collision")
    occupied = []
    for block in plan:
        span = interval(block, zone)
        if block["kind"] == "scheduled":
            task, due = tasks[block["task_id"]]
            require(span[1] <= due, "retained schedule violates task deadline: refresh snapshot")
            minutes = task['minutes']
            if 'day_minutes' in task:
                original = scheduled.get(block['task_id'])
                minutes = ((spans[original['id']][1] - spans[original['id']][0]).total_seconds() / 60
                           if original and block['id'] == original['id'] else
                           task['day_minutes'].get(span[0].astimezone(zone).date().isoformat()))
            require(minutes is not None and span[1] - span[0] == timedelta(minutes=minutes), "retained schedule duration mismatch")
        require(not any(overlaps(span, old) for old in occupied), "checked plan overlap")
        occupied.append(span)
    return {"schema_version": 1, "status": "checked_fixture_only", "blocks": sorted(plan, key=lambda b: (b["start"], b["id"])),
            "changes": [{"block_id": key, "reason": reasons[key]} for key in sorted(replacements)],
            "pending_task_ids": sorted(set(tasks) - {b["task_id"] for b in plan} - set(data["deleted_task_ids"]))}
