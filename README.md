# Feynman Teach-Back MCP

Learn by teaching. This MCP server lets **any MCP-capable AI** (Claude Desktop, Claude Code,
Cursor, etc.) act as a curious beginner. **You** explain a topic out loud and draw diagrams,
either on a **mouse drawing board** or on a **real whiteboard through your camera**. The AI:

- asks logical follow-up questions like a newcomer would ("wait, why…?", "can you draw that?")
- judges every answer against the correct understanding (deviation 0–10)
- **small gaps:** keeps asking follow-ups so you find the gap yourself
- **important errors:** flags them right away (on screen and spoken aloud) with the correct idea
- **at the end:** generates a report (score, strengths, gaps, flagged errors, concept
  breakdown, next steps, full transcript including your diagrams)

```
 Your AI  ──MCP (stdio/HTTP)──►  feynman-mcp  ◄──WebSocket──  Browser UI (you)
 (student)                     (session + report)        🎤 voice · ✏️ canvas · 📷 whiteboard
```

## Install

Requires Python 3.10+.

```bash
git clone <this repo> && cd reinforcement_attaining
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Connect your AI

**Claude Desktop** (`claude_desktop_config.json`), **Cursor** (`.cursor/mcp.json`), and similar clients:

```json
{
  "mcpServers": {
    "feynman": {
      "command": "/absolute/path/to/reinforcement_attaining/.venv/bin/feynman-mcp"
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add feynman -- /absolute/path/to/.venv/bin/feynman-mcp
```

**HTTP clients / remote agents:**

```bash
feynman-mcp --transport http --mcp-port 8000   # MCP at http://127.0.0.1:8000/mcp
```

## Use it

1. Tell your AI: *"Let's do a teach-back session. I'll teach you about the TCP handshake."*
   (or use the `teach_back` prompt if your client shows MCP prompts).
2. The teacher UI opens at `http://localhost:8765`. Allow microphone and camera access.
3. The AI's question appears and is read aloud. Click 🎤 (or press **Space**) and explain.
   Draw on the board, or switch to **📷 Whiteboard camera** and point it at your whiteboard.
4. Click **Send answer** (or press Enter). The AI looks at your words *and* your images.
5. Say "I'm done" whenever you like. The AI calls `end_session` and the report opens in the UI. It is also saved to
   `~/feynman-reports/` as HTML, Markdown and JSON.

> Voice input uses the browser's Web Speech API, so use **Chrome or Edge**. Other browsers can
> still type answers, and questions are still spoken aloud.

## MCP surface

| Tool | What it does |
|---|---|
| `start_session(topic, scope, teacher_name)` | Starts a session (`scope`: `specific` or `general`) and opens the UI |
| `ask_teacher(question, wants_diagram, wait_seconds)` | Shows and speaks a question, then returns the transcript plus drawing/whiteboard images |
| `wait_for_answer(wait_seconds)` | Keeps waiting if the teacher is taking a while |
| `look_at_board(source)` | Grabs the current canvas and/or camera frame without asking anything |
| `assess_explanation(concept, deviation, severity, note, correction)` | Logs a judgment. Major/critical (or deviation ≥ 6) is **flagged immediately**; otherwise the AI is told to keep probing |
| `get_session_state()` | Returns the transcript, assessments and running score as JSON |
| `end_session(summary, strengths, gaps, next_steps, score?)` | Builds and saves the report |

The prompt `teach_back(topic, scope)` and the server `instructions` both carry the "newbie student"
playbook, so the behavior is consistent whichever AI is connected.

### Options

| Flag / env | Default | |
|---|---|---|
| `--port` / `FEYNMAN_PORT` | `8765` | Teacher UI port (tries the next free one if it is taken) |
| `--transport` / `FEYNMAN_TRANSPORT` | `stdio` | `stdio` or `http` |
| `--mcp-port` / `FEYNMAN_MCP_PORT` | `8000` | MCP port for the HTTP transport |
| `--reports-dir` / `FEYNMAN_REPORTS_DIR` | `~/feynman-reports` | Where reports are saved |
| `--no-browser` | off | Don't auto-open the UI |

## Development

```bash
pip install -e '.[dev]'
pytest   # end-to-end: real MCP client over stdio + simulated teacher over WebSocket
```
