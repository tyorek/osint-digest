# osint-digest

Nightly cross-outlet news digest that runs entirely on a local model (Gemma via Ollama). It pulls RSS feeds from outlets across the political spectrum, embeds and clusters the last ~26 hours of items by event, keeps stories covered by enough independent outlets, and has the model judge each story twice (reasoning pass, then a quick cross-check) to separate what outlets agree on from what is contested, attributed or single-sourced. Output is a short Markdown digest for the lab's digest viewer.

| File | Purpose |
|---|---|
| `osint.py` | the pipeline: fetch, embed (`nomic-embed-text`), cluster, judge, write |
| `feeds.json` | feed list with lean buckets and watchlist |
| `CLAUDE.md` | system prompt for the per-story judge |
| `run.sh` | cron wrapper (04:30 daily on the agent box) |
| `docs/x-integration-plan.md` | plan for adding X/Twitter as a source (not built) |

Needs an Anthropic-Messages-compatible endpoint (Ollama's) for the judge and `/api/embed` for embeddings; the optional Congress.gov source reads `CONGRESS_API_KEY` from the environment.

---

*Published from a home lab. Site identifiers are sanitized: public IPv4 uses the documentation ranges (203.0.113.0/24, 198.51.100.0/24), the delegated IPv6 prefix is `2001:db8::/32`, the ULA prefix is `fd12:3456:789a::/48`, domains are `example.com` / `home.arpa`, MAC addresses, Wi-Fi SSIDs, serials, keys and names are replaced. RFC 1918 addresses are left as they are so the layouts still read. Credentials are never in these files; scripts read them from `agent.env` or similar files that are not published.*
