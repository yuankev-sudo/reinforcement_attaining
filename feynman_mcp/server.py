"""Feynman MCP: an MCP server that turns any AI into a curious newbie student.

The human explains a topic by voice plus a mouse drawing board or a camera
pointed at a real whiteboard. The AI (connected over MCP) asks follow-up
questions, decides per answer whether to flag an error right away or keep
probing, and writes a report at the end.

Two halves run in one process:
  * the MCP server (stdio by default, or streamable HTTP) that the AI talks to
  * a local web UI (aiohttp + WebSocket) the human uses for mic, canvas and camera
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Literal

from aiohttp import WSMsgType, web
from mcp.server.mcpserver import Context, Image, MCPServer

from .report import build_report, save_report, to_html, to_markdown
from .session import FLAG_DEVIATION_THRESHOLD, Assessment, Hub, Turn

log = logging.getLogger("feynman_mcp")

STUDENT_PLAYBOOK = f"""\
You are a curious, humble NEWBIE STUDENT. The human is your teacher. Your job is
to make them explain a topic out loud (and with diagrams) so they discover how
well they really understand it - the Feynman technique.

Loop:
1. start_session(topic, scope). For a "general" topic, first ask the teacher
   which part they want to teach; for a "specific" one, dive straight in.
2. ask_teacher(question). Ask ONE short, natural question at a time, like a
   smart beginner would: "Wait, why does...?", "What happens if...?",
   "Can you draw how X connects to Y?" (set wants_diagram=true for that).
   Your question is spoken aloud, so keep it conversational, < 40 words.
3. After every answer, judge it against the correct understanding and call
   assess_explanation(concept, deviation 0-10, severity, note, correction).
     - deviation 0-2 / severity none: correct. Probe deeper or move on.
     - deviation 3-5 / severity minor: imprecise or incomplete. Do NOT
       correct yet - ask a follow-up that nudges them to notice the gap.
     - deviation >= {FLAG_DEVIATION_THRESHOLD} or severity major/critical: an important knowledge
       error. The tool flags it to the teacher immediately (shown + spoken).
       Then, still in character, ask them to re-explain that piece.
   Look at any drawing/whiteboard images carefully - errors in diagrams count.
4. Cover the key sub-concepts of the topic (aim for ~6-12 exchanges, or until
   the teacher says they are done). Escalate difficulty: definition -> mechanism
   -> edge cases -> applications/connections.
5. end_session(summary, strengths, gaps, next_steps) to generate the report.

Never lecture. Never reveal the full correct answer unless an error was flagged.
Stay in the student persona in everything you ask.
"""

hub = Hub()
CONFIG = {"ui_url": "http://127.0.0.1:8765", "reports_dir": Path("reports"), "open_browser": True}

mcp = MCPServer(
    name="feynman-teachback",
    title="Feynman Teach-Back",
    instructions=STUDENT_PLAYBOOK,
)


def _answer_content(turn: Turn) -> list[str | Image]:
    parts: list[str | Image] = [f'Teacher\'s answer (voice transcript / typed):\n"{turn.answer}"']
    if turn.snapshots:
        parts.append(f"The teacher shared {len(turn.snapshots)} image(s): " + ", ".join(s.source for s in turn.snapshots))
        parts += [Image(data=s.data, format=s.mime.split("/")[-1]) for s in turn.snapshots]
    elif turn.wants_diagram:
        parts.append("(You asked for a diagram but none was shared.)")
    parts.append("Next: call assess_explanation for this answer, then ask your next question.")
    return parts


async def _wait_with_progress(turn: Turn, wait_seconds: float, ctx: Context | None) -> Turn | None:
    """Wait for the teacher, sending progress pings so clients don't time out the call."""
    waited = 0.0
    while waited < wait_seconds:
        chunk = min(10.0, wait_seconds - waited)
        done = await hub.wait_answer(turn, chunk)
        if done:
            return done
        waited += chunk
        if ctx is not None:
            try:
                await ctx.report_progress(waited, wait_seconds, "Waiting for the teacher to answer...")
            except Exception:  # client may not support progress
                pass
    return None


@mcp.tool()
async def start_session(
    topic: str,
    scope: Literal["specific", "general"] = "general",
    teacher_name: str = "Teacher",
) -> str:
    """Start a teach-back session and open the teacher's whiteboard/voice UI.

    topic: what the human will teach you (e.g. "TCP handshake" or "machine learning").
    scope: "specific" for a narrow concept, "general" for a broad field (you'll ask which part first).
    """
    s = hub.start(topic, scope, teacher_name)
    await hub.broadcast({"type": "session", "state": s.public_state()})
    opened = ""
    if CONFIG["open_browser"] and not hub.clients:
        try:
            webbrowser.open(CONFIG["ui_url"])
            opened = " (opened in the browser)"
        except Exception:
            pass
    return (
        f"Session {s.id} started on '{topic}' ({scope}).\n"
        f"Teacher UI: {CONFIG['ui_url']}{opened} - tell the teacher to open it if it isn't up, "
        "and to allow mic/camera.\n\n" + STUDENT_PLAYBOOK
    )


