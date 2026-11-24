#!/usr/bin/env python3
"""osint-digest: nightly cross-outlet US policy digest + trending world stories.

Run by run.sh from this folder. Stdlib only. Uses ../bin/llm-report's settings and call_local
(local model, Anthropic Messages API on ollama) and ollama's /api/embed for nomic-embed-text.
Writes reports/YYYY-MM-DD.md (+ reports/.raw/YYYY-MM-DD.json). Exit 0 when a report was written.
"""
import datetime as dt
import email.utils
import html
import importlib.machinery
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import socket

# The agents box has an IPv6 address but the RB5009 drops its v6 egress on purpose (agent-box6).
# urllib tries every address in order without happy-eyeballs, so each v6 attempt costs a full
# timeout. Prefer IPv4 answers.
_GAI = socket.getaddrinfo


def _prefer_v4(host, *args, **kwargs):
    res = _GAI(host, *args, **kwargs)
    return [r for r in res if r[0] == socket.AF_INET] or res


socket.getaddrinfo = _prefer_v4

HERE = os.path.dirname(os.path.abspath(__file__))
_ldr = importlib.machinery.SourceFileLoader("llmr", "/home/agent/agents/bin/llm-report")
_spec = importlib.util.spec_from_loader("llmr", _ldr)
LLM = importlib.util.module_from_spec(_spec)
_ldr.exec_module(LLM)
CFG = LLM.load_settings()
CONF = json.load(open(os.path.join(HERE, "feeds.json")))
S = CONF["settings"]
CFG["LOCAL_LLM_TIMEOUT"] = str(S.get("local_timeout", 420))
UA = "Mozilla/5.0 (X11; Linux x86_64) osint-digest/1.0 (home lab)"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = dt.datetime.now().astimezone().date()
T0 = time.monotonic()


