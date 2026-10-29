# Steward

Steward is a single-owner personal scheduling agent for people who want their calendar and background work prepared, not another task inbox. It owns **when**: time on a schedule calendar and compute time on a host. Its distinguishing idea is to separate deterministic dispatch from model judgement: code reads, validates and executes; a bounded model wake chooses useful changes through recorded CLI verbs.

## What it offers

- **A day prepared to walk through.** An orientation joins meetings, scheduled blocks, prepared-page links, waiting-on-others items and planned compute work. A day close reviews what happened and looks ahead.
- **Time for related work.** Bundle open gsd items into a scheduled block with a deadline and a preparation page, without changing gsd's definition of the work.
- **Background work with a reason.** Jobs declare freshness or ready-by needs; reservations, run receipts and failure reasons make their execution inspectable.
- **Finds rather than report sprawl.** Watchers connect evidence of interests to outside lookups, check what was already told, and deliver linked findings through Telegram.
- **Changes you can inspect and, where supported, undo.** Calendar and job changes carry reasons and change ids. Failed reads stay unknown rather than becoming an empty agenda.
- **Quiet delivery.** Routine messages are silent; deliberate interrupts need a reason and consume a daily cap. Longer evidence lives on linked pages.

This is an integration of an existing personal stack, not a standalone hosted assistant. Calendar access, Telegram, model harnesses and sibling tools need configuring.

## How it works

### Tick and wake

`systemd/steward.timer` starts `steward-tick.service` every 20 minutes. `steward tick` is deterministic and makes no model call: it polls owner messages, refreshes due senses, folds run manifests, reserves and dispatches work, flushes messages and maintains archives.

`steward-wake.service` runs `steward wake`. Scheduled slots and events request wakes; a gate checks for owner instructions, due moments, failures or relevant changes. A passing, enabled wake starts **one model harness invocation**, not a resident agent loop. That invocation can contain model/tool turns; it is not a promise of one provider API request. The wake service has a 40-minute bound. The current wake harness is Opus at medium effort; async agent jobs select Sol, Haiku or Opus through their job spec.

The wake reads `prompts/wake.md`, its trigger, the full Now section, an index of rendered sections, and the live mind. Index lines report line counts and changes since the last model wake (unknown reads never say unchanged), and name sections required by due moments. Pull detail with `steward briefing "SECTION" ["SECTION" …] [--wake ID]`; `steward briefing --list` lists the latest wake's sections. The full `briefing.md` remains in each wake archive; section digests and exact text boundaries are recorded with that wake. It is instructed to act through `steward` verbs. Writer locks cover individual operations, not the whole model session. `steward tick skip --from ISO --to ISO --reason TEXT` records an undoable window that suppresses wakes, not deterministic sensing, planning or dispatch.

### A mind outside the release

The configured `mind_dir` is its own local git repository:

- `soul.md` supplies purpose, values and how experience should become judgement.
- `charter.md` supplies current preferences, permission boundaries and scheduling judgement.
- `capabilities/<name>.md` supplies a job's declared inputs, required CLIs, record fields and policy prose.
- `reference/<task>.md` holds task detail (planning, messages, compute, memory, browser) that a wake or job pulls only when the task needs it, so the fixed prefix stays small.

Wakes read soul and charter afresh; agent jobs read their capability afresh. Tick commits pending mind edits and records release/mind revisions. `steward state` reports mind history, or unknown if it cannot establish it. The public mirror's `mind/` folder carries the live `soul.md` and `charter.md`; the capability and reference files stay private. `tests/fixtures/mind/` demonstrates the file mechanism with synthetic policy; it is not a production charter or an authoritative list of current limits.

`steward memory write|show|due|recall` provides day, week, month, lessons and synthesis layers. Filed journals preserve evidence; synthesis and an index keep each wake from rereading the entire past. `steward journal --wake N --json` identifies the revisions and changes visible to a wake.

### Senses and verdicts

`steward state` and the wake page expose these read-only senses:

| Sense | Read interface | Rule |
|---|---|---|
| `calendars` | `gog --readonly` calendar reads | Main calendars are constraints; only the configured schedule calendar is writable. |
| `gsd` | `gsd worklist --json` | Freshness needs a landed cut and evidence for the job's ready-by promise. |
| `qmd` | `qmd status --json`, `qmd feed` | Validate producer currency; advance arrival checkpoints only with a successful observation. |
| `health` | `garmin today --json` | Use dated watch evidence and producer expiry; never trigger sync. |
| `place` | `phone where --json` | Preserve named-place state, city evidence and compressed night runs; the model holds home in its synthesis. |
| `machine` | systemd, disk and load reads | Dispatch must respect host capacity. |
| `sol` | Observed Codex rollout rate-limit events | Expose measured headroom and observation time, not an invented spend allowance. |

