# Configuration

Python 3.12 and IANA tzdata are required. Runtime is stdlib-only; parcel jobs
also need `jobs/requirements.txt`. Commands require their producer CLIs on PATH:
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
optional `--partner-label`, `--partner-url`, `--partner-link-label` format delivery info.
The parcel job interpreter is `/home/ubuntu/.local/share/steward/venv/bin/python3`;
adapt its shebang and default CLI path when installing on another host.
Backup job `--remote` overrides `backup_remote`; `--no-upload` stays local.