def log(msg):
    print(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')}: [osint] {msg}", flush=True)


def env_value(key):
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in open("/home/agent/.config/agent.env"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            k, sep, v = line.partition("=")
            if sep and k.strip() == key:
                return v.strip().strip("'\"")
    except OSError:
        pass
    return ""


def fetch(url, timeout=20, accept="application/rss+xml, application/atom+xml, application/xml, text/xml, */*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def parse_feed(raw):
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            t = c.tag.split("}")[-1]
            if t == "link":
                if c.get("rel") not in (None, "alternate"):
                    continue
                val = c.get("href") or (c.text or "")
            else:
                val = c.text or ""
            f.setdefault(t, val)
        summary = f.get("description") or f.get("summary") or f.get("encoded") or f.get("content") or ""
        out.append({
            "title": clean(f.get("title")),
            "link": (f.get("link") or f.get("guid") or "").strip(),
            "summary": clean(summary)[:700],
            "date": parse_date(f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date")),
        })
    return out


def collect():
    items, health = [], []
    cutoff = NOW - dt.timedelta(hours=S["window_hours"])
    for feed in CONF["feeds"]:
        try:
            got = parse_feed(fetch(feed["url"]))
        except Exception as e:  # noqa: BLE001 - report every kind of feed failure
            health.append((feed["name"], f"FAILED: {type(e).__name__}: {str(e)[:80]}"))
            continue
        kept = [i for i in got if i["title"] and (i["date"] is None or i["date"] >= cutoff)][: S["per_feed_max"]]
        health.append((feed["name"], f"ok ({len(kept)} of {len(got)} items in window)"))
        for i in kept:
            i.update(feed=feed["id"], outlet=feed["name"], bucket=feed["bucket"], section=feed["section"])
            items.append(i)
    seen, uniq = set(), []
    for i in items:
        key = i["link"] or i["title"].lower()
        if key not in seen:
            seen.add(key)
            uniq.append(i)
    return uniq, health


def embed(texts):
    url = CFG["LOCAL_LLM_URL"].rstrip("/") + "/api/embed"
    vecs = []
    for k in range(0, len(texts), 32):
        body = json.dumps({"model": "nomic-embed-text", "input": texts[k:k + 32]}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            vecs.extend(json.load(r)["embeddings"])
    out = []
    for v in vecs:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cluster(vecs, thr, gap=0.08):
    """Greedy centroid clustering with a complete-link guard: an item joins a cluster only if it is
    close to the centroid AND not far from any member, so topic blobs ("the midterms") don't chain."""
    clusters = []
    for idx, v in enumerate(vecs):
        best, bs = None, thr
        for c in clusters:
            s = dot(v, c["centroid"])
            if s >= bs and min(dot(v, vecs[j]) for j in c["members"]) >= thr - gap:
                best, bs = c, s
        if best is None:
            clusters.append({"members": [idx], "sum": list(v), "centroid": list(v)})
        else:
            best["members"].append(idx)
            best["sum"] = [x + y for x, y in zip(best["sum"], v)]
            n = math.sqrt(sum(x * x for x in best["sum"])) or 1.0
            best["centroid"] = [x / n for x in best["sum"]]
    return clusters


ATTR = [
    (re.compile(r"\baccording to (?:the |a )?((?:[A-Z][\w.'&-]*\s?){1,6})"), "according to {}"),
    (re.compile(r"\b((?:[A-Z][\w.'&-]*\s?){1,6})(?:said|says|announced|confirmed) in a statement"), "statement by {}"),
    (re.compile(r"\b(?:told|reported by) (?:the )?(Associated Press|AP|Reuters|AFP|Bloomberg|Axios|CNN|Fox News)\b"), "via {}"),
    (re.compile(r"\((AP|AFP|Reuters)\)"), "wire {}"),
    (re.compile(r"\b(Associated Press|Reuters|Agence France-Presse|AFP)\b"), "wire {}"),
    (re.compile(r"\b(?:the )?(White House|Pentagon|State Department|Justice Department|Kremlin|governor's office|mayor's office)\b (?:said|says)"), "statement by {}"),
]


def attribution(text):
    hits = []
    for rx, fmt in ATTR:
        for m in rx.finditer(text):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
            if name and len(name) < 60:
                hits.append(fmt.format(name))
    return sorted(set(hits))[:4]


def shingles(text, n=8):
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(w[k:k + n]) for k in range(max(0, len(w) - n + 1))}


def judge(articles, think):
    ids, lines = {}, []
    for k, a in enumerate(articles, 1):
        aid = f"A{k}"
        ids[aid] = a
        hint = (" [hints: " + "; ".join(a["hints"]) + "]") if a["hints"] else ""
        lines.append(f"[{aid}] ({a['bucket']}) {a['outlet']}: {a['title']}. {a['summary'][: S.get('judge_chars', 450)]}{hint}")
    system = open(os.path.join(HERE, "CLAUDE.md")).read()
    text, _usage = LLM.call_local(CFG, system, "Articles:\n" + "\n\n".join(lines), think)
    return json.loads(text[text.find("{"): text.rfind("}") + 1]), ids


STOP = {"the", "a", "an", "of", "to", "in", "on", "and", "for", "is", "was", "that", "with", "by"}


def words(s):
    return set(re.findall(r"[a-z0-9]+", s.lower())) - STOP


def overlap(a, b):
    wa, wb = words(a), words(b)
    return len(wa & wb) / (len(wa | wb) or 1)


def merge(primary, check, ids):
    claims = (primary or check).get("claims", [])
    checked = check.get("claims", []) if (primary and check) else []
    for c in claims:
        c["tier"] = str(c.get("tier", "")).lower()
        src = [s for s in c.get("sources", []) if s in ids]
        c["sources"] = src
        if c["tier"] == "corroborated":
            buckets = {ids[s]["bucket"] for s in src} - {"primary"}
            hint_sets = [{h for h in ids[s]["hints"] if h.startswith(("statement", "according", "wire", "via"))} for s in src]
            common = set.intersection(*hint_sets) if hint_sets and all(hint_sets) else set()
            if len(buckets) < 2:
                c["tier"], c["note"] = "unverified", "only one lean bucket among the sources"
            elif common:
                c["tier"], c["note"] = "unverified", "every source traces to " + sorted(common)[0]
        if c["tier"] == "corroborated" and checked:
            best = max(checked, key=lambda x: overlap(c.get("claim", ""), x.get("claim", "")))
            if overlap(c.get("claim", ""), best.get("claim", "")) >= 0.35 and str(best.get("tier", "")).lower() != "corroborated":
                c["tier"], c["note"] = "unverified", "reasoning and quick passes disagreed"
    return claims


def primary_docs():
    docs = []
    y = (TODAY - dt.timedelta(days=1)).isoformat()
    base = "https://www.federalregister.gov/api/v1/documents.json?"
    for extra in ("conditions[type][]=PRESDOCU", "conditions[type][]=RULE&conditions[significant]=1"):
        q = f"per_page=40&order=newest&conditions[publication_date][gte]={y}&{extra}"
        q += "&fields[]=title&fields[]=type&fields[]=html_url&fields[]=abstract&fields[]=publication_date"
        try:
            data = json.loads(fetch(base + q, accept="application/json"))
            for d in data.get("results", []):
                docs.append({"src": "Federal Register", "title": d.get("title", ""), "url": d.get("html_url", ""),
                             "text": clean(d.get("abstract") or "")[:300], "kind": d.get("type", "")})
        except Exception as e:  # noqa: BLE001
            log(f"federal register query failed: {type(e).__name__}: {str(e)[:80]}")
    key = env_value("CONGRESS_API_KEY")
    if key:
        frm = (NOW - dt.timedelta(hours=S["window_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = urllib.parse.urlencode({"fromDateTime": frm, "sort": "updateDate desc", "limit": 250, "format": "json", "api_key": key})
        rx = re.compile(r"passed|became public law|signed by (the )?president|vetoed|presented to (the )?president|agreed to|cloture|failed of passage", re.I)
        try:
            data = json.loads(fetch("https://api.congress.gov/v3/bill?" + q, accept="application/json"))
            for b in data.get("bills", []):
                act = (b.get("latestAction") or {}).get("text", "")
                if rx.search(act):
                    btype = str(b.get("type", "")).lower()
                    kind = {"hr": "house-bill", "s": "senate-bill", "hres": "house-resolution", "sres": "senate-resolution",
                            "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution",
                            "hconres": "house-concurrent-resolution", "sconres": "senate-concurrent-resolution"}.get(btype, "bill")
                    url = f"https://www.congress.gov/bill/{b.get('congress')}th-congress/{kind}/{b.get('number')}"
                    docs.append({"src": "Congress.gov", "title": f"{b.get('type', '')} {b.get('number', '')}: {b.get('title', '')}",
                                 "url": url, "text": act[:300], "kind": "bill action"})
        except Exception as e:  # noqa: BLE001
            log(f"congress.gov query failed: {type(e).__name__}")
    return docs


def main():
    items, health = collect()
    exc = re.compile(S.get("exclude_regex") or r"$^", re.I)
    news = [i for i in items if i["section"] != "primary"
            and not exc.search(i["title"]) and not (" | " in i["title"] and i["feed"].startswith("guardian"))]
    prim_feed = [i for i in items if i["section"] == "primary"]
    log(f"{len(news)} news items, {len(prim_feed)} primary items from {len(CONF['feeds'])} feeds")
    if len(news) < 10:
        log("too few items, no report")
        return 1
    for i in news:
        i["hints"] = attribution(f"{i['title']}. {i['summary']}")
    vecs = embed(["clustering: " + i["title"] + ". " + i["summary"][:400] for i in news])
    groups = cluster(vecs, S["cluster_threshold"], S.get("cluster_gap", 0.08))
    wl = CONF.get("watchlist") or []
    watch = re.compile(r"\b(" + "|".join(re.escape(w) for w in wl) + r")\b", re.I) if wl else None
    stories = []
    for g in groups:
        mem = [news[k] for k in g["members"]]
        outlets = {m["outlet"] for m in mem}
        buckets = {m["bucket"] for m in mem}
        nworld = sum(1 for m in mem if m["section"] == "world")
        watched = bool(watch and any(watch.search(m["title"] + " " + m["summary"]) for m in mem))
        world = nworld > len(mem) / 2 or (watched and nworld > 0)
        need = S["world_min_outlets"] if world else S["us_min_outlets"]
        if (len(outlets) >= need and len(buckets) >= S["min_buckets"]) or (world and watched and len(outlets) >= 2):
            stories.append({"world": world, "watched": watched, "members": mem, "centroid": g["centroid"],
                            "score": (len(buckets), len(outlets))})
    us = sorted([s for s in stories if not s["world"]], key=lambda s: s["score"], reverse=True)[: S["max_us"]]
    wd = sorted([s for s in stories if s["world"]], key=lambda s: (s["watched"], s["score"]), reverse=True)[: S["max_world"]]
    log(f"{len(groups)} clusters; reporting {len(us)} US and {len(wd)} world stories")

    docs = primary_docs()
    docs += [{"src": i["outlet"], "title": i["title"], "url": i["link"], "text": i["summary"][:300], "kind": "release"} for i in prim_feed]
    dvec = embed(["clustering: " + d["title"] + ". " + d["text"] for d in docs]) if docs else []

    stats = {"reasoned": 0, "quick_only": 0, "failed": 0}
    for st in us + wd:
        by_outlet = {}
        for m in sorted(st["members"], key=lambda m: m["date"] or NOW, reverse=True):
            by_outlet.setdefault(m["outlet"], m)  # newest article per outlet: one outlet never counts twice
        # balanced, compact input: up to N newest outlets per lean bucket (one article each)
        per_bucket = {}
        for a in by_outlet.values():
            per_bucket.setdefault(a["bucket"], []).append(a)
        arts = [a for b in sorted(per_bucket) for a in per_bucket[b][: S.get("per_bucket_judge", 2)]]
        sh = [shingles(a["summary"]) for a in arts]
        for ai in range(len(arts)):
            for bi in range(ai):
                inter = len(sh[ai] & sh[bi])
                if inter and inter / (len(sh[ai] | sh[bi]) or 1) > 0.3:
                    arts[ai]["hints"] = sorted(set(arts[ai]["hints"] + [f"near-duplicate text of A{bi + 1}"]))
        st["articles"] = arts
        prim = chk = ids = None
        think = S["think_budget"] if (time.monotonic() - T0) < S["deadline_minutes"] * 60 else 0
        if think:
            try:
                prim, ids = judge(arts, think)
            except Exception as e:  # noqa: BLE001
                log(f"reasoning pass failed ({str(e)[:70]}); using quick pass")
        try:
            chk, ids2 = judge(arts, 0)
            ids = ids or ids2
        except Exception as e:  # noqa: BLE001
            log(f"quick pass failed ({str(e)[:70]})")
        st["related"] = [d for d, v in zip(docs, dvec) if dot(v, st["centroid"]) >= S["primary_link_threshold"]][:3]
        if prim is None and chk is None:
            st["claims"], st["headline"], st["mode"] = [], arts[0]["title"], "not judged"
            stats["failed"] += 1
            continue
        st["claims"] = merge(prim, chk or {}, ids)
        st["ids"] = {k: {"outlet": v["outlet"], "bucket": v["bucket"], "link": v["link"]} for k, v in ids.items()}
        st["headline"] = (prim or chk).get("headline") or arts[0]["title"]
        st["mode"] = "reasoned + cross-checked" if (prim and chk) else ("reasoned" if prim else "quick pass only")
        stats["reasoned" if prim else "quick_only"] += 1
        log(f"judged: {st['headline'][:70]} ({st['mode']})")

    write_report(us, wd, docs, health, stats, len(news))
    return 0


def fmt_story(st):
    out = [f"### {st['headline']}"]
    arts = st.get("articles", [])
    lean = {}
    for a in arts:
        lean.setdefault(a["bucket"], []).append(a["outlet"])
    cov = "; ".join(f"{b}: {', '.join(v)}" for b, v in sorted(lean.items()))
    out.append(f"*Coverage ({len(arts)} outlets):* {cov}. *Judged:* {st.get('mode')}.")
    ids = st.get("ids") or {}

    def srcs(c):
        return ", ".join(f"[{ids[s]['outlet']}]({ids[s]['link']})" for s in c.get("sources", []) if s in ids)
    for key, label in [("corroborated", "Corroborated across outlets"), ("contested", "Contested"),
                       ("unverified", "Single-source / unverified"), ("framing", "Framing (how each side describes it)")]:
        rows = [c for c in st.get("claims", []) if c.get("tier") == key]
        if rows:
            out.append(f"**{label}**")
            for c in rows:
                note = f" _({c['note']})_" if c.get("note") else ""
                out.append(f"- {str(c.get('claim', '')).strip()} — {srcs(c)}{note}")
    if not st.get("claims"):
        out.append("_Not judged. Headlines:_ " + "; ".join(f"[{a['outlet']}]({a['link']})" for a in arts))
    if st.get("related"):
        out.append("*Related primary documents:* " + "; ".join(f"[{d['src']}: {d['title'][:90]}]({d['url']})" for d in st["related"]))
    return "\n".join(out)


def write_report(us, wd, docs, health, stats, nitems):
    ncorr = sum(1 for st in us + wd for c in st.get("claims", []) if c.get("tier") == "corroborated")
    lines = [f"# OSINT policy digest, {TODAY.isoformat()}", "",
             f"Verdict: {len(us)} US and {len(wd)} world stories, {ncorr} corroborated facts.", "", "## United States", ""]
    lines += [fmt_story(st) + "\n" for st in us] or ["_No US story met the coverage bar._", ""]
    lines += ["## World (trending)", ""]
    lines += [fmt_story(st) + "\n" for st in wd] or ["_No world story met the coverage bar._", ""]
    lines += ["## Primary documents (last ~24 h)", ""]
    for src in sorted({d["src"] for d in docs}):
        lines.append(f"**{src}**")
        lines += [f"- [{d['title'][:140]}]({d['url']})" + (f" — {d['text'][:160]}" if d["kind"] == "bill action" else "")
                  for d in docs if d["src"] == src][:15]
        lines.append("")
    lines += ["## Feed health", ""] + [f"- {n}: {s}" for n, s in health]
    secs = time.monotonic() - T0
    lines += ["", "---", f"Written by {CFG['LOCAL_LLM_MODEL']} (local) from {nitems} items in {secs / 60:.0f} min; "
              f"stories reasoned+checked {stats['reasoned']}, quick only {stats['quick_only']}, not judged {stats['failed']}."]
    os.makedirs(os.path.join(HERE, "reports", ".raw"), exist_ok=True)
    path = os.path.join(HERE, "reports", f"{TODAY.isoformat()}.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    raw = [{"headline": st.get("headline"), "world": st["world"], "mode": st.get("mode"),
            "claims": st.get("claims"), "ids": st.get("ids")} for st in us + wd]
    with open(os.path.join(HERE, "reports", ".raw", f"{TODAY.isoformat()}.json"), "w") as f:
        json.dump(raw, f, indent=1, default=str)
    log(f"wrote {path} ({len(us)} US, {len(wd)} world, {ncorr} corroborated, {secs / 60:.0f} min)")


if __name__ == "__main__":
    sys.exit(main())
