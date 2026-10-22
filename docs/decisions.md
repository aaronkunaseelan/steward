# Design decisions

These decisions explain the boundaries behind Steward's runtime and deployment
policy. Identifiers are retained where code refers to them. This is public
reasoning, not a transcript or an installation's private charter. The code and
shipped contracts determine which parts this branch implements; prose permissions
are not an operating-system sandbox.

## Scope: own when for two resources

**Decision:** Steward schedules the owner's time on Calendar B and host compute
on Calendar C. gsd owns work; qmd owns archived data. Main calendars are read-only.

**Why:** one authority per meaning makes changes and mistakes attributable.

**Rules out:** triage or ingestion inside Steward, writes to gsd's store, calendar
invitations, and a second task backlog. Calendar C stays in Steward state rather
than a separate provider calendar.

## Clock: deterministic tick, bounded model wake

**Decision:** a timer drives deterministic ticks. Scheduled opportunities and
relevant events can request one gated model harness invocation per wake.

**Why:** polling, manifest folding and dispatch should keep working without
spending a model turn; judgement needs a fresh, finite decision context.

**Rules out:** a resident agent loop, a model call for every tick, or an independent
gsd timer competing with Steward's ready-by scheduling. One harness invocation
can contain multiple tool/model turns; it does not mean one provider request.

## Compute: needs, execution and delivery are separate

**Decision:** jobs declare ready-by, maximum-staleness or recurrence needs;
feasible starts are computed, reservations are checked, and costs are learned
from run evidence. Delivery can be digest, immediate or silent independently.

**Why:** an execution slot cannot protect a weekly model pool, and a useful time
to read a finding need not be a useful time to compute it.

**Rules out:** fixed per-capability daily slots, a hardcoded spend percentage or
reserve, and treating immediate delivery as permission to interrupt. Host and
heavy-lane constraints remain code checks; prioritisation is judgement.

## Calendar B: choose only from checked candidates

**Decision:** demands become code-issued candidates; model selections go through
an independent checker before recorded apply. Undo creates a new checked change.

**Why:** a plausible plan can still overlap busy time or violate a deadline.
Partial provider effects need recoverable intended state.

**Rules out:** fabricated candidate ids, direct model calendar writes, silent
replacement of omitted blocks, and unconditional rollback promises. Later owner
edits or current constraints may make an undo impossible.

## Capabilities: files and tools, not plugins

**Decision:** a capability is frontmatter and policy prose, referenced by a job
row. Providers with sufficient read interfaces are queried live; archived/searchable
owner data comes from qmd. Missing tools and missing sources belong to their
respective producers.

**Why:** extensibility should not multiply ingestion, timers or resident services.

**Rules out:** Python hooks inside the scheduler, an MCP server per capability,
and capability packages with independent data pipelines. Frontmatter names
required reads/calls and record fields; the job owns model, cadence and delivery.

## Attention: act, tell, offer undo

**Decision:** reversible scheduling changes can be made and explained. Messages
are silent by default; explicit interrupts need a reason and obey a daily cap.
Outbox intent precedes sending and finding identities share the told ledger.

**Why:** asking for every reversible action spends attention, while duplicate or
noisy messages erode trust.

**Rules out:** equating delivery urgency with sound, separate dedupe stores per
watcher, and describing at-least-once delivery as exactly once. Sent messages are
not undone by restoring state. Irreversible outward-facing actions require approval.

## Evidence: unknown is not empty

**Decision:** senses expose a verdict, reason and observation time. Read-age and
producer-owned validity rules both matter. Outcomes describe the observation and
its denominator, including missing history.

**Why:** a successful read of stale data is not a current view; silence is not
proof that the owner accepted a calendar block or read a finding.

**Rules out:** healthy status after a failed read, zero counts invented from
missing evidence, or declaring delivery merely because a process succeeded.
`intent/1` is shipped here; `telemetry/1` is not a shipped Steward interface in
this branch.

## X-5: local transcript archives, explicit exported views

**Decision:** Steward keeps wake and job sessions in its own file archive, not in
qmd. Normalised transcript and summary views can be published to the configured
edge surface; source excerpts may also be delivered in Telegram. Secrets and
credentials remain excluded from exported views.

**Why:** operational sessions have their own lifecycle and should not become
signals fed back through the owner's general archive. A stable read view decouples
the display from vendor session formats.