A sense carries `verdict`, `reason` and `as_of`: `fresh`, `stale` or `unknown`. Read age beyond twice its read interval is stale; producer-specific completeness and expiry checks also apply. Health and place are scheduling inputs, not wake events, and are excluded from the Page 2 intent view. An observation failure is not itself evidence that the world changed.

Producer schemas are vendored in `contracts/vendor/` and validated on reads. They include `gsd.worklist.v1`, `gsd.runs.v1`, `gsd.closures.v1`, `qmd.status.v1`, `qmd.feed.v1`, `qmd.extract.v1`, `garmin.today.v1` and `phone.place.v3`.

The wake page's Calendar covers today and two full days. Ahead reaches 24 months (its exclusive coverage date is shown; beyond is unknown), with individual events through day 30 and recurring-series summaries further out. Its event lines have a 16 KiB bound and an explicit omitted-line count. Near calendars refresh per tick; the far view refreshes once per local day and never triggers a wake. Planning and Calendar B placement still use their existing window. Every capability may read `steward memory show synthesis current` for home and notable whereabouts.

### Calendar B: candidates, checker, apply

Calendar B is the writable schedule calendar; main calendars remain read-only, with no attendee or invitation writes.

1. `steward block demand add|edit|rm` records scheduling needs. A block with items references gsd item ids, a title and a deadline.
2. `steward block plan` reads the calendar and produces candidate ids.
3. `steward block choose --spec FILE` takes selections and reasons. An independent checker validates candidate references, time windows, durations, deadlines and overlap before any calendar effect.
4. Apply records intended effects before provider writes, then confirms them. `steward block apply CHANGE_ID` resumes a partial apply only after a fresh check.
5. `steward undo CHANGE_ID` makes a new checked change. Later edits or today's constraints can prevent restoring an old placement.

The planning horizon covers today plus the next two full days and extends for demand deadlines; weekly count demands can place the whole week. Owner moves and deletions are reconciled as evidence, not permanent pins or tombstones. Calendar B has no frozen horizon or churn cap; reasons and Changes messages explain adaptations.

Training separates calendar containers from dose targets. `fulfilment.py` is the shared interpreter of watch evidence, including the `other`-as-strength mapping. A scheduled block alone does not prove exercise happened; unknown watch evidence holds additions and drops.

### Calendar C, jobs and capabilities

Calendar C lives in Steward's versioned state, not a second Google calendar. `steward plan` lists feasible compute starts; `steward run plan JOB --at ISO --reason TEXT` selects a checked reservation. Tick supplies defaults for unreserved recurring jobs.

A job row has a written `justification`, release-bound `argv`, and `needs`: `ready_by`, `max_staleness` or `every {min,max}`. It can declare `min_gap`, `heavy`, `model`, `deliver` and `max_minutes`. `max_minutes` is the first-run bound after add or an argv/capability blob change; ready-by targets are measuring, not promised, until a succeeded run. Then the limit is ceil(10 × the longest of the last five succeeded durations since that change), a runaway bound, at least 5 minutes. State shows `limit_minutes`, its source (`first-run bound` or `learned`), the job fields, validation failures and capability file version. There is no recurring-job count cap.

Use `steward job add|edit|pause|resume|rm`, `steward run now JOB` and `steward run cancel RUN`. Calendar C reservations may be moved or cancelled with a reason, including near-term and repeated changes. Host-load/disk guards and the shared heavy lane still constrain dispatch. A failed ready-by run gets one retry, not an unbounded retry loop.

`steward contract steward.job.v2` prints the runtime protocol (`steward.job.v1` remains available for old runtimes): jobs run through `batch-run` and `run-exec`, receive `STEWARD_RUN_DIR` and backlink variables, and write `records.jsonl`. Steward owns `manifest.json`, stdout/stderr tails, process-group deadlines and exit status. `steward contract steward.record.v1` prints record fields and the weighted-token formula. Learned run costs (`cost_tokens`, job `cost.median_tokens`, and journal `tokens`) use weighted tokens from the same session reader as `summary.json`, counted once. Records supply cost only for runs without sessions; absent evidence is unknown. On the first writer open after the unit correction, finished historical costs are replaced by each archive's `summary.json` `tokens.weighted`, or unknown when unavailable. A `message_meta.cost_unit` marker makes this correction one-time; it does not change the schema.

A capability is data plus prose, never an in-process plugin. Its frontmatter uses JSON-valued `name`, `reads`, `calls`, `record`, and optional `context`/`permits`; launch validation checks required tools and qmd sources. The job row owns cadence, model and delivery. `calls:` is the model shell's tool vocabulary and PATH (plus basic read tools), not a security boundary; absolute paths remain reachable. `permits` delegates authority, while destructive actions stay guarded in the owning CLI. Packets name `steward state` for interests and `steward told check KEY…` instead of preloading lists; missing tool facts become `unestablished` records, not improvised scripts. Fixed domain logic belongs in its own tool release. `jobs/` contains agent-run, backup and small existing helpers; it is not an ingestion framework.

