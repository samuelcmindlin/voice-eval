"""Pull the final answer out of a free-form reply and score it.

Replies come from text models and from transcripts of spoken answers, so they
are sentences, not labels. Extraction is rule-based and deliberately cautious:
when the rules cannot tell, it returns ``None`` and the run goes to a person or
a text judge. A wrong guess here would be scored as a model error, so "unknown"
is the safer failure.

Each category has one answer type:

- formal_fallacies: "valid" or "invalid"
- navigate, web_of_lies: "Yes" or "No"
- object_counting: a whole number
"""

from __future__ import annotations

import re
from dataclasses import dataclass

YES_NO_CATEGORIES = {"navigate", "web_of_lies"}

# How the answer was found, from most to least trustworthy.
LEAD = "lead"          # the reply opens with the answer
PATTERN = "pattern"    # an explicit "the answer is ..." style statement
FALLBACK = "fallback"  # the last answer-shaped word in the reply


@dataclass(frozen=True)
class Extraction:
    answer: str | None
    method: str | None

    @property
    def found(self) -> bool:
        return self.answer is not None


_NOT_FOUND = Extraction(None, None)

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

_NUMBER = (
    r"(?:\d+"
    r"|(?:" + "|".join(_TENS) + r")(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")"
)
_NUMBER_RE = re.compile(rf"\b({_NUMBER})\b")

# Phrases where a number word is not a count.
_NOT_A_COUNT = re.compile(
    r"\b(?:one by one|one at a time|one of (?:the|them|these|those)|(?:no|any|some|every|each|which|this|that) one"
    r"|one more|another one|one moment|one second|one sec)\b"
)

_FILLER_LEAD = re.compile(r"^(?:(?:so|okay|ok|alright|all right|well|hmm+|um+|uh+|right|sure|let'?s see|great question)[\s,.!:;-]+)+")

_ANSWER_CUE = r"(?:the\s+)?(?:final\s+)?(?:answer|result|total|count|verdict|conclusion)\s*(?:is|was|would be|comes to|:)\s*(?:that\s+)?(?:a\s+)?"


def _normalise(reply: str) -> str:
    text = reply.lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[*_`#]", " ", text)  # markdown emphasis from text models
    return re.sub(r"\s+", " ", text).strip()


def _to_int(token: str) -> int:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    if token in _UNITS:
        return _UNITS[token]
    parts = re.split(r"[\s-]", token)
    total = _TENS[parts[0]]
    if len(parts) > 1:
        total += _UNITS[parts[1]]
    return total


def _last(matches: list[re.Match]) -> re.Match | None:
    return matches[-1] if matches else None


def _extract_validity(text: str) -> Extraction:
    # Drop restatements of the question ("valid or invalid"), which carry no verdict.
    text = re.sub(r"\b(?:valid\s*(?:,|or|/)\s*invalid|invalid\s*(?:,|or|/)\s*valid)\b", " ", text)
    verdict = re.compile(r"(?:\b(not|never)\s+|(n't)\s+)?(?:\w+ly\s+)?\b(in)?valid\b")

    def read(match: re.Match) -> str:
        negated = bool(match.group(1) or match.group(2))
        invalid = bool(match.group(3))
        return "invalid" if invalid != negated else "valid"

    lead = verdict.match(_FILLER_LEAD.sub("", text))
    if lead:
        return Extraction(read(lead), LEAD)
    cued = _last(list(re.finditer(_ANSWER_CUE + r"(?:\w+ly\s+)?(?:\"|')?(in)?valid\b", text)))
    if cued:
        return Extraction("invalid" if cued.group(1) else "valid", PATTERN)
    found = list(verdict.finditer(text))
    if not found:
        return _NOT_FOUND
    verdicts = {read(m) for m in found}
    # A single consistent verdict is safe. Mixed verdicts mean the reply argued both ways.
    if len(verdicts) == 1:
        return Extraction(verdicts.pop(), FALLBACK)
    return Extraction(read(found[-1]), FALLBACK)


def _extract_yes_no(text: str) -> Extraction:
    lead = re.match(r"(yes|no|yeah|yep|nope)\b", _FILLER_LEAD.sub("", text))
    if lead:
        return Extraction("Yes" if lead.group(1) in {"yes", "yeah", "yep"} else "No", LEAD)
    cued = _last(list(re.finditer(_ANSWER_CUE + r"(?:\"|')?(yes|no)\b", text)))
    if cued:
        return Extraction(cued.group(1).capitalize(), PATTERN)
    # "no" is also an ordinary word ("no steps left"), so only standalone uses count:
    # at the start of a sentence or set off by punctuation.
    standalone = [m.group(1) for m in re.finditer(r"(?:^|[.!?;:,]\s+|\bso\s+|\bthen\s+)(yes|no)\b(?=\s*[,.!?;:]|$)", text)]
    values = set(standalone)
    if len(values) == 1:
        return Extraction(standalone[-1].capitalize(), FALLBACK)
    return _NOT_FOUND


def _extract_count(text: str) -> Extraction:
    cleaned = _NOT_A_COUNT.sub(" ", text)
    cue = re.compile(
        r"(?:" + _ANSWER_CUE + r"|\b(?:you have|there are|there're|that makes|that's|that is|it's|equals|=|totals?|altogether|in total,?"
        r"|i count|i get|i got|we get|comes to|adds up to|gives(?: you| us)?)\s+(?:a total of\s+)?)"
        rf"({_NUMBER})\b"
    )
    lead = re.match(rf"({_NUMBER})\b", _FILLER_LEAD.sub("", cleaned))
    if lead:
        return Extraction(str(_to_int(lead.group(1))), LEAD)
    cued = _last(list(cue.finditer(cleaned)))
    if cued:
        return Extraction(str(_to_int(cued.group(1))), PATTERN)
    numbers = _NUMBER_RE.findall(cleaned)
    if numbers:
        return Extraction(str(_to_int(numbers[-1])), FALLBACK)
    return _NOT_FOUND


def extract(category: str, reply: str) -> Extraction:
    """Return the answer a reply settles on, or an empty extraction if unclear."""
    text = _normalise(reply)
    if not text:
        return _NOT_FOUND
    if category == "formal_fallacies":
        return _extract_validity(text)
    if category in YES_NO_CATEGORIES:
        return _extract_yes_no(text)
    if category == "object_counting":
        return _extract_count(text)
    raise ValueError(f"unknown category: {category}")


def is_correct(category: str, extracted: str | None, official: str) -> bool | None:
    """Compare an extracted answer with the official one. ``None`` means not scoreable."""
    if extracted is None:
        return None
    if category == "object_counting":
        return int(extracted) == int(official)
    return extracted.strip().lower() == official.strip().lower()
