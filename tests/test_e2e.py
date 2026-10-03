"""End-to-end: a real MCP client over stdio plays the AI; a WebSocket client plays the teacher UI."""

import asyncio
import base64
import os
import socket
import sys

import aiohttp
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# 1x1 white PNG
PNG = "data:image/png;base64," + base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c63f8ffff3f0005fe02fea7d6a4f00000000049454e44ae426082"
    )
).decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def _teacher(port: int, answers: list[tuple[str, list]], seen: dict) -> None:
    async with aiohttp.ClientSession() as http:
        for _ in range(50):
            try:
                ws = await http.ws_connect(f"http://127.0.0.1:{port}/ws")
                break
            except aiohttp.ClientError:
                await asyncio.sleep(0.1)
        it = iter(answers)
        async for msg in ws:
            m = msg.json()
            seen.setdefault(m["type"], []).append(m)
            if m["type"] == "question":
                text, images = next(it)
                await ws.send_json({"type": "answer", "turn_id": m["turn_id"], "text": text, "images": images})
            elif m["type"] == "snapshot_request":
                await ws.send_json({"type": "snapshot", "images": [{"source": "canvas", "dataUrl": PNG}]})
            elif m["type"] == "report":
                await ws.close()


async def test_full_session(tmp_path):
    port = _free_port()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "feynman_mcp", "--port", str(port), "--no-browser", "--reports-dir", str(tmp_path)],
        env={**os.environ},
    )
    seen: dict = {}
    teacher = asyncio.create_task(
        _teacher(
            port,
            [
                ("TCP uses a three-way handshake: SYN, SYN-ACK, ACK.", []),
                ("The server sends ACK first, then the client sends SYN.", [{"source": "canvas", "dataUrl": PNG}]),
            ],
            seen,
        )
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as mcp:
        await mcp.initialize()
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"start_session", "ask_teacher", "assess_explanation", "end_session", "look_at_board"} <= names
        assert "teach_back" in {p.name for p in (await mcp.list_prompts()).prompts}

        out = await mcp.call_tool("start_session", {"topic": "TCP handshake", "scope": "specific"})
        assert "TCP handshake" in _text(out)
        await asyncio.sleep(0.3)  # let the teacher socket connect

        a1 = await mcp.call_tool("ask_teacher", {"question": "How does a TCP connection start?", "wait_seconds": 10})
        assert "three-way handshake" in _text(a1)
        ok = await mcp.call_tool(
            "assess_explanation", {"concept": "handshake steps", "deviation": 1, "severity": "none", "note": "Correct."}
        )
        assert "Solid" in _text(ok)

        a2 = await mcp.call_tool(
            "ask_teacher", {"question": "Who sends the first packet? Can you draw it?", "wants_diagram": True, "wait_seconds": 10}
        )
        assert any(c.type == "image" for c in a2.content)
        bad = await mcp.call_tool(
            "assess_explanation",
            {
                "concept": "who initiates",
                "deviation": 8,
                "severity": "major",
                "note": "Reversed the order.",
                "correction": "The client sends SYN first.",
            },
        )
        assert "FLAGGED" in _text(bad)

        board = await mcp.call_tool("look_at_board", {"source": "canvas"})
        assert any(c.type == "image" for c in board.content)

        rep = await mcp.call_tool(
            "end_session",
            {"summary": "Knows the steps, mixed up the initiator.", "strengths": ["names all 3 steps"],
             "gaps": ["initiator"], "next_steps": ["review client/server roles"]},
        )
        assert "Report ready" in _text(rep)

    await asyncio.wait_for(teacher, 5)
    assert seen["flag"][0]["correction"] == "The client sends SYN first."
    assert len(list(tmp_path.glob("*.html"))) == 1
    md = next(tmp_path.glob("*.md")).read_text()
    assert "who initiates" in md and "Score:" in md
