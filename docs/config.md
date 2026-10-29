# Configuration

Python 3.12 and IANA tzdata are required. Runtime, including parcel jobs, is
stdlib-only. Commands require their producer CLIs on PATH:
gog, gsd, qmd, garmin, phone, tty, batch-run, rclone (as used).
Settings: `$XDG_CONFIG_HOME/steward/config.toml` (default `~/.config/steward`);
CLI `--config FILE` overrides it. Unknown keys or invalid types fail validation.
Missing host values fail by key name when needed. Example (invented values):

```toml
timezone = "Etc/UTC"                    # local wall clocks, calendar and daily caps
gmail_account = "owner@example.test"    # drafts account; no default, never sends
pages_host = "https://pages.example.test" # HTTPS origin, no trailing slash
backup_remote = "archive:example-backups/steward" # rclone prefix; --remote overrides
mind_dir = "~/.local/share/steward/mind" # soul, charter, capabilities; own git repo
schedule_calendar_id = "schedule@example.test" # writable calendar B
main_calendar_ids = ["main@example.test"] # read-only calendars
qmd_watch_sources = ["instapaper", "x"]  # sources to watch
wake_times = ["07:00", "18:30"]          # configured timezone, HH:MM
wake_model = false                     # whether model wakes are enabled
load_max = 3.0                         # positive 5-minute load ceiling
interrupt_cap = 3                      # nonnegative daily audible message cap
```

`telegram.toml` in the same config dir: `chat_id = -1`, `owner_user_id = 2`,
and `[topics]` mapping Today, Changes, Finds, Alerts, Talk to integer thread ids.
`telegram.env`: `STEWARD_TELEGRAM_BOT_TOKEN` (bot secret).
`pages.env`: `STEWARD_PAGES_TOKEN` (API secret), `SESSION_SECRET` (edge cookie key).
Edge deploy reads `~/.config/mech-d/credentials.json` key `PAGE_PASSWORD`;
`edge/wrangler.toml` declares Worker name, R2 binding/bucket and deployment options.
Do not commit credential files; use mode 0600. Examples omit secret values.

Tick/wake units start through `python -m steward.service`. At each start its
allowlist (`capabilities.UNIT_CREDENTIALS`) reads only `GOG_KEYRING_PASSWORD`
from `~/.config/agent-secrets/credentials.env`, `STEWARD_TELEGRAM_BOT_TOKEN` from
`telegram.env`, and `STEWARD_PAGES_TOKEN` from `pages.env`. Other names (including
edge-only `SESSION_SECRET`) are not loaded. The original files remain the only
definitions; no credential copies, shell sourcing, argv values or logging.
Assignments must be single-line `NAME=value` (one literal, optionally shell-quoted
value; no expansion). Missing, empty or malformed required values fail visibly by
name before Steward starts. Ordinary CLI calls do not discover secrets in files.

A capability run receives credentials from its permits via `batch-run --pass NAME`:
`draft` brings `GOG_KEYRING_PASSWORD`; `page` brings `STEWARD_PAGES_TOKEN`
(`capabilities.PERMIT_ENV`). Other permits bring none. A capability with no permits,
or a non-capability job, gets no credential passes; an explicit job `env_file`
remains independent. Telegram notifications from credentialless runs queue for the
tick. Drafts keep the exact-command gate and permanent `--gmail-no-send`: known
keyring/config/auth/refusal failures are recorded as `not made: <reason class>`;
a timeout or unusable provider reply stays unknown and needs a Drafts check before
retrying. Provider stderr never enters the row or change summary.

Scripts read `EXA_API_KEY` from the environment
or `~/.config/agent-secrets/workbench.env`. Pocket Casts reads
`~/.config/pocketcasts/credentials.env`: `POCKETCASTS_EMAIL`, `POCKETCASTS_PASSWORD`.
`HOME`, `PATH`, `PYTHONPATH`, `LANG` have their usual process meanings.
The shipped launcher and service PATH retain the deployment-specific
`/home/ubuntu/workbench/scripts/run-tools` entry for batch-run; adjust it on a
different host and install the matching run-tools launcher. They use
`~/.local/lib/steward/current/src` and `/usr/bin/python3.12`.

`XDG_STATE_HOME` defaults to `~/.local/state`; `--state` overrides state location.
`STEWARD_DATA` or `--data` selects archives (default `~/.local/share/steward`).
`TTY_STATE` selects harness records (default `~/.local/state/tty`).
Job context: `STEWARD_RUN_DIR` (output dir), `STEWARD_STATE` (state),
`STEWARD_JOB`, `STEWARD_RUN_ID`, `STEWARD_WAKE_ID` (backlinks),
`STEWARD_ACTOR` (default owner), `STEWARD_DELIVER` (default digest).
`STEWARD_BIN` selects the parcel helper CLI (default `/home/ubuntu/.local/bin/steward`).
Parcel job argv supplies `--source-timezone`, `--source-label`, `--route`;
optional `--partner-label`, `--partner-url`, `--partner-link-label` add a carrier link
on non-final scans (no provider-specific handoff flag). An optional positional
`SEEN_THROUGH_ISO` suppresses scans already seen. Times use the API's offset, or the
explicit source timezone if absent; messages show source and owner-local time.
The parcel job interpreter is `/usr/bin/python3.12`; adapt its shebang and default
CLI path when installing on another host. No dedicated venv or curl dependency.

`parcel-watch` reads [17TRACK v2.4](https://api.17track.net/en/doc) via POST
`gettrackinfo`. It requires `TRACK17_TOKEN` in its environment: put the key only in
this job's own mode-0600 `env_file`, never the tick/wake units or shared credentials.
Missing token, HTTP status and API error code/message fail visibly without headers
or the token. `--carrier KEY` selects a 17TRACK carrier (ELTA 7071, ELTA Courier
100179, La Poste/Colissimo 6051); omit it for detection. The job uses the API's
`latest_event` (sub-status/stage as event code, description and offset timestamp)
and treats `Delivered` as final. It retains the told key and records.jsonl shape;
a new final scan says it will stop watching, as before (it does not mutate the job).

At the watched-parcel cadence of roughly 2 h 20 min, each normal run makes one
`gettrackinfo` call. Only error -18019902 (not registered) triggers `register` then
another `gettrackinfo`: three calls that first run, one thereafter. Registration
error -18019901 (already registered) is success. No local registration cache, delete,
retrack or carrier-change calls; no extra translation service. Registration consumes
the number's quota once while auto-tracked; deleting and re-registering charges again.
The owner creates the account/key; the supervisor makes the first real call.

Backup job `--remote` overrides `backup_remote`; `--no-upload` stays local.
