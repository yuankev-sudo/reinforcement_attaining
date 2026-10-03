"""Builds the end-of-session report (Markdown, standalone HTML and JSON)."""

from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path
from typing import Any

from .session import SEVERITY_ORDER, Session


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _duration(s: Session) -> str:
    secs = int((s.ended_at or time.time()) - s.started_at)
    return f"{secs // 60}m {secs % 60:02d}s"


def _grade(score: int) -> str:
    if score >= 85:
        return "Strong - could teach this"
    if score >= 70:
        return "Good - a few gaps"
    if score >= 50:
        return "Developing - notable gaps"
    return "Needs work - core misconceptions"


def build_report(
    s: Session,
    summary: str,
    strengths: list[str],
    gaps: list[str],
    next_steps: list[str],
    ai_score: int | None = None,
) -> dict[str, Any]:
    computed = s.score()
    score = ai_score if ai_score is not None else computed
    # Concept rollup: worst assessment per concept.
    concepts: dict[str, dict[str, Any]] = {}
    for a in s.assessments:
        c = concepts.setdefault(a.concept, {"concept": a.concept, "worst_severity": "none", "max_deviation": 0, "notes": []})
        c["max_deviation"] = max(c["max_deviation"], a.deviation)
        if SEVERITY_ORDER[a.severity] > SEVERITY_ORDER[c["worst_severity"]]:
            c["worst_severity"] = a.severity
        c["notes"].append(a.note)
    return {
        "session_id": s.id,
        "topic": s.topic,
        "scope": s.scope,
        "learner_name": s.learner_name,
        "started": _fmt_time(s.started_at),
        "duration": _duration(s),
        "score": score,
        "computed_score": computed,
        "grade": _grade(score),
        "summary": summary,
        "strengths": strengths,
        "gaps": gaps,
        "next_steps": next_steps,
        "concepts": list(concepts.values()),
        "flags": [
            {"concept": a.concept, "severity": a.severity, "deviation": a.deviation, "note": a.note, "correction": a.correction}
            for a in s.flags
        ],
        "transcript": [
            {"question": t.question, "answer": t.answer, "images": [img.data_url() for img in t.snapshots]}
            for t in s.turns
        ],
    }


def to_markdown(r: dict[str, Any]) -> str:
    lines = [
        f"# Teach-back report: {r['topic']}",
        "",
        f"**Teacher:** {r['learner_name']}  |  **Date:** {r['started']}  |  **Duration:** {r['duration']}",
        f"**Score:** {r['score']}/100 - {r['grade']}",
        "",
        "## Summary",
        r["summary"] or "_(none)_",
        "",
        "## Strengths",
        *[f"- {x}" for x in r["strengths"] or ["_(none recorded)_"]],
        "",
        "## Knowledge gaps",
        *[f"- {x}" for x in r["gaps"] or ["_(none recorded)_"]],
        "",
        "## Flagged errors (corrected during the session)",
    ]
    if r["flags"]:
        for f in r["flags"]:
            lines.append(f"- **[{f['severity'].upper()}] {f['concept']}** - {f['note']}")
            if f["correction"]:
                lines.append(f"  - Correct idea: {f['correction']}")
    else:
        lines.append("_No major errors were flagged._")
    lines += ["", "## Concept breakdown", "", "| Concept | Worst severity | Max deviation (0-10) |", "|---|---|---|"]
    lines += [f"| {c['concept']} | {c['worst_severity']} | {c['max_deviation']} |" for c in r["concepts"]]
    lines += ["", "## Recommended next steps", *[f"- {x}" for x in r["next_steps"] or ["_(none)_"]], "", "## Transcript", ""]
    for i, t in enumerate(r["transcript"], 1):
        lines += [f"**Q{i} (AI):** {t['question']}", "", f"**A{i} (you):** {t['answer'] or '_(unanswered)_'}"]
        if t["images"]:
            lines.append(f"_({len(t['images'])} drawing/whiteboard image(s) - see HTML report)_")
        lines.append("")
    return "\n".join(lines)


