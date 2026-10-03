"""Teaching-session state shared between the MCP tools and the browser UI."""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["none", "minor", "major", "critical"]
SEVERITY_ORDER: dict[str, int] = {"none": 0, "minor": 1, "major": 2, "critical": 3}

# A deviation at or above this score (0-10), or a major/critical severity,
# gets flagged to the learner immediately instead of probing further.
FLAG_DEVIATION_THRESHOLD = 6


@dataclass
class Snapshot:
    """An image the learner shared: a mouse drawing or a whiteboard camera frame."""

    source: Literal["canvas", "camera"]
    mime: str
    data: bytes
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_data_url(cls, source: str, data_url: str) -> "Snapshot":
        header, _, b64 = data_url.partition(",")
        mime = header.removeprefix("data:").split(";")[0] or "image/png"
        return cls(source="camera" if source == "camera" else "canvas", mime=mime, data=base64.b64decode(b64))

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


@dataclass
class Turn:
    """One exchange: the AI-student's question and the human-teacher's answer."""

    id: str
    question: str
    asked_at: float
    wants_diagram: bool = False
    answer: str | None = None
    answered_at: float | None = None
    snapshots: list[Snapshot] = field(default_factory=list)


@dataclass
class Assessment:
    concept: str
    deviation: int  # 0 = spot on, 10 = completely wrong
    severity: Severity
    note: str
    correction: str | None
    flagged: bool
    turn_id: str | None
    ts: float = field(default_factory=time.time)


@dataclass
class Session:
    topic: str
    scope: Literal["specific", "general"] = "general"
    learner_name: str = "Teacher"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    turns: list[Turn] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)
    report: dict[str, Any] | None = None

    @property
    def flags(self) -> list[Assessment]:
        return [a for a in self.assessments if a.flagged]

    def current_turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    def score(self) -> int:
        """Understanding score 0-100: mean of (10 - deviation), with flagged errors weighted double."""
        if not self.assessments:
            return 0
        weights = [2 if a.flagged else 1 for a in self.assessments]
        total = sum((10 - a.deviation) * w for a, w in zip(self.assessments, weights))
        return round(total / (10 * sum(weights)) * 100)

    def public_state(self) -> dict[str, Any]:
        """JSON-safe state for the browser UI (no image bytes)."""
        turn = self.current_turn()
        return {
            "id": self.id,
            "topic": self.topic,
            "scope": self.scope,
            "learner_name": self.learner_name,
            "ended": self.ended_at is not None,
            "pending_question": (
                {"id": turn.id, "question": turn.question, "wants_diagram": turn.wants_diagram}
                if turn and turn.answer is None
                else None
            ),
            "turns": [
                {"id": t.id, "question": t.question, "answer": t.answer, "images": len(t.snapshots)}
                for t in self.turns
            ],
            "assessments": [
                {
                    "concept": a.concept,
                    "deviation": a.deviation,
                    "severity": a.severity,
                    "note": a.note,
                    "correction": a.correction,
                    "flagged": a.flagged,
                }
                for a in self.assessments
            ],
            "score": self.score(),
            "report_url": f"/report/{self.id}" if self.report else None,
        }


class Hub:
    """Holds the active session and bridges MCP tool calls with connected browser tabs."""

    def __init__(self) -> None:
        self.session: Session | None = None
        self.sessions: dict[str, Session] = {}
        self.clients: set[Any] = set()  # aiohttp WebSocketResponse objects
        self._answer_waiters: dict[str, asyncio.Future[Turn]] = {}
        self._snapshot_waiter: asyncio.Future[list[Snapshot]] | None = None

    # --- browser fan-out -------------------------------------------------
    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def push_state(self) -> None:
        if self.session:
            await self.broadcast({"type": "state", "state": self.session.public_state()})

    # --- session lifecycle ----------------------------------------------
    def start(self, topic: str, scope: str, learner_name: str) -> Session:
        s = Session(topic=topic, scope="specific" if scope == "specific" else "general", learner_name=learner_name)
        self.session = s
        self.sessions[s.id] = s
        return s

    def require(self) -> Session:
        if not self.session:
            raise RuntimeError("No active session. Call start_session first.")
        return self.session

    # --- questions / answers --------------------------------------------
    async def ask(self, question: str, wants_diagram: bool) -> Turn:
        s = self.require()
        prev = s.current_turn()
        if prev and prev.answer is None:
            # Replace an unanswered question rather than stacking them.
            prev.answer = "(skipped - AI asked a new question)"
            prev.answered_at = time.time()
        turn = Turn(id=uuid.uuid4().hex[:8], question=question, asked_at=time.time(), wants_diagram=wants_diagram)
        s.turns.append(turn)
        self._answer_waiters[turn.id] = asyncio.get_running_loop().create_future()
        await self.broadcast({"type": "question", "turn_id": turn.id, "question": question, "wants_diagram": wants_diagram})
        await self.push_state()
        return turn

    async def wait_answer(self, turn: Turn, timeout: float) -> Turn | None:
        if turn.answer is not None:
            return turn
        fut = self._answer_waiters.get(turn.id)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None

    async def submit_answer(self, turn_id: str, text: str, images: list[dict[str, str]]) -> bool:
        s = self.session
        if not s:
            return False
        turn = next((t for t in s.turns if t.id == turn_id), None)
        if not turn or turn.answer is not None:
            return False
        turn.answer = text.strip() or "(no spoken/typed answer)"
        turn.answered_at = time.time()
        turn.snapshots = [Snapshot.from_data_url(i.get("source", "canvas"), i["dataUrl"]) for i in images if i.get("dataUrl")]
        fut = self._answer_waiters.pop(turn_id, None)
        if fut and not fut.done():
            fut.set_result(turn)
        await self.push_state()
        return True

    # --- ad-hoc snapshots -----------------------------------------------
    async def request_snapshot(self, source: str, timeout: float = 15) -> list[Snapshot]:
        if not self.clients:
            return []
        self._snapshot_waiter = asyncio.get_running_loop().create_future()
        await self.broadcast({"type": "snapshot_request", "source": source})
        try:
            return await asyncio.wait_for(self._snapshot_waiter, timeout)
        except asyncio.TimeoutError:
            return []
        finally:
            self._snapshot_waiter = None

    def deliver_snapshot(self, images: list[dict[str, str]]) -> None:
        if self._snapshot_waiter and not self._snapshot_waiter.done():
            self._snapshot_waiter.set_result(
                [Snapshot.from_data_url(i.get("source", "canvas"), i["dataUrl"]) for i in images if i.get("dataUrl")]
            )