### Messages, pages and undo

Telegram topics are Today, Changes, Finds, Alerts and Talk. Only the configured owner's messages are accepted. `steward inbox --outstanding --json` exposes unhandled instructions; `steward inbox done ID…` acknowledges addressed messages.

Writes require an explicit `STEWARD_ACTOR=owner`, `model` or `maintenance`; reads need none. Internal runtime operations use `tick` or `migration` in code.

`steward notify` records outbox intent before sending. The topic determines the route; there is no separate message kind. `--told-key` and `steward told check KEY…` deduplicate canonical finding identities, including pending delivery. Text delivery is at least once: a crash after sending but before recording the receipt can repeat a message. Delivered messages cannot be unsent by runtime undo.

`steward notify --reply-to MESSAGE_ID TEXT` replies to a message Steward sent.
Its topic and thread come from the outbox; `--topic` is optional but must agree.
Replies use ordinary immediate notification delivery, including from Finds, and
credentialless runs queue them for the tick. All limits apply, including Today's
character limit. The model decides whether an update matters and writes it;
code never derives an update from a changed page or memory.

All delivery is silent unless `notify --interrupt REASON` explicitly requests sound; even `--deliver immediate` is silent by default. Code enforces the daily interrupt cap; quiet hours, focus and urgency are model judgement. Automatic Changes includes owner-visible edits by the owner and model; maintenance stays in the journal.

`steward page put --title TITLE FILE` uploads Markdown, HTML or an attachment to the Worker edge in `edge/`, backed by R2. Generated slugs combine descriptive words with randomness; local page metadata expires after 28 days and records read receipts. Configure the R2 bucket lifecycle separately to expire stored objects. Pages are password-protected by default; `--public` is explicit. The upload API uses a separate bearer credential. `steward page get SLUG` prints the current authenticated object body without recording or changing local state; unknown pages and failed reads exit non-zero. Notifications require hyperlinks; the message should carry the point and the page the evidence. `bin/deploy-edge` tests and deploys the edge from an exact commit; it does not provision the bucket lifecycle.

`steward draft` creates Gmail drafts when the model judges they serve the owner, including during preparation. Capability jobs need the `draft` permit; the CLI checks the stored run and job context before calling the provider. Provider commands use a draft-only allowlist and `--gmail-no-send`; no send verb exists. Undo deletes the draft. After an ambiguous provider failure, inspect Drafts before retrying.

### Intent, transcripts and outcomes

`steward intent --json` produces **`intent/1`**, validated in tests against `contracts/vendor/intent-1.json`. It is the Page 2 view for mech-d: promises, attention, process state, a 36-hour schedule and at most 100 recent runs over seven days. It reads Steward's own state, configuration, journal and archive metadata without producer calls or writes. Broken inbound polling is visible, not silently healthy.

**`telemetry/1` is a separate family monitoring contract, not an interface shipped by this branch.** There is no vendored telemetry schema or `steward telemetry` verb here. Steward reads producer status through the concrete contracts above; monitoring belongs to mech-d, not to a second stream of Steward alerts.

`steward transcript wake-N --json` and `steward transcript run-HEX --json` read Steward's own archives. `steward contract steward.transcript.v1` and `steward contract steward.run-summary.v1` print their schemas. Raw native sessions remain unchanged in private local files; exported views are bounded and credential-redacted. Missing archives are unknown, not empty success. Finalizers and tick sweeps prepare transcript and summary files for the edge shipper; qmd is not this archive.

