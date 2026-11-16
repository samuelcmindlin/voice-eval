"""Big Bench Audio: metadata, text twins, sampling and audio preparation.

Big Bench Audio (Artificial Analysis, MIT licence) is 1,000 spoken questions
taken from four Big Bench Hard tasks, 250 each. The audio dataset ships no
question text, but its ids line up one-to-one with the Big Bench Hard source
files (checked: all 1,000 answers match in order), so the source text serves as
the text twin of each spoken question.

Downloaded files are untrusted data. They live under ``data/`` and are only
ever read as JSON or passed to ffmpeg by path.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

HF_REPO = "ArtificialAnalysis/big_bench_audio"
BBH_URL = "https://raw.githubusercontent.com/suzgunmirac/BIG-Bench-Hard/main/bbh/{task}.json"

# Category -> first id in Big Bench Audio. Each category holds 250 questions.
CATEGORY_OFFSETS = {
    "formal_fallacies": 0,
    "navigate": 250,
    "object_counting": 500,
    "web_of_lies": 750,
}
PER_CATEGORY = 250

# The audio adds a short instruction after each source question.
SPOKEN_SUFFIX = "Answer the question."

# Live voice APIs take 24 kHz mono 16-bit PCM.
PCM_RATE = 24_000


@dataclass(frozen=True)
class Question:
    id: int
    category: str
    answer: str
    text: str
    audio_mp3: str  # path relative to the dataset directory


def _dataset_dir(data_dir: Path) -> Path:
    return data_dir / "big_bench_audio"


def fetch_metadata(data_dir: Path) -> Path:
    """Download metadata.jsonl (98 KB) if it is not already present."""
    from huggingface_hub import hf_hub_download

    target = _dataset_dir(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    # token=False: the dataset is public, and a stale stored login would otherwise fail.
    return Path(
        hf_hub_download(HF_REPO, "metadata.jsonl", repo_type="dataset", local_dir=target, token=False)
    )


def fetch_bbh(data_dir: Path) -> dict[str, list[dict]]:
    """Download the four Big Bench Hard source files and return their examples."""
    out: dict[str, list[dict]] = {}
    bbh_dir = data_dir / "bbh"
    bbh_dir.mkdir(parents=True, exist_ok=True)
    for task in CATEGORY_OFFSETS:
        path = bbh_dir / f"{task}.json"
        if not path.exists():
            with urllib.request.urlopen(BBH_URL.format(task=task), timeout=60) as resp:
                path.write_bytes(resp.read())
        out[task] = json.loads(path.read_text(encoding="utf-8"))["examples"]
    return out


def load_questions(data_dir: Path) -> list[Question]:
    """Join the audio metadata with the source text, verifying the alignment."""
    meta_path = fetch_metadata(data_dir)
    rows = [json.loads(line) for line in meta_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    bbh = fetch_bbh(data_dir)

    questions: list[Question] = []
    for row in sorted(rows, key=lambda r: r["id"]):
        category = row["category"]
        index = row["id"] - CATEGORY_OFFSETS[category]
        if not 0 <= index < PER_CATEGORY:
            raise ValueError(f"id {row['id']} is outside the expected range for {category}")
        source = bbh[category][index]
        official = str(row["official_answer"]).strip()
        if official.lower() != str(source["target"]).strip().lower():
            raise ValueError(
                f"id {row['id']}: audio answer {official!r} does not match source answer {source['target']!r}"
            )
        questions.append(
            Question(
                id=row["id"],
                category=category,
                answer=official,
                text=f"{source['input'].strip()}\n{SPOKEN_SUFFIX}",
                audio_mp3=row["file_name"],
            )
        )
    if len(questions) != PER_CATEGORY * len(CATEGORY_OFFSETS):
        raise ValueError(f"expected 1000 questions, found {len(questions)}")
    return questions


def sample(questions: list[Question], per_category: int, seed: int) -> list[Question]:
    """Pick ``per_category`` questions from each category, balanced across answers.

    Three of the four categories are two-way choices, so an unbalanced draw
    would shift the chance baseline. Within a category the draw takes answers
    in rotation, most common first, until the quota is filled.
    """
    rng = random.Random(seed)
    picked: list[Question] = []
    by_category: dict[str, list[Question]] = defaultdict(list)
    for q in questions:
        by_category[q.category].append(q)

    for category in CATEGORY_OFFSETS:
        by_answer: dict[str, list[Question]] = defaultdict(list)
        for q in by_category[category]:
            by_answer[q.answer].append(q)
        for pool in by_answer.values():
            rng.shuffle(pool)
        answers = sorted(by_answer, key=lambda a: (-len(by_answer[a]), a))
        chosen: list[Question] = []
        while len(chosen) < per_category:
            progressed = False
            for answer in answers:
                if by_answer[answer] and len(chosen) < per_category:
                    chosen.append(by_answer[answer].pop())
                    progressed = True
            if not progressed:
                raise ValueError(f"{category} has fewer than {per_category} questions")
        picked.extend(sorted(chosen, key=lambda q: q.id))
    return picked


def fetch_audio(questions: list[Question], data_dir: Path) -> None:
    """Download the MP3 for each question (about 0.3 MB each)."""
    from huggingface_hub import hf_hub_download

    target = _dataset_dir(data_dir)
    for q in questions:
        if not (target / q.audio_mp3).exists():
            hf_hub_download(HF_REPO, q.audio_mp3, repo_type="dataset", local_dir=target, token=False)


def wav_path(q: Question, data_dir: Path) -> Path:
    return data_dir / "pilot" / "wav" / f"question_{q.id}.wav"


def convert_to_wav(questions: list[Question], data_dir: Path) -> None:
    """Convert each MP3 to 24 kHz mono 16-bit WAV with ffmpeg."""
    for q in questions:
        dst = wav_path(q, data_dir)
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = _dataset_dir(data_dir) / q.audio_mp3
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
             "-ac", "1", "-ar", str(PCM_RATE), "-c:a", "pcm_s16le", str(dst)],
            check=True,
        )


def write_jsonl(questions: list[Question], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(asdict(q), ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[Question]:
    return [Question(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the Big Bench Audio pilot sample.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--per-category", type=int, default=45)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--out", type=Path, default=Path("data/pilot/questions.jsonl"))
    parser.add_argument("--skip-audio", action="store_true", help="write the sample without downloading audio")
    args = parser.parse_args()

    questions = load_questions(args.data_dir)
    chosen = sample(questions, args.per_category, args.seed)
    write_jsonl(chosen, args.out)
    print(f"sampled {len(chosen)} questions -> {args.out}")
    if not args.skip_audio:
        fetch_audio(chosen, args.data_dir)
        convert_to_wav(chosen, args.data_dir)
        print(f"audio ready under {args.data_dir / 'pilot' / 'wav'}")


if __name__ == "__main__":
    main()
