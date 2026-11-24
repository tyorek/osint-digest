# Plan: X (Twitter) as an early-signal and primary-statement source for osint-digest

Status: proposal, not built. Written 2026-11-02. Owner: the admin (account, token, allowlist); Claude (code, config,
judge rules, testing). Related: `claude-noc-agent-setup.md` section "osint-digest agent -- live 2026-11-01";
agent files `Scripts - Monitoring & Audit/osint-digest-*`.

## Goal

X usually breaks news before outlets do, because the people involved post there first. The goal is to use that
in the nightly news digest to help decide whether a story is "the actual story or just a story": to show who said
what first, to confirm that an official or agency really made a statement, and to surface stories that are moving
on X before any outlet covers them. The digest's existing job does not change: separating what independent outlets
across the spectrum agree on from what is contested, single-source, or framing.

## The core rule: X is evidence of a statement, not evidence of a fact

X is first for real news and first for false news, and its volume can be manufactured. So X posts must never be able
to push a claim to "corroborated" on their own. Every X post the agent pulls falls into exactly one class, set by an
allowlist in `feeds.json`, and the code (not the model) enforces what each class can do:

| Class | Who | What it can do | What it can never do |
|---|---|---|---|
| **primary** | the actor at the center of a story: officials, agencies, campaigns, militaries (e.g. a senator, State Dept, CENTCOM, a governor) | Confirms *that they said it*, with a timestamp, the same way a Federal Register entry does today. Never counts as a lean side. | Make the content of the statement true. |
| **reporter** | known beat reporters, each mapped to their outlet and that outlet's lean bucket | Counts as one source *in their outlet's bucket*, the same as that outlet's article would. Collapses with their own outlet's article (one source, not two). | Count twice with their outlet, or count in a different bucket. |
| **signal** | everyone else: unknown accounts, OSINT aggregators, viral posts | Shows attention and timing ("discussed on X from 01:10"), and feeds the "Developing on X" lead list. | Upgrade any claim's tier, ever. |

Many accounts posting the same or near-identical text collapse to one source through the near-duplicate check that
osint.py already runs on articles. That is also how coordinated amplification shows up in the report.

## How it fits the current pipeline

The current run: fetch RSS (26 h) -> filter -> embed -> cluster into events -> keep stories with enough outlets and
buckets -> judge each story twice -> code checks -> report. X is added in three places:

1. **Per-story X lookup (after clustering, before judging).** For each kept story, build a search query from the
   story's key entities (names, places, bill numbers) and run X recent search for the story window. Keep posts from
   allowlisted accounts plus at most a few top signal posts; drop reposts and replies, English only. Cap posts per
   story (`x.per_story_max`).
2. **Allowlist sweep (once per run).** Read the recent posts of the primary and reporter accounts (or one X List
   holding them). Posts that match an existing story join it. Clusters of allowlisted posts that match *no* outlet
   story become candidate leads for "Developing on X".
3. **Judge input and code checks.** X posts go to the judge as extra items labelled by class (e.g. `X-P1` primary,
   `X-R1 (reporter, Politico, left)`, `X-S1` signal) with their timestamps. The judge rules gain the class rules
   above. After judging, the existing code checks gain three hard rules: a reporter post counts only as its mapped
   outlet and bucket, and is merged with that outlet's article; primary posts never count as a bucket; signal posts
   are removed from any claim's sources before the tier check. A claim whose only support is X signal is forced to
   "unverified".

## What the report gains

- Per story, an **On X** line: earliest allowlisted post time vs earliest outlet time, primary statements with
  links, and whether chatter was organic or mostly near-duplicate.
- A **Developing on X** section: unverified leads with links, clearly marked as not yet reported by any outlet in
  the feed set.
- The next run checks every lead from the last 48 h: did outlets pick it up, and did the story hold? Results go to a
  state file (`state/x_leads.json`) with a per-account record (leads that held / leads that didn't). After a few weeks
  that record shows which accounts actually break real news, so the allowlist can be trimmed with evidence.

## Configuration (feeds.json, new `x` block)

- `enabled` (true/false): the whole feature; false restores today's behaviour exactly.
- `max_posts_per_run` (start 200), `per_story_max` (start 15), `lookback_hours` (26, same as RSS).
- `accounts`: one entry per account with `handle`, `class` (primary / reporter / signal), and for reporters
  `outlet` and `bucket`. User IDs are looked up once and cached in `state/` so lookups aren't paid for every run.
- `list_id` (optional): read one X List instead of per-account timelines, if cheaper.
- Credentials: `X_BEARER_TOKEN` in `agent.env` (app-only, read-only use). Never logged, never in reports.

## Cost

X has no free read tier and X Premium does not include API access (it's a consumer subscription; the API is billed
separately). The API is pay-per-use through the X Developer Console: about $0.005 per post returned and $0.010 per
user lookup, with repeats inside a 24 h UTC window de-duplicated. New paying accounts can earn a one-time $20 credit.
At the starting cap of 200 posts per run that is about $1 per day, or $30 per month; the cached user IDs keep
lookup costs near zero after the first run. A hard spending cap is set in the console so the bill can't run away,
and the run treats an API refusal (cap reached, auth error) as "X unavailable" and still publishes the digest.

Sources checked 2026-11-02: docs.x.com pricing page (pay-per-use, credits, no subscription tiers);
opentweet.io/x-api-pricing (per-resource prices, checked against docs.x.com 2026-10-10).

## Security notes

- **X text is untrusted input to the local model.** Anyone can write a post designed to steer an LLM. Mitigations:
  posts are passed as quoted data with a length cap, the judge only returns JSON, and the tier rules that matter are
  enforced in code after the model answers, so a hostile post can at worst be mislabelled inside the rules, never
  promoted. The viewer already HTML-escapes everything and only links http(s) URLs.
- **Token handling**: in `agent.env` like the Congress.gov key; the supervisor's deny list already keeps that file
  out of reach.
- **Network**: the agents box already has `agent: HTTPS out` (tcp/443 to WAN) on the RB5009; no firewall change.
  osint.py's IPv4 preference applies to X too.

## Rollout

1. **The admin:** sign in at the X Developer Console with the existing X account, create an app, buy the minimum
   credit, set the spending cap, and put the bearer token in `agent.env` as `X_BEARER_TOKEN` (not in chat).
2. **The admin + Claude:** agree the starting allowlist. Claude drafts a starter list in the three classes; the admin cuts
   it down. Start small (roughly 20-40 accounts) so the early track record is readable.
3. **Claude:** implement behind `x.enabled`, with a `--x-dry-run` mode that fetches and classifies posts and writes
   what it *would* add to a side file, without touching the judge or the report. Run it for a few days and check
   cost and match quality.
4. **Claude:** enable judge integration and the report sections; update the judge rules in the agent's CLAUDE.md
   and the code checks; copy the changed files to the workspace and verify by sha256 as with the first build.
5. **Review after 2-4 weeks:** cost per day, how many "Developing on X" leads were later reported by outlets, how
   many didn't hold, and which accounts earned their place.

## Rollback

Set `x.enabled` to false in `feeds.json`. The next run behaves exactly as today. Revoke the token in the console if
the feature is dropped.

## Open questions

- Whether to add a cheap hourly check limited to the watchlist (Strait of Hormuz, Iran) for near-real-time alerts,
  since the 04:30 daily run doesn't use X's speed advantage. Separate decision; would add cost.
- One X List vs per-account reads, decided by whichever costs less for the final allowlist.
- Starting spending cap amount.
