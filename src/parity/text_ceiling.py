"""Text ceiling: the thinking model answers the same questions as text.

This is the score the voice system is measured against. It uses the same
backend prompt and reasoning effort that GPT-Live's delegation uses, so the
only difference from the voice runs is the voice path itself.

    python -m parity.text_ceiling --dry-run
    python -m parity.text_ceiling --model gpt-6-sol

Each call costs real money; ``--dry-run`` prints the plan and spends nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from . import bba, config
from .answers import extract, is_correct


async def _ask(client, question: bba.Question, model: str, effort: str | None) -> dict:
    kwargs = {
        "model": model,
        "instructions": config.BACKEND_INSTRUCTIONS,
        "input": question.text,
        "store": False,
    }
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    response = await client.responses.create(**kwargs)
    reply = response.output_text
    found = extract(question.category, reply)
    usage = response.usage
    return {
        "id": question.id,
        "category": question.category,
        "official": question.answer,
        "reply": reply,
        "answer": found.answer,
        "answer_method": found.method,
        "correct": is_correct(question.category, found.answer, question.answer),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }


async def _run(questions: list[bba.Question], model: str, effort: str | None, out: Path, concurrency: int) -> list[dict]:
    from openai import AsyncOpenAI

    done = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            done[row["id"]] = row
    todo = [q for q in questions if q.id not in done]
    gate = asyncio.Semaphore(concurrency)
    out.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncOpenAI() as client:
        with out.open("a", encoding="utf-8") as fh:

            async def one(question: bba.Question) -> None:
                async with gate:
                    row = await _ask(client, question, model, effort)
                done[row["id"]] = row
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()

            await asyncio.gather(*(one(q) for q in todo))
    return [done[q.id] for q in questions]


def report(rows: list[dict]) -> str:
    lines = []
    by_category: dict[str, Counter] = {}
    for row in rows:
        tally = by_category.setdefault(row["category"], Counter())
        tally["right" if row["correct"] else "unread" if row["correct"] is None else "wrong"] += 1
    total = Counter()
    for category, tally in by_category.items():
        total.update(tally)
        scored = tally["right"] + tally["wrong"]
        share = f"{100 * tally['right'] / scored:.1f}%" if scored else "n/a"
        lines.append(f"{category:18s} right {tally['right']:3d}  wrong {tally['wrong']:3d}  unread {tally['unread']:3d}  -> {share}")
    scored = total["right"] + total["wrong"]
    share = f"{100 * total['right'] / scored:.1f}%" if scored else "n/a"
    lines.append(f"{'all':18s} right {total['right']:3d}  wrong {total['wrong']:3d}  unread {total['unread']:3d}  -> {share}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the pilot questions as text on the thinking model.")
    parser.add_argument("--questions", type=Path, default=Path("data/pilot/questions.jsonl"))
    parser.add_argument("--model", default=config.BACKEND_MODEL)
    parser.add_argument("--effort", default=None, help="reasoning effort; omit to use the model default")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="only the first N questions")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    questions = bba.read_jsonl(args.questions)[: args.limit]
    out = args.out or Path("runs/pilot/text_ceiling") / f"{args.model}.jsonl"
    if args.dry_run:
        words = sum(len(q.text.split()) for q in questions)
        print(f"would send {len(questions)} questions (about {words} words in all) to {args.model}")
        print(f"effort: {args.effort or 'model default'}; results -> {out}")
        return

    config.load_env()
    rows = asyncio.run(_run(questions, args.model, args.effort, out, args.concurrency))
    print(report(rows))
    tokens_in = sum(r["input_tokens"] or 0 for r in rows)
    tokens_out = sum(r["output_tokens"] or 0 for r in rows)
    print(f"tokens: {tokens_in} in, {tokens_out} out; results -> {out}")


if __name__ == "__main__":
    main()
