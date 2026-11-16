"""Put one spoken question through a GPT-Live session and log what happens.

GPT-Live is full duplex and has no "turn finished" event, so this client:

- streams the question as 24 kHz mono PCM at real-time pace, then keeps sending
  silence, because the input stream must stay continuous;
- logs every server event with a local time measured from ``session.started``;
- decides for itself when the answer is over: no hand-off still running, and no
  speech for ``quiet_s`` seconds, or ``max_wait_s`` seconds without that.

Speech timing comes from the transcript events, whose ``start_ms`` and
``end_ms`` are on the server's session timeline. Output audio on this
connection carries no timestamps and can arrive faster than it would play.

Written against openai 3.51 (``client.live.connect``) and the GPT-Live guides.
It has not yet been run against the live service.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SAMPLE_RATE = 24_000
CHUNK_MS = 100
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MS // 1000  # 16-bit mono
_SILENCE = b"\x00" * CHUNK_BYTES

_BACKEND_FINISHED = {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}


@dataclass(frozen=True)
class LiveSettings:
    live_model: str
    backend_model: str
    backend_instructions: str
    instructions: str | None = None  # None: send none, so the server default applies
    voice: str = "marin"
    reasoning_effort: str | None = None
    lead_silence_s: float = 0.5
    quiet_s: float = 4.0
    max_wait_s: float = 90.0
    close_timeout_s: float = 15.0


def session_config(settings: LiveSettings) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "model": settings.backend_model,
        "instructions": settings.backend_instructions,
    }
    if settings.reasoning_effort:
        responses["reasoning"] = {"effort": settings.reasoning_effort}
    config: dict[str, Any] = {
        "model": settings.live_model,
        "audio": {
            "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
            "output": {"voice": settings.voice},
        },
        "delegation": {"type": "responses", "responses": responses},
    }
    if settings.instructions is not None:
        config["instructions"] = settings.instructions
    return config


def read_pcm(wav_file: Path) -> bytes:
    """Read a 24 kHz mono 16-bit WAV and return its raw samples."""
    with wave.open(str(wav_file)) as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError(f"{wav_file} is not 24 kHz mono 16-bit PCM")
        return wav.readframes(wav.getnframes())


@dataclass
class _Run:
    started_at: float | None = None
    question_start_ms: float | None = None
    question_end_ms: float | None = None
    closing: bool = False
    stop_reason: str | None = None
    close_reason: str | None = None
    usage_seconds: float | None = None
    last_audio_ms: float = 0.0
    input_segments: list[list[Any]] = field(default_factory=list)   # [start_ms, end_ms, text]
    output_segments: list[list[Any]] = field(default_factory=list)  # [start_ms, end_ms, text]
    delegations: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    audio_out: bytearray = field(default_factory=bytearray)

    def now_ms(self) -> float:
        assert self.started_at is not None
        return (time.monotonic() - self.started_at) * 1000.0

    def last_activity_ms(self) -> float:
        marks = [self.question_end_ms or 0.0, self.last_audio_ms]
        marks += [seg[1] for seg in self.output_segments]
        marks += [d["done_ms"] for d in self.delegations.values() if d["done_ms"] is not None]
        return max(marks)

    def pending_delegations(self) -> bool:
        return any(d["done_ms"] is None for d in self.delegations.values())


def _loggable(event: Any) -> dict[str, Any]:
    payload = event.model_dump(mode="json", exclude_none=True)
    if payload.get("type") == "session.output_audio.delta":
        payload["delta"] = f"<{len(payload.get('delta', ''))} base64 chars>"
    return payload


def _record(run: _Run, event: Any) -> None:
    kind = event.type
    if kind == "session.output_audio.delta":
        run.audio_out.extend(base64.b64decode(event.delta))
        run.last_audio_ms = run.now_ms()
    elif kind == "session.output_transcript.delta":
        run.output_segments.append([event.start_ms, event.end_ms, event.delta])
    elif kind == "session.input_transcript.delta":
        run.input_segments.append([event.start_ms, event.end_ms, event.delta])
    elif kind == "session.delegation.created":
        d = event.delegation
        run.delegations[d.id] = {
            "id": d.id,
            "target": d.target,
            "response_id": d.response_id,
            "offset_ms": event.offset_ms,
            "created_ms": run.now_ms(),
            "done_ms": None,
            "status": None,
            "text": "",
            "usage": None,
        }
    elif kind == "response.event":
        nested = event.event or {}
        delegation = run.delegations.get(event.delegation_id or "")
        if delegation is None:
            return
        nested_type = nested.get("type")
        if nested_type == "response.output_text.delta":
            delegation["text"] += str(nested.get("delta", ""))
        elif nested_type in _BACKEND_FINISHED:
            delegation["done_ms"] = run.now_ms()
            delegation["status"] = nested_type
            response = nested.get("response")
            if isinstance(response, dict):
                delegation["usage"] = response.get("usage")
    elif kind == "error":
        run.errors.append(event.error.model_dump(mode="json", exclude_none=True))
    elif kind == "session.closed":
        run.close_reason = event.reason
        run.usage_seconds = event.usage.seconds


async def _send_audio(connection: Any, run: _Run, pcm: bytes, settings: LiveSettings) -> None:
    lead_chunks = round(settings.lead_silence_s * 1000 / CHUNK_MS)
    chunks = [_SILENCE] * lead_chunks
    for i in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[i : i + CHUNK_BYTES]
        chunks.append(chunk + b"\x00" * (CHUNK_BYTES - len(chunk)))

    began = time.monotonic()
    sent = 0

    async def send(chunk: bytes) -> None:
        nonlocal sent
        await connection.session.input_audio.append(audio=base64.b64encode(chunk).decode("ascii"))
        sent += 1
        await asyncio.sleep(max(0.0, began + sent * CHUNK_MS / 1000 - time.monotonic()))

    for index, chunk in enumerate(chunks):
        if run.closing:
            return
        if index == lead_chunks:
            run.question_start_ms = run.now_ms()
        await send(chunk)
    run.question_end_ms = run.now_ms()
    # The input stream must stay continuous, so send silence until the run ends.
    while not run.closing:
        await send(_SILENCE)


async def _watch(connection: Any, run: _Run, settings: LiveSettings, closed: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(0.25)
        if run.question_end_ms is None:
            continue
        now = run.now_ms()
        if now - run.question_end_ms > settings.max_wait_s * 1000:
            run.stop_reason = "timeout"
            break
        answered = bool(run.output_segments)
        if answered and not run.pending_delegations() and now - run.last_activity_ms() >= settings.quiet_s * 1000:
            run.stop_reason = "quiet"
            break
    run.closing = True
    await connection.session.close()
    try:
        await asyncio.wait_for(closed.wait(), timeout=settings.close_timeout_s)
    except asyncio.TimeoutError:
        run.stop_reason = f"{run.stop_reason}; no session.closed event"
        await connection.close()


async def run_question(client: Any, pcm: bytes, settings: LiveSettings, out_dir: Path) -> dict[str, Any]:
    """Run one question and write ``events.jsonl``, ``answer.wav`` and ``summary.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    run = _Run()
    closed = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    with (out_dir / "events.jsonl").open("w", encoding="utf-8") as log:
        async with client.live.connect() as connection:
            await connection.session.start(session=session_config(settings), event_id="start")
            try:
                async for event in connection:
                    if event.type == "session.started":
                        run.started_at = time.monotonic()
                        tasks.append(asyncio.create_task(_send_audio(connection, run, pcm, settings)))
                        tasks.append(asyncio.create_task(_watch(connection, run, settings, closed)))
                    if run.started_at is None:
                        # Nothing but an error can arrive before the session starts.
                        log.write(json.dumps({"t_ms": None, "event": _loggable(event)}) + "\n")
                        if event.type == "error":
                            raise RuntimeError(f"session did not start: {event.error.message}")
                        continue
                    _record(run, event)
                    log.write(json.dumps({"t_ms": round(run.now_ms(), 1), "event": _loggable(event)}) + "\n")
                    if event.type == "session.closed":
                        closed.set()
                        break
            finally:
                run.closing = True
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    with wave.open(str(out_dir / "answer.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(run.audio_out))

    summary = {
        "question_start_ms": run.question_start_ms,
        "question_end_ms": run.question_end_ms,
        "input_segments": run.input_segments,
        "output_segments": run.output_segments,
        "delegations": list(run.delegations.values()),
        "usage_seconds": run.usage_seconds,
        "stop_reason": run.stop_reason,
        "close_reason": run.close_reason,
        "errors": run.errors,
        "answer_audio_seconds": round(len(run.audio_out) / (SAMPLE_RATE * 2), 2),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary
