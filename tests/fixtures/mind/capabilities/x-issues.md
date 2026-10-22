---
name: "x-issues"
reads: ["qmd:x"]
calls: ["qmd", "tty", "exa-search"]
record: ["key", "title", "url", "why", "evidence", "issue", "published_at", "lookup_urls"]
---
Synthetic example: find developments on recurring interests inferred from saved posts. An issue is not a topic they touched once: it is something they come back to. Count an
issue only when at least five saved posts in the last 30 days are about it **and** they fall in at
least two different weeks. A burst of likes in one evening is a moment, not an issue.

Read `qmd browse --help` and `qmd search --help`. Browse `--source x --since <30 days ago ISO> --json`
and follow cursors until the window is covered (pages hold at most 100). Posts carry a snippet;
OCR rows belong to images in a post (their `context` names the post), so read them with the post,
not as separate saves. Use `qmd search --source x` to firm up a cluster. Refresh each issue you use
with `steward interest set` (topic, evidence = qmd ids, source "qmd:x", capability "x-issues",
refreshed_at), and remove stale ones for this capability. A failed read is unknown, never "no issues".

The lookup is the open web through `exa-search QUERY --num 10 --days 7` (Exa; JSON with title, url,
published date and a text excerpt; about $0.007 a search). This synthetic watcher uses Exa for web lookups. Use at most **five searches per
run** across all issues: a weekly watcher should stay a small cost, and five well-aimed queries beat
twenty loose ones. Prefer primary sources (the organisation, the paper, the filing, the official
post) and reputable reporting over aggregators. Only items from the last 7 days count as "latest";
older items are context, not news. When the excerpt can't tell you what actually changed, fetch the
page itself (curl, read-only) rather than searching again.

What makes a Find worth the owner's attention: something actually moved on the issue since the
posts they saved (a decision, a release, a result, a reversal), not commentary restating it. If
nothing moved, send nothing: zero Finds is a good result, and a weak Find teaches the owner to
ignore the topic.

Telling the same thing twice is the main way this kind of agent fails. notify canonicalises URL keys. Call `steward told check` on every
candidate first; `notify` is the single dedupe point.

At most two Finds per run. Each message: a short headline of what changed; one line on why it matters
given what the owner has been saving; the link; the evidence ("you saved 7 posts on X across 3 weeks
since 09-22"). Send with
`steward notify --topic Finds --kind result --told-key KEY TEXT` and write one record
per Find to `$STEWARD_RUN_DIR/records.jsonl` with the declared fields (lookup_urls = full URLs used).