**Rules out:** using qmd as Steward's transcript store, reading live vendor files
from the transcript CLI, treating an incomplete archive as empty success, or
shipping credential-bearing raw sessions as the public view. Raw local copies
remain unchanged; bounded exported views are redacted.

## X-18: calendar edits are evidence, not permanent pins

**Decision:** reconcile owner moves and deletions, but allow later reasoned
rescheduling. Treat a single calendar edit as weak timing evidence alongside
actual activity, repeated patterns and explicit instructions.

**Why:** an edit describes a situation; it need not be an enduring preference.

**Rules out:** permanent pin states, deletion tombstones preventing replacement,
and learning attendance merely from placement. The owner's actionable list follows
gsd's next-actor and ordering semantics, not a second priority model.

## X-19: values in code/data, judgement in prose

**Decision:** moments are undoable data; runtime-used values have one code/data
definition and are rendered on the decision page. Soul and charter refer to the
page rather than repeat numeric limits. Every job runs from an immutable release.

**Why:** copied clocks and limits drift; mutable entrypoints make a run's identity
unknowable. Policy should explain why, not maintain a shadow scheduler.

**Rules out:** hardcoded prose schedules, scratch-script jobs, and live edits to
released code. Automatic retirement after several empty runs is removed: weekly
review judges utility from outcomes. Waiting on others means people, not agent
processes. The Today message has one code-enforced size limit.

## X-24: one ready-by retry, all actors visible

**Decision:** when a failed ready-by run is folded, schedule one new retry without
moving the ordinary next reservation. Keep host/heavy guards. Changes reports
owner and supervisor edits as well as model edits.

**Why:** one failed execution should have a bounded recovery path, and the owner
needs to see meaningful changes regardless of who initiated them.

**Rules out:** infinite retry chains, ignoring shared-resource guards during
recovery, and model-only change reporting. A recovered deadline remains historically
late rather than becoming retroactively punctual.

## X-25: scoped security capability, not a general upgrade agent

**Decision:** the approved deployment policy is a daily relevant-advisory review.
Digest ordinary findings; consider an interrupt only for a confirmed affected
installed version with evidence of exploitation. Approved low-risk host fixes must
be verified and recorded. Device fixes are recommendations, not autonomous changes.

**Why:** advisories matter through actual exposure, while unreviewed upgrades can
damage the very system being protected.

**Rules out:** blanket vulnerability alerts, raw upgrade commands bypassing a
tool's fix allowlist, edits inside immutable production environments, or disruptive
fixes during critical/heavy runs. The runtime supports capability permits for
pages, alerts, health verification and declared fix tools; the advisory collector
and its remediation logic are external tool/deployment responsibilities, not a
security scanner bundled in Steward's core.

## X-26: malleable jobs, no count cap

**Decision:** combine a capability file with a job row; fixed domain logic belongs
in its own released CLI. Remove the recurring-job count cap. Show the complete job
configuration and capability version. Validate settings before tick effects and
active job specs before planning or dispatch.

**Why:** learned cost and delivered value are better controls than an arbitrary
count. Extensibility requires inspectable inputs and failures, not more runtime
extension mechanisms.

**Rules out:** silent execution of invalid jobs, partial job views, domain tools
accumulating inside the scheduler, and duplicate schema definitions in tests.
Existing small helpers move to their own tools when next revised. The vendored
`intent/1` retains its legacy job-chip `cap` field for format compatibility;
removing that field requires the consumer's next contract version, not an
unannounced format edit.

## X-27: assistant and coach through readiness

**Decision:** prepare the way: bundle related work, find time, prepare linked
material and present the day in the order the owner will encounter it. Keep the
work/data ownership boundaries and quiet-by-default delivery.

**Why:** useful scheduling reduces the effort of starting and completing work,
not merely the effort of drawing calendar rectangles.

**Rules out:** taking over the backlog, changing gsd's work, or proactively making
Gmail drafts. Draft creation still needs an explicit request. The shipped wake
prompt implements this posture; the synthetic mind demonstrates the file mechanism,
not a complete deployable personal policy.

## X-28: one interpretation of watch evidence

**Decision:** a watch session typed `other` counts toward the strength dose in the
shared fulfilment function. Heart-rate labels classify cardio, not strength.

**Why:** planning, descriptions and briefings must agree on what the evidence means.

**Rules out:** a model-only reinterpretation, separate dose counters per surface,
and guessing exercise attendance from calendar or place alone. This mapping is an
explicit policy choice of the current implementation, not a universal physiological
claim about an unclassified watch activity.
