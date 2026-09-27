"""
Draw the v3 held-out thirty: the articles that decide whether the binary gate
is adopted.

The binary prompt was tuned on the v2 thirty (ten as worked examples, twenty as a
dev set), so its accuracy can only be measured on articles it has never seen.
This draws them, writes a labelling page with two buttons, and seals the key.

**It prints counts and nothing else**, for the reason in `blind_sample.py`: a
verdict printed next to a title is contamination nobody can measure afterwards.

The rules of the draw:

- **Test-set countries only**, from each one's most recent candidate pool: after
  retrieval and dedupe, before the gate. The census keeps every candidate the
  gate saw, including the ones it rejected, which is where its errors live.
- **Full articles only.** The digest's `body_quality` must be `full`. The old
  gate admitted only some candidates, so the rest have never been digested; they
  are digested here, and those digests are cached like any other.
- **Nothing already seen.** Every v1 and v2 URL is excluded. So is every US, PT
  and KW URL from the 2026-09-22 pool, because Session A's bake-off drew from it
  and printed verdicts next to titles.
- **No duplicate events.** Titles that share most of their words are one story,
  and one of them is kept.
- **At most six per country**, stratified on the old gate's label (`structural`
  as likely relevant, anything else as likely irrelevant) to get roughly half of
  each. The old label is used to stratify, never to score.

    python -m backend.llm.blind_draw_v3
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
from collections import defaultdict
from typing import Any, Dict, List, Sequence

from backend.data_upsert import store
from backend.llm import digest_engine
from backend.news_fetching.source_filter import is_blocked_url
from backend.util import constants, db, paths, usage
from backend.util.hashing import content_hash

__all__ = ["build"]

SEED = 20260927
SAMPLE_SIZE = 30
PER_COUNTRY = 6
PREVIEW_CHARS = 2500
SAME_STORY = 0.5          # title word overlap at which two candidates are one story

DOCS = paths.PROJECT_ROOT / "docs"
OUT_HTML = DOCS / "blind-labels-v3.html"
OUT_KEY = DOCS / "blind-labels-v3-key.json"
SEEN_KEYS = (DOCS / "blind-labels-key.json", DOCS / "blind-labels-v1-key.json")
SESSION_A_POOL = paths.PROJECT_ROOT / "backend" / "data" / "backups" / \
    "2026-09-25-neondb-legacy-payload_census.csv"
SESSION_A_COUNTRIES = ("US", "PT", "KW")


def _seen_urls() -> set:
    seen = set()
    for path in SEEN_KEYS:
        key = json.loads(path.read_text(encoding="utf-8"))
        seen |= {r["url"] for r in key["rows"]}
        seen |= {r["removed_url"] for r in key.get("replacements", [])}
    csv.field_size_limit(10 ** 9)
    with open(SESSION_A_POOL, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["country_iso2"] in SESSION_A_COUNTRIES:
                for g in json.loads(row["articles"]).get("gate_labels", []):
                    seen.add(g["url"])
    return seen


def _latest_pools(countries: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    """``{iso2: census gate_labels}`` from each country's most recent snapshot."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (country_iso2) country_iso2,
                       manifest->'census'->'articles'->'gate_labels'
                  FROM risk_snapshot
                 WHERE country_iso2 = ANY(%s)
                 ORDER BY country_iso2, run_date DESC
                """,
                (list(countries),),
            )
            return {iso2: rows or [] for iso2, rows in cur.fetchall()}
    finally:
        conn.close()


def _words(title: str) -> set:
    return {w for w in title.lower().split() if len(w) > 3}


def _one_per_story(rows: List[Dict[str, Any]], rng: random.Random) -> List[Dict[str, Any]]:
    """Keep one candidate per story, across countries, chosen at random."""
    rows = list(rows)
    rng.shuffle(rows)
    kept: List[Dict[str, Any]] = []
    for r in rows:
        w = _words(r["title"])
        if any(w and k["_words"] and len(w & k["_words"]) / len(w | k["_words"]) >= SAME_STORY
               for k in kept):
            continue
        if any(r["body_sha"] == k["body_sha"] for k in kept):
            continue
        r["_words"] = w
        kept.append(r)
    return kept


def _page(picked: Sequence[Dict[str, Any]]) -> str:
    data = [
        {"id": r["id"], "country": r["country"], "publisher": r["publisher"],
         "date": r["date"], "title": r["title"], "body": r["body"]}
        for r in picked
    ]
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", blob).replace("__N__", str(len(picked))) \
                    .replace("__PREVIEW__", str(PREVIEW_CHARS))


def build() -> int:
    countries = list(constants.TEST_SET)
    seen = _seen_urls()
    pools = _latest_pools(countries)

    candidates: List[Dict[str, Any]] = []
    excluded = defaultdict(int)
    for iso2, gate_labels in pools.items():
        stored = store.read_articles([g["url"] for g in gate_labels if g.get("url")])
        for g in gate_labels:
            url = g.get("url")
            a = stored.get(url) or {}
            body = (a.get("body") or "").strip()
            if not url or url in seen:
                excluded["already seen"] += 1
                continue
            if is_blocked_url(url):
                excluded["denylisted"] += 1
                continue
            if not body or not (a.get("title") or "").strip():
                excluded["no stored body"] += 1
                continue
            candidates.append({
                "url": url, "iso2": iso2,
                "country": constants.COUNTRY_NAME_BY_ISO2.get(iso2, iso2),
                "title": a["title"].strip(),
                "publisher": (a.get("publisher") or "").strip() or "(unknown)",
                "date": str(a.get("page_published_at") or a.get("published_at") or "")[:10],
                "body": body, "body_sha": content_hash(body),
                "old_label": g.get("label"),
                "stratum": "likely relevant" if g.get("label") == "structural" else "likely irrelevant",
            })
    print(f"[draw] {sum(len(v) for v in pools.values())} candidates in "
          f"{len(pools)} test-set pools; {len(candidates)} with a body and unseen")
    print(f"[draw] excluded: {dict(excluded)}")

    # Full articles only. Cached verdicts first; the rest are digested now.
    items = [{"publisher_link": c["url"], "text": c["body"]} for c in candidates]
    quality = digest_engine.cached_body_quality(items)
    todo = [i for i in items if i["publisher_link"] not in quality]
    usage.project("v3 draw digests", model=digest_engine.DEFAULT_MODEL, calls=len(todo),
                  input_tokens_per_call=3500, output_tokens_per_call=250, cap_usd=1.0)
    meter = usage.Meter()
    if todo:
        got = digest_engine.digest_articles(todo, meter=meter)
        for url, d in got["digests"].items():
            if d and d.get("body_quality"):
                quality[url] = d["body_quality"]
    print(f"[draw] body quality known for {len(quality)} "
          f"({len(items) - len(todo)} cached, {len(todo)} digested now); {meter.summary()}")
    counts = defaultdict(int)
    for c in candidates:
        c["body_quality"] = quality.get(c["url"], "unknown")
        counts[c["body_quality"]] += 1
    print(f"[draw] body quality: {dict(counts)}")
    full = [c for c in candidates if c["body_quality"] == "full"]

    rng = random.Random(SEED)
    full = _one_per_story(full, rng)
    print(f"[draw] {len(full)} full articles after one-per-story")

    by_cell: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for c in full:
        by_cell[(c["iso2"], c["stratum"])].append(c)
    for rows in by_cell.values():
        rng.shuffle(rows)

    picked: List[Dict[str, Any]] = []
    half = PER_COUNTRY // 2
    for iso2 in countries:
        rel = by_cell[(iso2, "likely relevant")]
        irr = by_cell[(iso2, "likely irrelevant")]
        take_rel = min(half, len(rel))
        take_irr = min(PER_COUNTRY - take_rel, len(irr))
        take_rel = min(PER_COUNTRY - take_irr, len(rel))   # top up if irr ran short
        picked += rel[:take_rel] + irr[:take_irr]
    picked = picked[:SAMPLE_SIZE]
    rng.shuffle(picked)
    for i, r in enumerate(picked, start=1):
        r["id"] = f"h{i:02d}"

    per = defaultdict(lambda: defaultdict(int))
    for r in picked:
        per[r["iso2"]][r["stratum"]] += 1
    print(f"[draw] drew {len(picked)}: " +
          "; ".join(f"{k} {dict(v)}" for k, v in sorted(per.items())))

    OUT_HTML.write_text(_page(picked), encoding="utf-8")
    OUT_KEY.write_text(json.dumps({
        "_note": "SEALED. Do not read before Eli's v3 labels are committed.",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "seed": SEED,
        "draw": "v3 held-out: test-set pools, full articles, one per story, "
                "at most 6 per country, stratified on the old gate's label",
        "rows": [
            {"id": r["id"], "url": r["url"], "country_iso2": r["iso2"], "title": r["title"],
             "stratum": r["stratum"], "old_gate_label": r["old_label"],
             "body_quality": r["body_quality"], "content_sha256": r["body_sha"]}
            for r in picked
        ],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[draw] wrote {OUT_HTML.name} (no verdicts) and {OUT_KEY.name} (sealed)")
    return len(picked)


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Held-out Labels</title>
<style>
  :root {
    --bg: #111418; --panel: #1a1f25; --panel-2: #222830; --text: #e6e9ee;
    --muted: #9aa4b2; --line: #2d353f; --accent: #7fb3ff;
    --relevant: #4fb286; --irrelevant: #9a8fd8;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text); }
  body { font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 820px; margin: 0 auto; padding: 24px 16px 120px; }
  h1 { font-size: 1.4rem; margin: 0 0 12px; }
  .rule { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; margin-bottom: 24px; }
  .rule p { margin: 0 0 8px; }
  .rule p:last-child { margin: 0; }
  .bar { position: sticky; top: 0; z-index: 5; background: rgba(17,20,24,.96); border-bottom: 1px solid var(--line);
         padding: 10px 16px; display: flex; gap: 12px; align-items: center; justify-content: space-between; }
  .bar .count { font-weight: 600; }
  .bar .track { flex: 1; height: 6px; background: var(--panel-2); border-radius: 3px; overflow: hidden; }
  .bar .fill { height: 100%; width: 0; background: var(--accent); transition: width .2s; }
  button { font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid var(--line);
           background: var(--panel-2); color: var(--text); padding: 8px 14px; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  #download:not(:disabled) { background: var(--accent); color: #0b1220; border-color: var(--accent); font-weight: 600; }
  article { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 18px; margin: 0 0 18px; }
  article.done { border-color: #3a4552; }
  .meta { color: var(--muted); font-size: .9rem; display: flex; flex-wrap: wrap; gap: 4px 14px; }
  .meta .id { color: var(--text); font-weight: 700; }
  h2 { font-size: 1.15rem; line-height: 1.4; margin: 8px 0 12px; }
  .body { white-space: pre-wrap; overflow-wrap: anywhere; color: #d5dae1; }
  .more { margin-top: 8px; background: none; border: none; color: var(--accent); padding: 0; }
  .choices { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
  .choices button[aria-pressed="true"].relevant { background: var(--relevant); border-color: var(--relevant); color: #0b1a12; }
  .choices button[aria-pressed="true"].irrelevant { background: var(--irrelevant); border-color: var(--irrelevant); color: #120f24; }
  .note { color: var(--muted); font-size: .9rem; }
</style>
</head>
<body>
<div class="bar">
  <span class="count" id="count">0 / __N__</span>
  <div class="track"><div class="fill" id="fill"></div></div>
  <button id="download" disabled>Download CSV</button>
</div>
<div class="wrap">
  <h1>Held-out labels</h1>
  <div class="rule">
    <p><strong>Is this article material to the risk of holding investments exposed to the country named on the card?</strong></p>
    <p><strong>Relevant</strong> if it bears on that country's cost of doing business, its order and security, or whether you can trust what you know about it.</p>
    <p><strong>Irrelevant</strong> otherwise.</p>
  </div>
  <p class="note">Your choices are kept in this browser while you work. The download unlocks at __N__ of __N__ and
  saves <code>blind-labels-v3.csv</code> with the columns <code>id</code> and <code>your_label</code>.</p>
  <div id="list"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var ARTICLES = JSON.parse(document.getElementById("data").textContent);
  var LABELS = ["relevant", "irrelevant"];
  var NAMES = { relevant: "Relevant", irrelevant: "Irrelevant" };
  var PREVIEW = __PREVIEW__;
  var KEY = "blind-labels-v3";
  var chosen = {};
  try { chosen = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { chosen = {}; }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(chosen)); } catch (e) {} }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  var list = document.getElementById("list");
  ARTICLES.forEach(function (a) {
    var art = el("article");
    art.id = a.id;
    var meta = el("div", "meta");
    meta.appendChild(el("span", "id", a.id));
    meta.appendChild(el("span", null, a.country));
    meta.appendChild(el("span", null, a.publisher));
    if (a.date) meta.appendChild(el("span", null, a.date));
    art.appendChild(meta);
    art.appendChild(el("h2", null, a.title));
    var body = el("div", "body");
    var full = a.body || "";
    var short = full.length > PREVIEW;
    body.textContent = short ? full.slice(0, PREVIEW) + "\\u2026" : full;
    art.appendChild(body);
    if (short) {
      var label = "Show full article (" + full.length.toLocaleString() + " characters)";
      var more = el("button", "more", label);
      var open = false;
      more.addEventListener("click", function () {
        open = !open;
        body.textContent = open ? full : full.slice(0, PREVIEW) + "\\u2026";
        more.textContent = open ? "Show less" : label;
      });
      art.appendChild(more);
    }
    var choices = el("div", "choices");
    LABELS.forEach(function (lab) {
      var b = el("button", lab, NAMES[lab]);
      b.setAttribute("aria-pressed", chosen[a.id] === lab ? "true" : "false");
      b.addEventListener("click", function () {
        chosen[a.id] = lab;
        save();
        Array.prototype.forEach.call(choices.children, function (c) {
          c.setAttribute("aria-pressed", c === b ? "true" : "false");
        });
        update();
      });
      choices.appendChild(b);
    });
    art.appendChild(choices);
    list.appendChild(art);
  });
  var dl = document.getElementById("download");
  function update() {
    var n = ARTICLES.filter(function (a) { return LABELS.indexOf(chosen[a.id]) >= 0; }).length;
    document.getElementById("count").textContent = n + " / " + ARTICLES.length;
    document.getElementById("fill").style.width = (100 * n / ARTICLES.length) + "%";
    ARTICLES.forEach(function (a) {
      document.getElementById(a.id).classList.toggle("done", !!chosen[a.id]);
    });
    dl.disabled = n !== ARTICLES.length;
  }
  dl.addEventListener("click", function () {
    var rows = ["id,your_label"].concat(ARTICLES.map(function (a) { return a.id + "," + chosen[a.id]; }));
    var blob = new Blob([rows.join("\\n") + "\\n"], { type: "text/csv" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "blind-labels-v3.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  });
  update();
})();
</script>
</body>
</html>
"""


def main() -> None:
    build()


if __name__ == "__main__":
    main()