@mcp.tool()
async def ask_teacher(
    question: str,
    wants_diagram: bool = False,
    wait_seconds: int = 240,
    ctx: Context | None = None,
) -> list[str | Image]:
    """Ask the teacher one question as a curious newbie. The question is shown and spoken aloud.

    Blocks until the teacher answers by voice/typing (plus any drawing or whiteboard photo),
    or until wait_seconds passes - then call wait_for_answer to keep waiting.
    Set wants_diagram=true when you want them to draw or use the whiteboard.
    """
    turn = await hub.ask(question, wants_diagram)
    if not hub.clients:
        return [f"Question queued, but no teacher UI is connected. Ask the human to open {CONFIG['ui_url']}, then call wait_for_answer."]
    done = await _wait_with_progress(turn, wait_seconds, ctx)
    if not done:
        return ["The teacher is still answering. Call wait_for_answer to keep waiting."]
    return _answer_content(done)


@mcp.tool()
async def wait_for_answer(wait_seconds: int = 240, ctx: Context | None = None) -> list[str | Image]:
    """Keep waiting for the teacher's answer to the most recent question."""
    turn = hub.require().current_turn()
    if not turn:
        return ["No question has been asked yet. Use ask_teacher."]
    done = await _wait_with_progress(turn, wait_seconds, ctx)
    if not done:
        return ["Still no answer. Call wait_for_answer again, or ask_teacher with a simpler question."]
    return _answer_content(done)


@mcp.tool()
async def look_at_board(source: Literal["canvas", "camera", "both"] = "both") -> list[str | Image]:
    """Peek at the teacher's drawing board and/or whiteboard camera right now, without asking anything."""
    hub.require()
    snaps = await hub.request_snapshot(source)
    if not snaps:
        return ["Nothing to see: no UI connected, the canvas is empty, or the camera is off."]
    return [f"Current board ({', '.join(s.source for s in snaps)}):"] + [
        Image(data=s.data, format=s.mime.split("/")[-1]) for s in snaps
    ]


@mcp.tool()
async def assess_explanation(
    concept: str,
    deviation: int,
    severity: Literal["none", "minor", "major", "critical"],
    note: str,
    correction: str | None = None,
) -> str:
    """Record how far the teacher's latest explanation is from the correct understanding.

    concept: the sub-concept being judged (e.g. "SYN/ACK ordering").
    deviation: 0 (spot on) .. 10 (completely wrong).
    severity: none | minor (imprecise, keep probing) | major/critical (important error, flag now).
    note: what they said that was right/wrong, in one or two sentences.
    correction: the correct idea - required when severity is major/critical.
    Returns what to do next.
    """
    s = hub.require()
    deviation = max(0, min(10, int(deviation)))
    flag = severity in ("major", "critical") or deviation >= FLAG_DEVIATION_THRESHOLD
    if flag and severity in ("none", "minor"):
        severity = "major"
    turn = s.current_turn()
    a = Assessment(concept, deviation, severity, note, correction, flag, turn.id if turn else None)
    s.assessments.append(a)
    if flag:
        await hub.broadcast(
            {"type": "flag", "concept": concept, "severity": severity, "note": note, "correction": correction}
        )
    await hub.push_state()
    if flag:
        return (
            f"FLAGGED to the teacher right away ({severity}, deviation {deviation}/10). "
            "Stay in the student persona: briefly react (\"Oh, I read that it's actually...\") and "
            "ask them to re-explain that part with the corrected idea, ideally with a diagram."
        )
    if severity == "minor" or deviation >= 3:
        return "Minor gap - not flagged. Ask a probing follow-up that leads them to notice the gap themselves."
    return "Solid. Go one level deeper (mechanism, edge case, or application) or move to the next sub-concept."


@mcp.tool()
async def get_session_state() -> str:
    """Return the session so far (topic, transcript, assessments, running score) as JSON."""
    return json.dumps(hub.require().public_state(), indent=2)


