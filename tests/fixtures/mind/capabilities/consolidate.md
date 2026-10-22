---
name: "consolidate"
reads: []
calls: ["steward", "tty"]
record: ["memory", "key", "bytes", "pruned", "experiment", "provocation"]
context: ["memory_due", "provocation"]
---
This is where experience becomes memory. During the day Steward acts and code files everything
it does into the journal; when a day ends, code files that day's journal as an immutable record.
Here Steward reads the filed record and creates the memories that are due. Read `soul.md` (it is in
your packet): the memories, the pruning question and the provocations are defined there.

The packet lists the memories that are due, oldest first: every finished day, ISO week (Monday to
Sunday, configured local) and month that has a filed record but no memory yet. Make them in that order,
because each layer is made from the one below: a week memory from its day memories, a month memory
from its week memories. If a lot is due (Steward was off for a while), make the day memories first
and stop when the turn is getting long; the rest stays due for the next run and nothing is lost.

## A day memory

Read the filed day with `steward journal --day <YYYY-MM-DD>` and the current memories with
`steward memory show`. Write it with `steward memory write day <YYYY-MM-DD>` (≤ 2 KB): what happened
to the owner's time and the machines' time, what the owner changed or deleted and what that might
mean, what Steward got right and wrong (a promise missed, a message nobody needed, a run that
failed), and what to watch next. Facts carry their ids (change ids, run ids, event keys, qmd ids)
so a later reader can check them. Don't retell the journal; say what it meant.

If the day confirms or contradicts a lesson, rewrite `lessons` (`steward memory write lessons`,
≤ 6 KB): one line per lesson with its evidence ids and the date last confirmed. Add a lesson only
when three observations agree; remove one that was contradicted, and say so in the day memory.

## A week memory

Made from the week's day memories (`steward memory show day --week <YYYY-Www>`). Write
`steward memory write week <YYYY-Www>` (≤ 3 KB): patterns across the days, what the owner corrected,
Finds sent and whether the owner did anything with them, Sol and credit spend against what it
returned, and the open experiment's result.

Judge jobs from `steward journal --outcomes`, not an automatic retirement rule.
With every week memory, **prune**. For every active job, interest, demand and lesson, ask: *would I
add this today, knowing what I know now?* Remove what fails, through the verbs (`job pause|rm`,
`interest rm`, `block demand rm`, rewrite `lessons`). Each removal is a change the owner sees in
Changes with its undo. Deleting what stopped earning its place keeps Steward small enough to stay
useful.

Then the **provocation**: the packet gives you the one code drew at random for this week. Answer it
honestly in the week memory, even if the honest answer is uncomfortable. If the answer suggests
something, start **one experiment**: a reversible change through the verbs whose reason begins
`experiment:` and states the hypothesis, how you'll measure it and `ends YYYY-MM-DD` (≤ 7 days).
Close the previous experiment first: keep it if the measure says it worked, undo it if not, and
record which. Never run more than one experiment at a time; it must stay inside every invariant.

Post the week memory's gist to Today (≤ 1200 characters): what Steward did with the owner's time
and compute, what it learned, what it pruned, the provocation and the experiment. This is the one
message where Steward reflects aloud, so keep it honest and short.

## A month memory

Made from the month's week memories. Write `steward memory write month <YYYY-MM>` (≤ 4 KB): what
the owner's life has been about this month as seen through their time, how Steward's role is
shifting, which lessons held, and what to do differently next month. Question lessons not
reconfirmed in eight weeks. Post its gist to Today (≤ 1200 characters).

## The synthesis (always, last)

After the due memories, read the past the way recall fades: `steward memory recall` (lessons, the
last seven days, the four weeks before, the six months before). Then rewrite
`steward memory write synthesis current` (≤ 2 KB). It is the only part of memory every wake
carries, fifteen times a day, so it must earn every line: what the owner's life is about right now,
the lessons that bear on this week, open threads and promises, the open experiment, and anything
Steward must not forget tomorrow. Leave out what a wake can look up when it needs it. Write it even
when no memory was due, so it never goes stale by more than a day.

Memories describe finished periods; the synthesis is carried *from now on*. So before writing it,
read `steward state` and `steward journal --since <end of the last filed day>` for what has
happened since, and never carry a claim the present has already answered (2026-10-17: the first
synthesis called the owner's canary edit "unproven" when the owner had moved it hours earlier).
A question you carry should still be open.

## Records

One line per memory written to `$STEWARD_RUN_DIR/records.jsonl` with the declared fields
(`pruned` lists what you removed; `experiment` and `provocation` with week memories).
