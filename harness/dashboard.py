"""Generate a static HTML comparison dashboard from stored runs."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from harness.evals import changed_decisions, summarize_turns
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[1]


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Calvis eval — {title}</title>
<style>
  :root {{
    --bg: #f4f1ea;
    --ink: #1a1a1a;
    --muted: #5c5c5c;
    --line: #d4cfc4;
    --ops: #8b1e1e;
    --ok: #1e5c3a;
    --card: #fffdf8;
    --advisory: #6b4c1e;
    --advisory-bg: #f4e6c1;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    background: var(--bg); color: var(--ink); line-height: 1.45;
  }}
  header {{
    padding: 2rem 2.5rem 1.25rem;
    border-bottom: 1px solid var(--line);
    background: linear-gradient(180deg, #ebe6dc, var(--bg));
  }}
  h1 {{ margin: 0 0 0.35rem; font-size: 1.6rem; letter-spacing: -0.02em; }}
  .sub {{ color: var(--muted); max-width: 52rem; }}
  main {{ padding: 1.5rem 2.5rem 3rem; display: grid; gap: 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  .panel {{
    background: var(--card); border: 1px solid var(--line); padding: 1rem 1.2rem;
  }}
  h2 {{ margin: 0 0 0.75rem; font-size: 1.05rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
  th, td {{ text-align: left; padding: 0.35rem 0.4rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ color: var(--muted); font-weight: 600; }}
  .tier-operational {{ color: var(--ops); font-weight: 600; }}
  .tier-cosmetic {{ color: var(--muted); }}
  .pass {{ color: var(--ok); font-weight: 600; }}
  .fail {{ color: var(--ops); font-weight: 600; }}
  .advisory {{
    color: var(--advisory); background: var(--advisory-bg);
    font-weight: 700; letter-spacing: 0.05em;
    padding: 0.05rem 0.4rem; display: inline-block;
  }}
  .advisory-panel {{ border-color: #c9a227; background: #fbf6e8; }}
  .msg {{ white-space: pre-wrap; font-size: 0.88rem; }}
  .note {{ font-size: 0.85rem; color: var(--muted); margin-top: 0.5rem; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} main, header {{ padding-left: 1rem; padding-right: 1rem; }} }}
</style>
</head>
<body>
<header>
  <h1>Calvis copilot experiment</h1>
  <p class="sub">{subtitle}</p>
</header>
<main>
  <section class="grid">
    <div class="panel">
      <h2>Baseline metrics</h2>
      {baseline_table}
    </div>
    <div class="panel">
      <h2>Variant metrics</h2>
      {variant_table}
    </div>
  </section>

  <section class="panel">
    <h2>Intent assertions</h2>
    {assertions}
    <p class="note">Baseline is a reference, not ground truth. Assertions describe intended change.</p>
  </section>

  <section class="panel advisory-panel">
    <h2><span class="advisory">ADVISORY</span> conduct flags (never a gate)</h2>
    {advisory}
    <p class="note">Deterministic scorers own PASS/FAIL. The checklist judge only narrates must_not_happen flags.</p>
  </section>

  <section class="panel">
    <h2>Changed decisions</h2>
    <p class="note">Operational rows (escalation / coverage / unanswered guard) require human review and are never cleared by an automated judge.</p>
    {changes_table}
  </section>

  <section class="panel">
    <h2>Side-by-side turns (first {n_side} changed)</h2>
    {side_by_side}
  </section>
</main>
</body>
</html>
"""


def _advisory_html(store: ExperimentStore, baseline_id: str, variant_id: str) -> str:
    """Render attached judge snapshots. Distinct from GATE pass/fail cells."""
    from harness.judge import MUST_NOT_HAPPEN_IDS, latest_judge_path

    chunks = []
    for label, run_id in (("Baseline", baseline_id), ("Variant", variant_id)):
        path = latest_judge_path(store.run_dir(run_id))
        if not path:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        flags = data.get("must_not_happen") or []
        flag_txt = ", ".join(html.escape(f) for f in flags) if flags else "(none)"
        rows = []
        for item in data.get("items") or []:
            verdict = html.escape(str(item.get("verdict") or "n_a"))
            flag_mark = ""
            if item.get("id") in MUST_NOT_HAPPEN_IDS and item.get("verdict") == "yes":
                flag_mark = ' <span class="advisory">FLAG</span>'
            quote = html.escape(item.get("quote") or "")
            rows.append(
                f"<tr><td>{html.escape(str(item.get('id','')))}</td>"
                f"<td>{verdict}{flag_mark}</td>"
                f"<td class='msg'>{quote}</td></tr>"
            )
        body = "".join(rows) or "<tr><td colspan=3>No checklist items</td></tr>"
        chunks.append(
            f"<h3>{html.escape(label)} <code>{html.escape(run_id)}</code></h3>"
            f"<p><span class='advisory'>ADVISORY FLAGS</span> {flag_txt}</p>"
            f"<table><tr><th>Item</th><th>Verdict</th><th>Quote</th></tr>{body}</table>"
        )
    if not chunks:
        return (
            "<p class='note'>No advisory checklist attached to these runs. "
            "Run <code>cx judge &lt;run&gt;</code>.</p>"
        )
    return "".join(chunks)