@mcp.tool()
async def end_session(
    summary: str,
    strengths: list[str],
    gaps: list[str],
    next_steps: list[str],
    score: int | None = None,
) -> str:
    """Finish the session and generate the report (HTML + Markdown + JSON), shown to the teacher.

    summary: 2-4 sentence overview of how well they understand the topic.
    strengths / gaps / next_steps: short bullet points.
    score: optional 0-100 override; otherwise computed from the assessments.
    """
    s = hub.require()
    s.ended_at = time.time()
    r = build_report(s, summary, strengths, gaps, next_steps, score)
    s.report = r
    paths = save_report(r, CONFIG["reports_dir"])
    url = f"{CONFIG['ui_url']}/report/{s.id}"
    await hub.broadcast({"type": "report", "url": f"/report/{s.id}", "score": r["score"], "grade": r["grade"]})
    await hub.push_state()
    return (
        f"Report ready: {url}\nSaved: {paths['html'].resolve()}\n        {paths['md'].resolve()}\n\n"
        + to_markdown({**r, "transcript": []})
    )


@mcp.prompt()
def teach_back(topic: str, scope: str = "general") -> str:
    """Kick off a Feynman teach-back session where you play the newbie student."""
    return (
        f"Let's do a teach-back session. I want to teach you about: {topic} (scope: {scope}).\n"
        "Call start_session, then follow the student playbook below.\n\n" + STUDENT_PLAYBOOK
    )


# --------------------------------------------------------------------------- web UI

STATIC = resources.files("feynman_mcp") / "static"


async def _index(_: web.Request) -> web.Response:
    return web.Response(text=(STATIC / "index.html").read_text(encoding="utf-8"), content_type="text/html")


async def _static(req: web.Request) -> web.Response:
    name = req.match_info["name"]
    f = STATIC / name
    if "/" in name or ".." in name or not f.is_file():
        raise web.HTTPNotFound()
    ctype = {"js": "application/javascript", "css": "text/css"}.get(name.rsplit(".", 1)[-1], "text/plain")
    return web.Response(text=f.read_text(encoding="utf-8"), content_type=ctype)


async def _report(req: web.Request) -> web.Response:
    s = hub.sessions.get(req.match_info["sid"])
    if not s or not s.report:
        raise web.HTTPNotFound(text="Report not ready")
    return web.Response(text=to_html(s.report), content_type="text/html")


async def _ws(req: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=32 * 1024 * 1024)
    await ws.prepare(req)
    hub.clients.add(ws)
    if hub.session:
        await ws.send_json({"type": "session", "state": hub.session.public_state()})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            kind = data.get("type")
            if kind == "answer":
                ok = await hub.submit_answer(data.get("turn_id", ""), data.get("text", ""), data.get("images", []))
                await ws.send_json({"type": "answer_ack", "ok": ok})
            elif kind == "snapshot":
                hub.deliver_snapshot(data.get("images", []))
    finally:
        hub.clients.discard(ws)
    return ws


def build_web_app() -> web.Application:
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_get("/", _index)
    app.router.add_get("/static/{name}", _static)
    app.router.add_get("/report/{sid}", _report)
    app.router.add_get("/ws", _ws)
    return app


async def start_web(host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_web_app(), access_log=None)
    await runner.setup()
    for p in range(port, port + 20):  # several AI clients may each spawn a server
        try:
            await web.TCPSite(runner, host, p).start()
            CONFIG["ui_url"] = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{p}"
            log.info("Teacher UI on %s", CONFIG["ui_url"])
            return runner
        except OSError:
            continue
    raise RuntimeError(f"No free port in {port}-{port + 19}")


async def _amain(args: argparse.Namespace) -> None:
    runner = await start_web(args.host, args.port)
    try:
        if args.transport == "http":
            log.info("MCP streamable HTTP on http://%s:%d/mcp", args.host, args.mcp_port)
            await mcp.run_streamable_http_async(host=args.host, port=args.mcp_port)
        else:
            await mcp.run_stdio_async()
    finally:
        await runner.cleanup()


def main() -> None:
    ap = argparse.ArgumentParser(description="Feynman teach-back MCP server")
    ap.add_argument("--transport", choices=["stdio", "http"], default=os.getenv("FEYNMAN_TRANSPORT", "stdio"))
    ap.add_argument("--host", default=os.getenv("FEYNMAN_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("FEYNMAN_PORT", "8765")), help="teacher UI port")
    ap.add_argument("--mcp-port", type=int, default=int(os.getenv("FEYNMAN_MCP_PORT", "8000")), help="MCP HTTP port")
    ap.add_argument("--reports-dir", default=os.getenv("FEYNMAN_REPORTS_DIR", str(Path.home() / "feynman-reports")))
    ap.add_argument("--no-browser", action="store_true", help="don't auto-open the teacher UI")
    args = ap.parse_args()
    # stdout is the MCP channel in stdio mode: log to stderr only.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[feynman] %(message)s")
    CONFIG["reports_dir"] = Path(args.reports_dir).expanduser()
    CONFIG["open_browser"] = not args.no_browser
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
