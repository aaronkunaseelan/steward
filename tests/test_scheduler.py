from fixture_mind import use_settings
import copy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from fixture_mind import TEST_ZONE

from steward.scheduler import (InvalidPlan, check, choice, context, generate,
                               instant)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.data = json.loads(Path(__file__).with_name("fixture.json").read_text())

    def pick(self, candidates, action="place", task="read", index=0):
        item = [x for x in candidates["choices"] if x["task_id"] == task and x["action"] == action][index]
        return {"candidate_id": item["id"], "reason": "deadline priority"}

    def checked(self, candidates, selections, at=None):
        return check(self.data, candidates, {"schema_version": 1, "selections": selections}, at or self.data["now"])

    def forged(self, item):
        # Even a corrupted trusted inventory must not bypass physical constraints.
        candidates = generate(self.data)
        candidates["choices"].append(item)
        return candidates, [{"candidate_id": item["id"], "reason": "test"}]

    def test_deterministic_plan_preserves_existing_blocks_and_deletions(self):
        candidates = generate(self.data)
        self.assertEqual(candidates, generate(copy.deepcopy(self.data)))
        selections = [self.pick(candidates)]
        plan = self.checked(candidates, selections)
        self.assertEqual(plan, self.checked(generate(copy.deepcopy(self.data)), selections))
        for block in self.data["blocks"]:
            self.assertIn(block, plan["blocks"])
        self.assertNotIn("deleted", [b["task_id"] for b in plan["blocks"]])
        self.assertEqual(plan["pending_task_ids"], [])
        self.assertEqual(candidates["week_key"], "2026-10-18")
        self.assertEqual(candidates["horizon_end"], "2026-10-27T00:00:00+02:00")

    def test_dst_horizon_elapsed(self):
        for date, offset, end, expected in (("2026-03-28", "+02:00", "2026-03-31T00:00:00+03:00", 71),
                                            ("2026-10-24", "+03:00", "2026-10-27T00:00:00+02:00", 73)):
            data = copy.deepcopy(self.data)
            data.update(now=date + "T00:00:00" + offset,
                        deadline=date + "T00:30:00" + offset, horizon_end=end,
                        windows=[], blocks=[], tasks=[], deleted_task_ids=[])
            _, now, end, *_ = context(data)
            self.assertEqual(end - now, timedelta(hours=expected))

    def test_ambiguous_and_nonexistent_local_times(self):
        zone = TEST_ZONE
        a = instant("2026-10-25T03:30:00+03:00", zone)
        b = instant("2026-10-25T03:30:00+02:00", zone)
        self.assertEqual(b - a, timedelta(hours=1))
        for value in ("2026-03-29T03:30:00+02:00", "2026-03-29T03:30:00+03:00",
                      "2026-10-25T03:30:00+01:00", "2026-10-25T03:30:00"):
            with self.subTest(value=value), self.assertRaises(InvalidPlan):
                instant(value, zone)

    def test_dst_slots_are_elapsed_duration(self):
        for day, offset, endoffset in (("2026-03-29", "+02:00", "+03:00"),
                                       ("2026-10-25", "+03:00", "+02:00")):
            data = copy.deepcopy(self.data)
            data.update(now=day + "T00:00:00" + offset, deadline=day + "T00:30:00" + offset,
                        horizon_end=("2026-04-01T00:00:00+03:00" if day.startswith("2026-03")
                                     else "2026-10-28T00:00:00+02:00"),
                        blocks=[], deleted_task_ids=[],
                        windows=[{"start": day + "T02:00:00" + offset, "end": day + "T05:00:00" + endoffset}],
                        tasks=[{"id": "read", "minutes": 60, "deadline": day + "T06:00:00" + endoffset}])
            c = generate(data)
            zone = TEST_ZONE
            for item in c["choices"]:
                self.assertEqual(instant(item["end"], zone) - instant(item["start"], zone), timedelta(hours=1))
            self.assertTrue(c["choices"])
            check(data, c, {"schema_version": 1, "selections": [self.pick(c)]}, data["now"])

    def test_unknown_expired_unconfirmed_and_bounds(self):
        for key, value, reason in (("external_state", "unknown", "unknown external state"),
                                   ("timezone_confirmed", False, "confirmed timezone required"),
                                   ("deadline", self.data["now"], "expired or unbounded wake deadline"),
                                   ("timezone", "fake/zone", "invalid IANA zone")):
            data = copy.deepcopy(self.data)
            data[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(InvalidPlan, reason):
                generate(data)
        self.data["tasks"][1]["minutes"] = 241
        with self.assertRaisesRegex(InvalidPlan, "invalid integer bound"):
            generate(self.data)

    def test_malicious_choices_schema_refs_duplicates(self):
        c = generate(self.data)
        good = self.pick(c)
        for selections in ([{"candidate_id": "c_fake", "reason": "test"}], [good, good],
                           [{**good, "start": self.data["now"]}],
                           [{**good, "reason": None}]):
            with self.subTest(selections=selections), self.assertRaises(InvalidPlan):
                self.checked(c, selections)
        duplicate_task = [good, self.pick(c, index=1)]
        with self.assertRaises(InvalidPlan):
            self.checked(c, duplicate_task)
        c["choices"][0]["start"] = self.data["now"]
        with self.assertRaises(InvalidPlan):
            self.checked(c, [])

    def test_checker_deadline_and_recovery(self):
        c = generate(self.data)
        selections = [self.pick(c)]
        with self.assertRaises(InvalidPlan):
            self.checked(c, selections, self.data["deadline"])
        before = copy.deepcopy(self.data)
        result = self.checked(c, selections)
        self.assertEqual(result["status"], "checked_fixture_only")
        self.assertEqual(self.data, before)

    def test_independent_overlap_horizon_duration_deadline_deletion_checks(self):
        cases = [
            ("read", "2026-10-24T09:00:00+03:00", "2026-10-24T09:30:00+03:00", "checked plan overlap"),
            ("read", "2026-10-24T11:00:00+03:00", "2026-10-24T11:30:00+03:00", "checked plan overlap"),
            ("read", "2026-10-27T09:00:00+02:00", "2026-10-27T09:30:00+02:00", "placement outside mutable horizon"),
            ("read", "2026-10-24T10:00:00+03:00", "2026-10-24T10:15:00+03:00", "invalid duration"),
            ("read", "2026-10-25T09:00:00+02:00", "2026-10-25T09:30:00+02:00", "task deadline exceeded"),
            ("deleted", "2026-10-24T10:00:00+03:00", "2026-10-24T10:30:00+03:00", "user deletion wins"),
            ("read", "2026-10-24T08:00:00+03:00", "2026-10-24T08:30:00+03:00", "outside working windows"),
        ]
        for task, start, end, reason in cases:
            c, selections = self.forged(choice(task, "place", None, start, end))
            with self.subTest(task=task, start=start), self.assertRaisesRegex(InvalidPlan, reason):
                self.checked(c, selections)

    def test_pinned_elapsed_outside_horizon_immutable(self):
        for mode in ("pinned", "elapsed", "outside"):
            data = copy.deepcopy(self.data)
            old = data["blocks"][-1]
            if mode == "elapsed":
                old.update(pinned=False, start="2026-10-24T07:00:00+03:00", end="2026-10-24T08:00:00+03:00")
            if mode == "outside":
                old.update(pinned=False, start="2026-10-27T10:00:00+02:00", end="2026-10-27T11:00:00+02:00")
            c = generate(data)
            self.assertEqual([x["action"] for x in c["choices"] if x["task_id"] == "write"], ["keep"])
            forged = choice("write", "drop", "old-1")
            c["choices"].append(forged)
            with self.subTest(mode=mode), self.assertRaisesRegex(InvalidPlan, "immutable block change"):
                check(data, c, {"schema_version": 1, "selections": [{"candidate_id": forged["id"], "reason": "test"}]}, data["now"])

    def test_move_reason_and_omission(self):
        self.data["blocks"][-1]["pinned"] = False
        c = generate(self.data)
        move = self.pick(c, task="write")
        unchanged = self.checked(c, [])
        self.assertIn(self.data["blocks"][-1], unchanged["blocks"])
        with self.assertRaisesRegex(InvalidPlan, "schedule change needs reason"):
            self.checked(c, [{**move, "reason": " "}])
        plan = self.checked(c, [move])
        self.assertEqual(len(plan["changes"]), 1)
        self.assertNotIn(self.data["blocks"][-1], plan["blocks"])
        dropped = self.checked(c, [self.pick(c, "drop", "write")])
        self.assertIn("write", dropped["pending_task_ids"])

    def test_candidate_bound_and_conflicting_new_selections(self):
        # Two tasks at the same valid slot cannot pass the final whole-plan check.
        self.data["tasks"].append({"id": "extra", "minutes": 30,
                                  "deadline": "2026-10-24T12:00:00+03:00"})
        c = generate(self.data)
        with self.assertRaises(InvalidPlan):
            self.checked(c, [self.pick(c), self.pick(c, task="extra")])
        self.data.update(blocks=[], deleted_task_ids=[], windows=[
            {"start": "2026-10-24T09:00:00+03:00", "end": "2026-10-26T00:00:00+02:00"}],
            tasks=[{"id": str(i), "minutes": 15, "deadline": "2026-10-26T00:00:00+02:00"}
                   for i in range(32)])
        bounded = generate(self.data)['choices']
        self.assertLessEqual(len(bounded), 2048)
        self.assertEqual({c['task_id'] for c in bounded}, {str(i) for i in range(32)})

    def test_deleted_existing_block_has_no_placements_and_baseline_overlap(self):
        self.data["deleted_task_ids"].append("write")
        choices = generate(self.data)['choices']
        self.assertTrue(any(c['task_id'] == 'write' for c in choices))
        self.assertFalse(any(c['task_id'] == 'write' and c['action'] == 'place' for c in choices))
        self.data["deleted_task_ids"].remove("write")
        self.data["blocks"][1].update(start=self.data["blocks"][0]["start"], end=self.data["blocks"][0]["end"])
        c = generate(self.data)
        with self.assertRaises(InvalidPlan):
            self.checked(c, [])


if __name__ == "__main__":
    unittest.main()