`steward journal --outcomes --week YYYY-MM-DD --json` (the Sunday's date; weeks run Sunday–Saturday in Athens) computes observations without storing another score ledger: owner moves/deletions, replies to Finds, gsd amendment rates, use of blocks with items, preparation-page opens and closed items. Definitions travel with counts. An unchanged block is not affirmative acceptance, a page open is not proof of reading, and missing denominators or history stay unknown.

## Design decisions

1. **Own when, not what →** schedule from gsd and read qmd → no parallel task authority or source ingestion.
2. **Deterministic tick, bounded wake →** routine work does not need model reasoning → no resident model polling loop.
3. **Needs rather than fixed compute slots →** freshness, execution cost and delivery time are different constraints → no fixed daily slot per capability.
4. **Checked CLI changes →** a model's choice is not a validated effect → no direct calendar writes around the checker.
5. **Live mind, immutable runtime →** preferences evolve independently of code → no production edits to a running release.
6. **Code owns machine-used values; prose owns judgement →** prompts should not become drifting copies of constants → no second numeric policy table in the charter.
7. **Measured spend, no hardcoded share →** interactive and background work share capacity → no invented percentage-reserve gate.
8. **Owner calendar edits are weak evidence →** one move should not freeze a demand forever → no permanent pins or deletion tombstones.
9. **Silent by default, interrupt deliberately →** attention is a limited resource → immediate delivery does not automatically mean sound.
10. **Capability files plus job rows →** extension should reuse existing tools → no plugin platform or per-capability ingestion/timer service.
11. **Outcomes before activity →** a successful process need not deliver anything useful → no automatic retirement after an arbitrary number of empty runs.
12. **Honest unknowns and local evidence →** planning needs trustworthy observations → no green view built from failed reads or inferred acceptance.
13. **Prepare the way without taking over the backlog →** bundling and preparation reduce friction → no autonomous rewrite of gsd's work; Gmail drafts are a model decision, never a mail send.

[Detailed decisions](docs/decisions.md) retain the identifiers referenced by code and distinguish runtime enforcement from deployment policy.

## The family

- [qmd](https://github.com/aaronkunaseelan/qmd-ak) holds the data and exposes archive reads and arrival feeds.
- [gsd](https://github.com/aaronkunaseelan/gsd-ak) decides **what**: work, ownership, deadlines and closure evidence.
- [Steward](https://github.com/aaronkunaseelan/steward) decides **when**: human and compute reservations, preparation and delivery.
- [tg-tools](https://github.com/aaronkunaseelan/tg-tools) provides Telegram delivery tools in the family. Steward uses its own bot and direct Bot API outbox, not a tg-tools outbox dependency.

## Run it

Requires Linux user systemd, Python 3.12, IANA tzdata and git. Python runtime code is stdlib-only. The operational stack also needs the appropriate `gog`, `gsd`, `qmd`, `garmin`, `phone`, `tty`, `batch-run` and `rclone` commands, provider authentication and a configured Telegram forum bot. Some helpers need extra dependencies; the edge uses Node/Wrangler and Cloudflare Workers/R2.

Start with [docs/config.md](docs/config.md): settings, secret-file names, job arguments and deployment-specific paths are explicit there. The shipped launcher, service PATH and parcel interpreter retain installation-specific paths; adapt those in your deployment before use. This repository does not provision the sibling tools or ship a universal installer.

Install an exact reviewed commit as a release, not a live checkout:

```sh
SHA=$(git rev-parse HEAD)
R="$HOME/.local/lib/steward/releases/$SHA"
mkdir -p "$R"
git archive "$SHA" | tar -x -C "$R"
```

After configuring credentials, calendars, the bot and a separate mind git repository, point `~/.local/lib/steward/current` at that release and `~/.local/bin/steward` at its launcher. On a new install run `steward init-state`. Enable model wakes only when the configured mind and permissions are ready (`wake_model = true`).

Install the adapted `systemd/steward-tick.service`, `systemd/steward-wake.service` and `systemd/steward.timer` in the user unit directory, then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now steward.timer
systemctl --user status steward.timer steward-tick.service
steward state
steward intent --json
```

An active timer alone is not acceptance: check sense verdicts/reasons, inbound polling, planned runs, completed manifests and actually sent moment messages. `steward journal --day YYYY-MM-DD --json` provides the evidence. Tick and wake run inside their services so they receive the required environment files; they are not standalone shell polling commands.

## Developing

```sh
env PYTHONPATH=src python3.12 -m unittest discover -s tests
PYTHONPATH=src python3.12 -m steward --help
cd edge && npm ci && npm test
```

- `src/steward/`: runtime, scheduler/checker, senses, state, messages, archives and views.
- `contracts/`: runtime-owned formats; `contracts/vendor/`: producer and consumer-owned schemas. Tests validate against these files, not duplicate definitions.
- `prompts/` and `tests/fixtures/mind/`: wake instructions and synthetic policy examples.
- `jobs/`, `systemd/`, `edge/`: executable helpers, service units and page delivery.

Release outline: run tests, review the exact commit and configuration changes, back up state before schema changes, wait for active ticks/wakes to finish, archive the commit, atomically switch `current`, reload changed units and verify the observables above. Job entrypoints must resolve inside immutable tool releases; system interpreters or scratch scripts are not job entrypoints. Keep the live mind in its own git history.

`jobs/backup` snapshots SQLite, memory and mind history, records object hashes and sizes, and optionally uploads through rclone; `--no-upload` keeps the snapshot local. `jobs/parcel-watch` reads ELTA's public lookup through the shipped stdlib `jobs/elta-track` reader; no account or key is needed. Source-timezone, route and partner labels are job arguments, not runtime policy. See [configuration](docs/config.md) for the three-request protocol, timezone and event-key details.

## Licence

[MIT](LICENSE).
