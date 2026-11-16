"""Shared settings for the pilot: models, prompts and the three conditions.

The pilot asks one question: does GPT-Live trail its own backend model, and if
so, is the loss a coordination loss? Each spoken question therefore runs under
three conditions that differ only in the live (frontend) instructions:

- ``shipped``:  no instructions, so the server's default prompt applies
- ``fast``:     never hand off; the voice model answers alone
- ``thinking``: always hand off; the voice model waits for the backend

The backend prompt is identical in every condition and in the text-ceiling run,
so any difference between voice and text comes from the voice path.
"""

from __future__ import annotations

import os
from pathlib import Path

LIVE_MODEL = "gpt-live-1"
BACKEND_MODEL = "gpt-6-sol"
VOICE = "marin"

# One prompt for the thinking model, used by the text ceiling and by delegation.
BACKEND_INSTRUCTIONS = (
    "Answer the question. Work through it carefully, then give the final answer in one short sentence."
)

# The live prompts follow the structure in OpenAI's prompting guide, which says to
# name the token rather than write "delegate to the backend".
_ROLE = "Role: You are a voice assistant. A user will ask you a question out loud. Answer it."

FAST_INSTRUCTIONS = f"""{_ROLE}

Delegation policy:
Do not generate <|SpawnThinking|> for any request. Answer every question yourself, straight away, from your own reasoning."""

THINKING_INSTRUCTIONS = f"""{_ROLE}

Delegation policy:
Backend tools: a reasoning model that works through questions carefully.
Generate <|SpawnThinking|> when: the user asks any question. Do this for every question, however simple it looks.
Do not generate <|SpawnThinking|> when: the user is only greeting you.
Before any answer, request backend work and wait for its result. Do not guess the result while waiting. Then state the result."""

# None means "send no instructions", which selects the server default prompt.
CONDITIONS: dict[str, str | None] = {
    "shipped": None,
    "fast": FAST_INSTRUCTIONS,
    "thinking": THINKING_INSTRUCTIONS,
}

# List price for a Live voice session, used only for the spend guard and estimates.
LIVE_USD_PER_MINUTE = 0.05


def load_env(root: Path | None = None) -> None:
    """Load KEY=VALUE lines from a ``.env`` file in the project root, if present.

    A key in ``.env`` replaces one already in the environment, so a project key
    wins over a stale shell-wide one.
    """
    root = root or Path(__file__).resolve().parents[2]
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")
