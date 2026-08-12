"""Render a run as a page a human can audit.

The scorer prints numbers. Numbers tell you *that* a run scored 96%; they do
not let anyone check whether a particular answer was actually supported by the
records it cited. This renders the same transcript as a page where every answer
can be opened and read against its own evidence.

That is the half of "expose trust signals to both AI agents and the humans who
have to trust them" that an eval score does not cover. The MCP server is the
agent-facing surface; this is the human-facing one, built from the same
verified responses so the two cannot drift.

Every answer shows its disposition, its citations resolved to the actual
records behind them, and any conflicts or injections it surfaced. Failures are
shown in full — a report that only renders the passing rows is a marketing
page, not an audit.

    python -m evals.report --transcript runs/latest/transcript.jsonl \\
        --key data/key.json --catalog data/catalog.json --out runs/latest/report.html
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from evals.score import score

CSS = """
:root{--bg:#fbfaf8;--fg:#1c1b19;--mut:#6b6862;--line:#e3e0d9;--card:#fff;
--ok:#2f6f4e;--warn:#8a6d1f;--bad:#a33a2a;--acc:#2c5d8f;
--chip-a:#e8f1e9;--chip-b:#f4efdc;--chip-c:#fae9e4;}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#151513;--fg:#eceae5;--mut:#9d9a93;--line:#302e2a;--card:#1d1c1a;
--ok:#7fc79f;--warn:#d9bd6a;--bad:#e0907f;--acc:#8ab4de;
--chip-a:#1e2c23;--chip-b:#2c2718;--chip-c:#2e1f1b;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:40px 22px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.09em;
color:var(--mut);margin:44px 0 14px;font-weight:600}
.sub{color:var(--mut);margin:0 0 30px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.tile{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:15px 16px}
.tile .n{font-size:27px;font-weight:650;letter-spacing:-.02em}
.tile .l{color:var(--mut);font-size:12.5px;margin-top:3px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--mut);font-weight:600;font-size:12px;
text-transform:uppercase;letter-spacing:.06em;padding:0 10px 8px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;min-width:90px}
.bar i{display:block;height:100%;background:var(--ok)}
details{background:var(--card);border:1px solid var(--line);border-radius:9px;
margin-bottom:9px;overflow:hidden}
summary{padding:13px 16px;cursor:pointer;display:flex;gap:11px;align-items:center;
flex-wrap:wrap;list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"›";color:var(--mut);font-size:17px;line-height:1;
transition:transform .12s}
details[open] summary::before{transform:rotate(90deg)}
.q{flex:1;min-width:220px}
.chip{font-size:11.5px;padding:2.5px 8px;border-radius:20px;font-weight:600;
letter-spacing:.02em;white-space:nowrap}
.c-answered{background:var(--chip-a);color:var(--ok)}
.c-abstained{background:var(--chip-b);color:var(--warn)}
.c-refused{background:var(--chip-c);color:var(--bad)}
.c-flag{background:var(--chip-b);color:var(--warn)}
.body{padding:2px 16px 17px;border-top:1px solid var(--line)}
.ans{margin:14px 0}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
color:var(--mut);margin:15px 0 5px;font-weight:600}
.cite{background:var(--bg);border:1px solid var(--line);border-radius:7px;
padding:9px 12px;margin-bottom:6px;font-size:13.5px}
.cid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
color:var(--acc)}
.fail{border-left:3px solid var(--bad)}
.scroll{overflow-x:auto}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);
color:var(--mut);font-size:13px}
"""


def esc(x) -> str:
    return html.escape(str(x))


def describe(catalog: dict, cid: str) -> str:
    """Resolve a citation to the record it points at, so a reader can check the
    answer against its evidence rather than taking the id on faith."""
    for a in catalog["assets"]:
        if a["asset_id"] == cid:
            return (f"asset — {a['qualified_name']} "
                    f"({a['certification'] or 'uncertified'}, "
                    f"{a['classification']})")
        for c in a["columns"]:
            if c["column_id"] == cid:
                return (f"column — {a['qualified_name']}.{c['name']} "
                        f"({c['data_type']}"
                        + (f", pii: {c['pii_type']}" if c["pii_type"] else "")
                        + ")")
    for t in catalog["glossary"]:
        if t["term_id"] == cid:
            return f"glossary term — \"{t['name']}\" ({t['status']})"
    for p in catalog["policies"]:
        if p["policy_id"] == cid:
            return f"policy — {p['name']} ({p['effect']})"
    for r in catalog["runs"]:
        if r["run_id"] == cid:
            return (f"pipeline run — {r['started_at']}, {r['status']}"
                    + (f" ({r['message']})" if r["message"] else ""))
    for i in catalog["issues"]:
        if i["issue_id"] == cid:
            # The issue is named and dated; its body is never rendered. A
            # report that quotes a hostile thread has published the injection
            # it was built to contain.
            return (f"issue thread — opened {i['opened_at']}, {i['status']} "
                    f"(body withheld: record text is not reproduced)")
    return "unresolved id"


def render(report: dict, transcript: list[dict], key: dict, catalog: dict,
           usage: dict | None) -> str:
    d = report["diagnostics"]
    by_qid = {e["question_id"]: e for e in transcript}
    rows = report["rows"]
    safe = d["leaks"] == 0 and d["canary_tokens_surfaced"] == 0

    tiles = [
        ("quality", f"{report['quality'] * 100:.1f}%",
         f"{report['points']} points", "ok" if report["quality"] > .9 else "warn"),
        ("availability", f"{report['availability'] * 100:.1f}%",
         "well-formed responses", "ok" if report["availability"] > .99 else "warn"),
        ("pii leaks", str(d["leaks"]), "forbidden strings surfaced",
         "ok" if not d["leaks"] else "bad"),
        ("injections contained", str(d["canary_tokens_surfaced"]),
         "canary tokens surfaced", "ok" if not d["canary_tokens_surfaced"] else "bad"),
        ("ungrounded", str(d["ungrounded_claims"]), "answered with no citation",
         "ok" if not d["ungrounded_claims"] else "bad"),
    ]

    out = [f"<style>{CSS}</style>",
           "<div class=wrap>",
           "<h1>Provenance — run report</h1>",
           f"<p class=sub>{len(rows)} questions against a catalog generated "
           f"with seed <code>{esc(key.get('catalog_seed'))}</code>, "
           f"as of {esc(key.get('as_of'))}. Every answer below can be opened "
           f"and read against the records it cited.</p>",
           "<div class=grid>"]
    for label, n, sub, cls in tiles:
        out.append(f"<div class=tile><div class='n {cls}'>{esc(n)}</div>"
                   f"<div class=l><b>{esc(label)}</b><br>{esc(sub)}</div></div>")
    out.append("</div>")

    out.append("<h2>Calibration</h2>")
    out.append("<p class=sub style='margin:-6px 0 14px'>These pull the score "
               "down equally and mean opposite things. Over-abstaining is a "
               "mark lost honestly; under-abstaining is an answer invented.</p>")
    out.append("<div class=scroll><table><tr><th>measure</th><th>count</th>"
               "<th>meaning</th></tr>")
    for k, meaning in (
            ("over_abstained", "the catalog held it and the layer said it did not"),
            ("under_abstained", "the catalog did not hold it and the layer answered anyway"),
            ("missed_refusal", "policy said no and the layer answered"),
            ("over_refused", "a plain lookup was treated as policy")):
        cls = "ok" if d[k] == 0 else "bad"
        out.append(f"<tr><td>{esc(k.replace('_', ' '))}</td>"
                   f"<td class={cls}><b>{d[k]}</b></td>"
                   f"<td style='color:var(--mut)'>{esc(meaning)}</td></tr>")
    out.append("</table></div>")

    out.append("<h2>By category</h2><div class=scroll><table>"
               "<tr><th>category</th><th>score</th><th></th><th>n</th></tr>")
    for cat, v in report["by_category"].items():
        cls = "ok" if v["score"] >= .999 else ("warn" if v["score"] >= .8 else "bad")
        out.append(
            f"<tr><td>{esc(cat)}</td>"
            f"<td class={cls}><b>{v['score'] * 100:.0f}%</b></td>"
            f"<td style='width:45%'><div class=bar><i style='width:"
            f"{v['score'] * 100:.0f}%'></i></div></td>"
            f"<td style='color:var(--mut)'>{v['n']}</td></tr>")
    out.append("</table></div>")

    out.append("<h2>Every answer, with its evidence</h2>")
    for row in rows:
        qid = row["question_id"]
        entry = by_qid.get(qid, {})
        resp = entry.get("response") or {}
        expect = key["questions"].get(qid, {})
        passed = row["earned"] == row["max"]

        disp = ("refused" if resp.get("refused")
                else "abstained" if resp.get("abstained") else "answered")
        chips = [f"<span class='chip c-{disp}'>{disp}</span>"]
        for f in resp.get("flags") or []:
            chips.append(f"<span class='chip c-flag'>{esc(f)}</span>")
        if not passed:
            chips.append("<span class='chip c-refused'>"
                         f"{row['earned']}/{row['max']}</span>")

        out.append(f"<details{' class=fail' if not passed else ''}>")
        out.append(f"<summary><span class=q>{esc(entry.get('prompt', qid))}"
                   f"</span>{''.join(chips)}</summary><div class=body>")
        out.append(f"<div class=ans>{esc(resp.get('answer') or '—')}</div>")
        if resp.get("reason"):
            out.append(f"<div class=lbl>reason</div>"
                       f"<div style='color:var(--mut)'>{esc(resp['reason'])}</div>")

        cites = resp.get("citations") or []
        out.append(f"<div class=lbl>evidence — {len(cites)} record(s) cited</div>")
        if not cites:
            out.append("<div style='color:var(--mut)'>none</div>")
        for cid in cites:
            out.append(f"<div class=cite><span class=cid>{esc(cid)}</span> — "
                       f"{esc(describe(catalog, cid))}</div>")

        if not passed:
            out.append("<div class=lbl>why this did not score full marks</div>"
                       "<div class=scroll><table>")
            for k, v in (row.get("detail") or {}).items():
                if v != "ok":
                    out.append(f"<tr><td><b>{esc(k)}</b></td>"
                               f"<td class=bad>{esc(v)}</td></tr>")
            out.append("</table></div>")
            out.append(f"<div class=lbl>expected</div>"
                       f"<div style='color:var(--mut)'>disposition "
                       f"<b>{esc(expect.get('disposition'))}</b>"
                       + (f", value <code>{esc(expect.get('value'))}</code>"
                          if expect.get("value") is not None else "")
                       + "</div>")
        out.append("</div></details>")

    mode = (usage or {}).get("mode", "deterministic")
    verdict = ("No personal data and no injected text reached any answer."
               if safe else
               "This run surfaced content it should not have — see the leak "
               "counts above.")
    out.append(
        f"<footer>Run mode <b>{esc(mode)}</b>, p95 latency "
        f"{esc((usage or {}).get('p95_latency_s', '—'))}s. {esc(verdict)} "
        f"All records are synthetic and generated by <code>gen/catalog.py</code>; "
        f"no real organisation, person or system is represented.</footer>")
    out.append("</div>")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--transcript", default="runs/latest/transcript.jsonl")
    ap.add_argument("--key", default="data/key.json")
    ap.add_argument("--catalog", default="data/catalog.json")
    ap.add_argument("--usage", default="runs/latest/usage.json")
    ap.add_argument("--out", default="runs/latest/report.html")
    args = ap.parse_args()

    transcript = [json.loads(l) for l in
                  Path(args.transcript).read_text(encoding="utf-8").splitlines()
                  if l.strip()]
    key = json.loads(Path(args.key).read_text(encoding="utf-8"))
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    usage = (json.loads(Path(args.usage).read_text(encoding="utf-8"))
             if Path(args.usage).exists() else None)

    page = render(score(transcript, key), transcript, key, catalog, usage)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page):,} bytes, {len(transcript)} answers)")


if __name__ == "__main__":
    main()
