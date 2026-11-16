"""Turn one run's log into scores: the answer, when it came, and why it failed.

Input is the ``summary.json`` written by ``live_client.run_question``. Transcript
segments are ``[start_ms, end_ms, text]`` on the session timeline.

Two timing rules matter:

- The user's turn ends at the later of the last input-transcript fragment and
  the moment the harness finished playing the question.
- The answer starts at the first sentence from which the reply's answer stops
  changing. A reply that says "let me check" and then "it's eight" is timed
  from "it's eight"; filler does not count.

Cause labels cover what a reasoning question can show. Stale results and
said-not-done need the disruption and backend-task scripts and are not
assigned here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .answers import extract, is_correct

MISSED_HANDOFF = "missed_handoff"      # cause 2
ANSWERED_EARLY = "answered_early"      # cause 4
GARBLED_RESULT = "garbled_result"      # cause 5
MISHEARD = "misheard"                  # cause 1
THINKER_WRONG = "thinker_wrong"        # not a coordination loss
UNSCORED = "unscored"                  # the answer could not be read; needs a person

# Share of the script's content words that must appear in the system's transcript.
HEARD_THRESHOLD = 0.85

_SENTENCE_END = re.compile(r"[.!?]+(?:\s+|$)")
_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    "a an the of to in on at is are was were be do does did you i it this that these those and or if then "
    "with for as by from have has had my your me we they he she them his her its not no yes so".split()
)


@dataclass(frozen=True)
class Sentence:
    start_ms: float
    text: str


def join_text(segments: list[list[Any]]) -> str:
    return "".join(str(seg[2]) for seg in segments).strip()


def split_sentences(segments: list[list[Any]]) -> list[Sentence]:
    """Group transcript fragments into sentences, each stamped with its start time."""
    text = ""
    starts: list[tuple[int, float]] = []  # (character offset, start_ms of the fragment at that offset)
    for start_ms, _end_ms, fragment in segments:
        starts.append((len(text), float(start_ms)))
        text += str(fragment)

    def start_at(offset: int) -> float:
        stamp = starts[0][1]
        for position, ms in starts:
            if position > offset:
                break
            stamp = ms
        return stamp

    sentences: list[Sentence] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        chunk = text[cursor : match.end()]
        if chunk.strip():
            lead = len(chunk) - len(chunk.lstrip())
            sentences.append(Sentence(start_at(cursor + lead), chunk.strip()))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        lead = len(tail) - len(tail.lstrip())
        sentences.append(Sentence(start_at(cursor + lead), tail.strip()))
    return sentences


def answer_start_ms(category: str, segments: list[list[Any]]) -> float | None:
    """Start time of the sentence from which the reply's answer no longer changes."""
    sentences = split_sentences(segments)
    if not sentences:
        return None
    running = [extract(category, " ".join(s.text for s in sentences[: i + 1])).answer for i in range(len(sentences))]
    final = running[-1]
    if final is None:
        return None
    first = len(running) - 1
    while first > 0 and running[first - 1] == final:
        first -= 1
    return sentences[first].start_ms


def heard_share(script: str, transcript: str) -> float:
    """Share of the script's content words found in the system's transcript of the user."""
    wanted = [w for w in _WORD.findall(script.lower()) if w not in _STOPWORDS]
    if not wanted:
        return 1.0
    heard = set(_WORD.findall(transcript.lower()))
    return sum(1 for w in wanted if w in heard) / len(wanted)


def _finished_delegation(summary: dict[str, Any]) -> dict[str, Any] | None:
    done = [d for d in summary.get("delegations", []) if d.get("done_ms") is not None]
    return max(done, key=lambda d: d["done_ms"]) if done else None


def analyse(category: str, official: str, script: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Score one voice run and, if it failed, say why."""
    output_segments = summary.get("output_segments", [])
    input_segments = summary.get("input_segments", [])
    reply = join_text(output_segments)
    found = extract(category, reply)
    correct = is_correct(category, found.answer, official)

    user_end_candidates = [summary.get("question_end_ms") or 0.0]
    user_end_candidates += [float(seg[1]) for seg in input_segments]
    user_end_ms = max(user_end_candidates)
    started = answer_start_ms(category, output_segments)

    delegations = summary.get("delegations", [])
    delegated = bool(delegations)
    backend = _finished_delegation(summary)
    backend_found = extract(category, backend["text"]) if backend else None
    backend_correct = is_correct(category, backend_found.answer, official) if backend_found else None

    heard = heard_share(script, join_text(input_segments)) if input_segments else None

    cause: str | None = None
    if correct is None:
        cause = UNSCORED
    elif correct is False:
        if heard is not None and heard < HEARD_THRESHOLD:
            cause = MISHEARD
        elif not delegated:
            cause = MISSED_HANDOFF
        elif backend_correct is False:
            cause = THINKER_WRONG
        elif backend_correct is None:
            cause = UNSCORED
        elif started is not None and backend is not None and started < backend["done_ms"]:
            cause = ANSWERED_EARLY
        else:
            cause = GARBLED_RESULT

    return {
        "reply": reply,
        "answer": found.answer,
        "answer_method": found.method,
        "correct": correct,
        "user_end_ms": user_end_ms,
        "answer_start_ms": started,
        "time_to_answer_s": None if started is None else round((started - user_end_ms) / 1000.0, 2),
        "delegated": delegated,
        "delegation_count": len(delegations),
        "first_handoff_ms": min((d["offset_ms"] for d in delegations), default=None),
        "backend_reply": backend["text"] if backend else None,
        "backend_answer": backend_found.answer if backend_found else None,
        "backend_correct": backend_correct,
        "backend_done_ms": backend["done_ms"] if backend else None,
        "heard_share": None if heard is None else round(heard, 3),
        "cause": cause,
        "stop_reason": summary.get("stop_reason"),
        "usage_seconds": summary.get("usage_seconds"),
    }
