# Voice-text parity benchmark

How long does a user wait for a right answer from a voice AI system, and how
much of the system's own thinking model survives into speech? The plan and
reasoning are in [proposal-voice-text-parity.md](proposal-voice-text-parity.md).

This repository currently holds the week-1 pilot: 180 spoken reasoning
questions put through OpenAI's GPT-Live under three conditions, plus the same
questions as text on its backend model.

## Status

| Step | State |
| --- | --- |
| Project scaffold, Python environment | Done |
| 180 Big Bench Audio questions sampled (45 per category), audio converted | Done |
| Answer scorer and timeline scorer, with tests | Done |
| Text-ceiling runner | Written, not yet run |
| GPT-Live client and pilot runner | Written against the SDK and docs, not yet run against the service |
| Paid runs: text ceiling, then the three GPT-Live conditions | Not started |

## Setup

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -e ".[dev]"
    cp .env.example .env        # then put a working OPENAI_API_KEY in .env

The key needs a paid tier with access to `gpt-live-1` and `gpt-6-sol`. A key in
`.env` takes precedence over one in the shell environment.

## Running the pilot

Free steps:

    .venv/bin/python -m pytest -q
    .venv/bin/python -m parity.bba                      # sample questions, fetch and convert audio
    .venv/bin/python -m parity.run_pilot --condition shipped --dry-run
    .venv/bin/python -m parity.text_ceiling --dry-run

Paid steps, in this order. Start with the three-question smoke test: the
GPT-Live client has not yet talked to the real service.

    .venv/bin/python -m parity.text_ceiling                                # about $1 to $2
    .venv/bin/python -m parity.run_pilot --condition shipped --limit 3     # smoke test, a few cents
    .venv/bin/python -m parity.run_pilot --condition shipped               # about $6 plus backend tokens
    .venv/bin/python -m parity.run_pilot --condition fast
    .venv/bin/python -m parity.run_pilot --condition thinking

Each condition is about two hours of session time, or roughly 15 minutes of
wall-clock at the default concurrency of 8. `--max-minutes` caps session time
per invocation (default 150). A run can be stopped and resumed: finished
questions are skipped.

## The three conditions

| Condition | Live instructions | What it shows |
| --- | --- | --- |
| `shipped` | None, so OpenAI's default prompt applies | What the system does as shipped |
| `fast` | Never hand off | The voice model alone |
| `thinking` | Always hand off | The most the voice path can deliver |

The backend prompt is the same in all three and in the text-ceiling run.

## Layout

    src/parity/bba.py           dataset, text twins, sampling, audio conversion
    src/parity/answers.py       pull the final answer out of a reply and score it
    src/parity/timeline.py      time to answer, hand-offs, and the cause of a failure
    src/parity/live_client.py   one question through one GPT-Live session, fully logged
    src/parity/run_pilot.py     a whole condition, with resume and a spend cap
    src/parity/text_ceiling.py  the same questions as text on the thinking model
    src/parity/config.py        models, prompts, conditions
    tests/                      scorer tests (no network)
    data/                       downloaded questions and audio (not in git)
    runs/                       run outputs (not in git)

## What the scorers can and cannot read

- `answers.py` is rule-based and cautious. When a reply does not state its
  answer plainly (for example "Elanor tells the truth" with no yes or no), it
  returns nothing and the run is marked `unread` for a person to label.
- `timeline.py` assigns four causes on reasoning questions: misheard, missed
  hand-off, answered early and garbled result. A wrong backend result is
  reported as `thinker_wrong`, which is not a coordination loss.
- Three of the four question categories are two-way choices, so a system that
  guesses scores 50% on them. Parity has to be read against that floor.

## Data

Big Bench Audio is MIT-licensed (Artificial Analysis). Its questions come from
Big Bench Hard; the ids line up one-to-one, which is how each spoken question
gets its text twin.
