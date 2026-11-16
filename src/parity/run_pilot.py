"""Run the pilot questions through GPT-Live under one condition.

    python -m parity.run_pilot --condition shipped --dry-run
    python -m parity.run_pilot --condition shipped --limit 3     # smoke test
    python -m parity.run_pilot --condition shipped

Each question gets its own session and its own folder with the event log, the
answer audio and a summary. Finished questions are skipped on a re-run, so a
run can be stopped and resumed. Sessions cost real money: ``--dry-run`` prints
the plan and spends nothing, and ``--max-minutes`` stops new sessions from
starting once that much session time has been used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import wave
from pathlib import Path

from . import bba, config
from .live_client import LiveSettings, read_pcm, run_question
from .timeline import analyse

# Rough allowance for the reply and the wait after each question, for estimates only.
_REPLY_ALLOWANCE_S = 20.0


def _clip_seconds(path: Path) -> float:
    with wave.open(str(path)) as wav:
        return wav.getnframes() / wav.getframerate()


def _settings(condition: str, backend: str, effort: str | None, max_wait_s: float) -> LiveSettings:
    return LiveSettings(
        live_model=config.LIVE_MODEL,
        backend_model=backend,
        backend_instructions=config.BACKEND_INSTRUCTIONS,
        instructions=config.CONDITIONS[condition],
        voice=config.VOICE,
        reasoning_effort=effort,
        max_wait_s=max_wait_s,
    )


async def _run(args: argparse.Namespace, questions: list[bba.Question], out_dir: Path) -> None:
    from openai import AsyncOpenAI

    settings = _settings(args.condition, args.backend, args.effort, args.max_wait)
    gate = asyncio.Semaphore(args.concurrency)
    used_seconds = 0.0
    budget_seconds = args.max_minutes * 60.0
    results = (out_dir / "results.jsonl").open("a", encoding="utf-8")
    skipped_for_budget = 0

    async with AsyncOpenAI() as client:

        async def one(question: bba.Question) -> None:
            nonlocal used_seconds, skipped_for_budget
            run_dir = out_dir / f"question_{question.id}"
            if (run_dir / "summary.json").exists():
                return
            async with gate:
                if used_seconds >= budget_seconds:
                    skipped_for_budget += 1
                    return
                pcm = read_pcm(bba.wav_path(question, args.data_dir))
                # A hard stop in case the session never reports that it closed.
                limit = len(pcm) / 48_000 + args.max_wait + 60
                try:
                    summary = await asyncio.wait_for(run_question(client, pcm, settings, run_dir), timeout=limit)
                except Exception as exc:  # keep the batch going; the failure is recorded
                    results.write(json.dumps({"id": question.id, "error": f"{type(exc).__name__}: {exc}"}) + "\n")
                    results.flush()
                    print(f"question {question.id}: failed: {type(exc).__name__}: {exc}")
                    return
            used_seconds += summary.get("usage_seconds") or 0.0
            scored = analyse(question.category, question.answer, question.text, summary)
            row = {"id": question.id, "category": question.category, "official": question.answer,
                   "condition": args.condition, "backend": args.backend, **scored}
            results.write(json.dumps(row, ensure_ascii=False) + "\n")
            results.flush()
            mark = {True: "right", False: "wrong", None: "unread"}[scored["correct"]]
            print(f"question {question.id}: {mark}; handed off: {scored['delegated']}; "
                  f"time to answer: {scored['time_to_answer_s']} s; cause: {scored['cause']}")

        try:
            await asyncio.gather(*(one(q) for q in questions))
        finally:
            results.close()

    print(f"session time used: {used_seconds / 60:.1f} min "
          f"(about ${used_seconds / 60 * config.LIVE_USD_PER_MINUTE:.2f} before backend tokens)")
    if skipped_for_budget:
        print(f"stopped early: {skipped_for_budget} questions not run because --max-minutes was reached")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pilot questions through GPT-Live under one condition.")
    parser.add_argument("--condition", choices=sorted(config.CONDITIONS), required=True)
    parser.add_argument("--questions", type=Path, default=Path("data/pilot/questions.jsonl"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("runs/pilot"))
    parser.add_argument("--backend", default=config.BACKEND_MODEL)
    parser.add_argument("--effort", default=None, help="backend reasoning effort; omit for the model default")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-wait", type=float, default=90.0, help="seconds to wait for an answer after the question")
    parser.add_argument("--max-minutes", type=float, default=150.0, help="stop starting sessions after this much session time")
    parser.add_argument("--limit", type=int, default=None, help="only the first N questions")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    questions = bba.read_jsonl(args.questions)[: args.limit]
    out_dir = args.out / args.condition / args.backend
    pending = [q for q in questions if not (out_dir / f"question_{q.id}" / "summary.json").exists()]

    if args.dry_run:
        audio = sum(_clip_seconds(bba.wav_path(q, args.data_dir)) for q in pending)
        minutes = (audio + _REPLY_ALLOWANCE_S * len(pending)) / 60
        print(f"condition: {args.condition}; live model: {config.LIVE_MODEL}; backend: {args.backend}")
        print(f"would run {len(pending)} of {len(questions)} questions ({len(questions) - len(pending)} already done)")
        print(f"question audio: {audio / 60:.1f} min; estimated session time: {minutes:.0f} min")
        print(f"estimated voice cost: ${minutes * config.LIVE_USD_PER_MINUTE:.2f} at list price, before backend tokens")
        print(f"session-time cap (--max-minutes): {args.max_minutes:.0f}; results -> {out_dir}")
        return

    config.load_env()
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run(args, pending, out_dir))


if __name__ == "__main__":
    main()