def _metrics_table(s: dict) -> str:
    rows = [
        ("Turns", s.get("turns")),
        ("DMs", s.get("dms")),
        ("Notes", s.get("notes")),
        ("Escalations", s.get("escalation_total")),
        ("Escalation breakdown", json.dumps(s.get("escalations") or {})),
        ("Scheduled no-op rate", s.get("scheduled_noop_rate")),
        ("Guard reply rate", s.get("guard_reply_rate")),
        ("Data gaps", s.get("data_gaps")),
        ("Reduced-confidence turns", s.get("reduced_confidence_turns")),
        ("Cost USD", s.get("cost_usd")),
    ]
    body = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in rows
    )
    return f"<table>{body}</table>"


def _turn_text(t: dict) -> str:
    msgs = t.get("messages") or []
    if not msgs:
        return f"<em>{html.escape(t.get('decision') or 'no_op')}</em>"
    return "<br/>".join(html.escape(m.get("body") or "") for m in msgs)


def build_dashboard(
    store: ExperimentStore,
    baseline_id: str,
    variant_id: str,
    shift_id: str,
    assertion_results: list[dict] | None = None,
    out_path: Path | None = None,
) -> Path:
    baseline = store.load_turns(baseline_id, shift_id)
    variant = store.load_turns(variant_id, shift_id)
    bs = summarize_turns(baseline)
    vs = summarize_turns(variant)
    changes = changed_decisions(baseline, variant)

    bmap = {t["turn"]: t for t in baseline}
    vmap = {t["turn"]: t for t in variant}

    if assertion_results is None:
        assertion_html = "<p class='note'>No assertions evaluated for this render.</p>"
    else:
        rows = []
        for r in assertion_results:
            cls = "pass" if r.get("passed") else "fail"
            mark = "PASS" if r.get("passed") else "FAIL"
            rows.append(
                f"<tr><td class='{cls}'>{mark}</td>"
                f"<td>{html.escape(r.get('id',''))}</td>"
                f"<td>{html.escape(r.get('description',''))}</td>"
                f"<td class='msg'>{html.escape(json.dumps(r.get('detail', {}), default=str))}</td></tr>"
            )
        assertion_html = (
            "<table><tr><th></th><th>ID</th><th>Description</th><th>Detail</th></tr>"
            + "".join(rows) + "</table>"
        )

    advisory_html = _advisory_html(store, baseline_id, variant_id)

    change_rows = []
    for c in changes:
        change_rows.append(
            "<tr>"
            f"<td>{c['turn']}</td><td>{html.escape(str(c.get('trigger')))}</td>"
            f"<td>{html.escape(str(c.get('baseline_decision')))}</td>"
            f"<td>{html.escape(str(c.get('variant_decision')))}</td>"
            f"<td class='tier-{c['tier']}'>{c['tier']}</td>"
            f"<td>{c.get('confidence')}</td>"
            "</tr>"
        )
    changes_table = (
        "<table><tr><th>Turn</th><th>Trigger</th><th>Baseline</th><th>Variant</th>"
        "<th>Tier</th><th>Confidence</th></tr>"
        + ("".join(change_rows) or "<tr><td colspan=6>No decision changes</td></tr>")
        + "</table>"
    )

    side = []
    for c in changes[:12]:
        b = bmap.get(c["turn"], {})
        v = vmap.get(c["turn"], {})
        side.append(
            f"<h3>Turn {c['turn']} · {html.escape(str(c.get('trigger')))} · "
            f"<span class='tier-{c['tier']}'>{c['tier']}</span></h3>"
            "<div class='grid'>"
            f"<div><strong>Baseline</strong><div class='msg'>{_turn_text(b)}</div></div>"
            f"<div><strong>Variant</strong><div class='msg'>{_turn_text(v)}</div></div>"
            "</div>"
        )

    page = TEMPLATE.format(
        title=f"{variant_id} vs {baseline_id} · shift {shift_id}",
        subtitle=(
            f"Baseline run <code>{html.escape(baseline_id)}</code> · "
            f"Variant run <code>{html.escape(variant_id)}</code> · "
            f"Shift <code>{html.escape(shift_id)}</code>. "
            "Anonymized bundle layers may disagree (job.json geography ≠ shift.site); "
            "fixtures served as recorded."
        ),
        baseline_table=_metrics_table(bs),
        variant_table=_metrics_table(vs),
        assertions=assertion_html,
        advisory=advisory_html,
        changes_table=changes_table,
        n_side=min(12, len(changes)),
        side_by_side="".join(side) or "<p class='note'>No changed turns to display.</p>",
    )

    out_path = out_path or (
        ROOT / "runs" / variant_id / f"dashboard_{shift_id}.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--shift", required=True)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    store = ExperimentStore(ROOT / "runs")
    path = build_dashboard(
        store, args.baseline, args.variant, args.shift,
        out_path=Path(args.out) if args.out else None,
    )
    print(path)


if __name__ == "__main__":
    main()
