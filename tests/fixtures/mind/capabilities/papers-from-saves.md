---
name: "papers-from-saves"
reads: ["qmd:instapaper"]
calls: ["qmd", "python3", "tty"]
record: ["key", "title", "url", "why", "evidence", "topic", "published_at", "lookup_urls"]
---
Synthetic example: find new papers related to repeated saved reading.
Example topics are urban gardening and materials reuse; infer interests from
saves, not from these invented labels.

Read qmd browse --help and qmd search --help. Browse --source instapaper --since
<30 days ago ISO> --json; follow cursors until the window is covered. Search
--source instapaper --since <same date> --json to clarify clusters. A topic counts
only with at least four distinct saves in those 30 days. Refresh each interest
used with interest set: JSON fields topic, evidence (qmd ids), source
"qmd:instapaper", capability "papers-from-saves", refreshed_at (offset ISO).
Remove stale interests for this capability. Never turn a failed read into empty interests.

Use an available research skill and read its resource map before use. Scope for free
with firecrawl.py papers from several angles per topic. Only Firecrawl papers,
Scite public reads and open APIs are authorized for this watcher; no paid or
metered actions. Helpers log their own calls; follow the skill's ledger rules.
Asta is optional, only if probe status works and the browser profile is available.
Resolve candidate identities through OpenAlex or Crossref (arXiv API for arXiv
ids); verify titles, dates and full URLs. Prefer papers from the last roughly
60 days. Scite tallies are optional context, never proof that a claim is true.
If lookups fail, record the gap and report unknown; do not invent findings.

Public APIs may rate-limit requests or reject identifier formats. A
paper you could not verify is a Find the owner never gets, so verify patiently rather
than fast: one request at a time, at least 5 seconds apart, and on a 429 wait
30 seconds, then try the next source. Sources, in order, for an arXiv id:
export.arxiv.org/api/query?id_list=ID (title, published date), then
api.semanticscholar.org/graph/v1/paper/arXiv:ID?fields=title,publicationDate,externalIds,
then firecrawl.py read of https://arxiv.org/abs/ID (free). For a DOI, Crossref
api.crossref.org/works/DOI first. One source agreeing on title and date is enough.

Telling the same thing twice is the main way this kind of agent fails. notify canonicalises URL keys.
Call told check on every candidate, including alternate DOI/arXiv identities
when available; pending digest messages also count. notify is the single dedupe
point. Do not maintain a separate dedupe store.

A weak Find costs attention. Send at most three Finds in a run, only when the
paper has a concrete connection to the owner's repeated saves. Each message:
title; one line on why it matters to this owner; full paper link; evidence,
e.g. “you saved 6 Instapaper pieces on X since 09-20”. Use
steward notify --topic Finds --kind result --told-key KEY TEXT
with the scratch/production state path specified in the packet. Write one record
per accepted finding to $STEWARD_RUN_DIR/records.jsonl using the declared fields;
evidence contains qmd ids, lookup_urls contains full bibliographic source URLs.
No report archive or source ingestion. Zero good findings is a valid result.