def to_html(r: dict[str, Any]) -> str:
    e = html.escape

    def ul(items: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{e(x)}</li>" for x in items) + "</ul>" if items else "<p class=muted>None recorded.</p>"

    flags = "".join(
        f"<div class='flag sev-{e(f['severity'])}'><b>[{e(f['severity'].upper())}] {e(f['concept'])}</b><p>{e(f['note'])}</p>"
        + (f"<p class=fix>Correct idea: {e(f['correction'])}</p>" if f["correction"] else "")
        + "</div>"
        for f in r["flags"]
    ) or "<p class=muted>No major errors were flagged.</p>"
    concepts = "".join(
        f"<tr><td>{e(c['concept'])}</td><td><span class='pill sev-{e(c['worst_severity'])}'>{e(c['worst_severity'])}</span></td>"
        f"<td><div class=bar><i style='width:{c['max_deviation'] * 10}%'></i></div> {c['max_deviation']}/10</td></tr>"
        for c in r["concepts"]
    )
    transcript = "".join(
        f"<div class=turn><p class=q><b>AI:</b> {e(t['question'])}</p><p class=a><b>You:</b> {e(t['answer'] or '(unanswered)')}</p>"
        + "".join(f"<img src='{src}' alt='diagram'>" for src in t["images"])
        + "</div>"
        for t in r["transcript"]
    )
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Teach-back report</title><style>
:root{{--bg:#fafaf7;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e4e4e0;--accent:#3b6ef5;--ok:#1f9d55;--warn:#d98a00;--bad:#d64545}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#ececf0;--muted:#9a9aa2;--card:#1e1e22;--line:#2e2e34}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:860px;margin:auto}} h1{{margin:0 0 4px}} .muted{{color:var(--muted)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin:16px 0}}
.score{{font-size:48px;font-weight:700;color:var(--accent)}} table{{width:100%;border-collapse:collapse}}
td,th{{text-align:left;padding:6px;border-bottom:1px solid var(--line)}}
.bar{{display:inline-block;width:120px;height:8px;background:var(--line);border-radius:4px;vertical-align:middle}}
.bar i{{display:block;height:100%;background:var(--warn);border-radius:4px}}
.pill{{padding:2px 8px;border-radius:99px;font-size:13px;background:var(--line)}}
.sev-none{{color:var(--ok)}} .sev-minor{{color:var(--warn)}} .sev-major,.sev-critical{{color:var(--bad)}}
.flag{{border-left:4px solid var(--bad);padding:4px 12px;margin:8px 0}} .flag p{{margin:4px 0;color:var(--fg)}} .fix{{color:var(--ok)!important}}
.turn{{border-top:1px solid var(--line);padding:8px 0}} .turn img{{max-width:100%;border:1px solid var(--line);border-radius:8px;margin:4px 0;background:#fff}}
.q{{color:var(--accent)}}
</style></head><body><main>
<h1>Teach-back report: {e(r['topic'])}</h1>
<p class=muted>{e(r['learner_name'])} &middot; {e(r['started'])} &middot; {e(r['duration'])} &middot; {e(r['scope'])} topic</p>
<section><div class=score>{r['score']}<small class=muted>/100</small></div><b>{e(r['grade'])}</b><p>{e(r['summary'])}</p></section>
<section><h2>Strengths</h2>{ul(r['strengths'])}<h2>Knowledge gaps</h2>{ul(r['gaps'])}</section>
<section><h2>Flagged errors</h2>{flags}</section>
<section><h2>Concept breakdown</h2><table><tr><th>Concept</th><th>Worst severity</th><th>Deviation</th></tr>{concepts}</table></section>
<section><h2>Next steps</h2>{ul(r['next_steps'])}</section>
<section><h2>Transcript</h2>{transcript or '<p class=muted>No exchanges.</p>'}</section>
</main></body></html>"""


def save_report(r: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", r["topic"].lower()).strip("-")[:40] or "session"
    base = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{r['session_id']}"
    paths = {"html": base.with_suffix(".html"), "md": base.with_suffix(".md"), "json": base.with_suffix(".json")}
    paths["html"].write_text(to_html(r), encoding="utf-8")
    paths["md"].write_text(to_markdown(r), encoding="utf-8")
    paths["json"].write_text(json.dumps(r, indent=2), encoding="utf-8")
    return paths
