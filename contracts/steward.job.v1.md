# steward.job.v1: the job runtime protocol

Owner: Steward. Implemented by every job Steward runs (gsd steady, jobs/agent-run, jobs/backup, ...). Exact values; change them only with a version bump.

1. **Launch.**
   - Steward runs the job's `argv` in its `cwd` under `batch-run [--heavy] --name steward-<job>-<run> --max <max_minutes+5>m`, then `steward run-exec`.
   - The job runs in its own session and process group.
   - Its stdout and stderr tails (64 KiB) are kept as `stdout.txt` and `stderr.txt`.
2. **Environment.** `STEWARD_RUN_DIR` (the run's directory, owned by the job for `records.jsonl` and `links.json`), `STEWARD_RUN_ID`, `STEWARD_JOB`, `STEWARD_DELIVER` (`digest|immediate|silent`), `STEWARD_STATE` and `STEWARD_ACTOR=model` (for `steward` verbs the job calls).
3. **Deadline.**
   - At `max_minutes`, Steward sends **TERM to the job's process group** and waits **60 s**, then sends KILL to whatever remains. It records exit **124**.
   - A job that has a record to write must trap TERM in its **top-level process**, write the record with `"terminated": true` within the grace, and exit.
   - Children are not guaranteed to outlive the parent.
4. **Exit codes.**
   - 0: done, including "nothing to do".
   - 1: failed.
   - 2: usage or configuration error.
   - 124: Steward's deadline (set by Steward, not by jobs).

   Anything non-zero is a failure, and three failures in a row reach the owner's Alerts.
   The last stderr line on failure is the job's public cause: it must not contain
   credentials or private input. Steward records `exit N: <last line clipped to 160>`
   as the failed run's reason, or `exit N` for empty stderr. Exit 124 always records
   `exit 124: deadline exceeded`, regardless of the interrupted job's stderr.
5. **Records.** `$STEWARD_RUN_DIR/records.jsonl` holds one JSON object per line, as `steward.record.v1`. Write records as work completes, not only at exit.
6. **Release.** A job runs from an immutable release. It invokes its own children from that release, never from `PATH`, so a promotion during a run cannot mix releases.
7. **Manifest.** Steward writes `manifest.json` when the job ends: job, run_id, host, argv, cwd, started_at, ended_at, exit_code, status, duration, links, record_errors. A job never writes the manifest.
