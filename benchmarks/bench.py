"""Time parse-broken-json against json_repair and jiter on a truncated document.

The document is generated from a fixed seed, so every run parses the same
input: about 284 KB of records like the ones a model returns, cut in the
middle. Before anything is timed, each parser must return every complete
record, so none of them can win by giving up early; the complete records it
returns altered are counted and reported next to its time.

Times are the best of several rounds, and every round runs each parser once,
so background load on the machine slows them all alike.

    uv run --group bench python benchmarks/bench.py
"""

import json
import platform
import random
import timeit
from collections.abc import Callable
from importlib.metadata import version

import jiter
import json_repair

from parse_broken_json import StreamParser, __version__, parse_broken_json

SEED = 2026
SIZE = 284 * 1024  # bytes of JSON before the cut
CHUNK = 4 * 1024  # characters per StreamParser.feed() call
ROUNDS = 20

# Prose: mostly plain words, with a quote, a newline or non-ASCII text now and then.
PLAIN = (
    "the a an model reply returned value values list field fields with data from to of in for"
    " on this that is are was more some each user item result answer however, first, then,"
    " finally, also, note: example: total, and or but not"
).split()
SPECIAL = ['"quoted"', "line\nbreak", "café", "niño", "東京", "🚀", "C:\\temp", "tab\tstop"]
TAGS = ["json", "llm", "stream", "repair", "python", "api", "tokens", "schema"]
NAMES = ["Ann", "Bruno", "Chen", "Dalia", "Émile", "Fatima", "Gustavo", "Hiroshi"]


def sentence(rng: random.Random, low: int, high: int) -> str:
    count = rng.randint(low, high)
    return " ".join(rng.choice(SPECIAL if rng.random() < 0.05 else PLAIN) for _ in range(count))


def make_records(rng: random.Random) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    size = 2  # the brackets
    while size < SIZE:
        record = {
            "id": len(records),
            "title": sentence(rng, 3, 8),
            "summary": sentence(rng, 20, 60),
            "tags": rng.sample(TAGS, rng.randint(1, 4)),
            "score": round(rng.uniform(0, 100), 3),
            "published": rng.random() < 0.7,
            "author": {
                "name": rng.choice(NAMES),
                "email": None if rng.random() < 0.2 else f"user{rng.randint(1, 999)}@example.com",
            },
            "metrics": {"views": rng.randint(0, 10**6), "ratio": rng.random()},
        }
        records.append(record)
        size += len(json.dumps(record, ensure_ascii=False).encode()) + 2  # and ", "
    return records


def best_ms(parsers: dict[str, Callable[[], object]]) -> dict[str, float]:
    best = dict.fromkeys(parsers, float("inf"))
    for _ in range(ROUNDS):
        for name, parse in parsers.items():
            best[name] = min(best[name], timeit.Timer(parse).timeit(number=1))
    return {name: seconds * 1000 for name, seconds in best.items()}


def main() -> None:
    records = make_records(random.Random(SEED))
    text = json.dumps(records, ensure_ascii=False)
    cut = text[: len(text) // 2]
    data = cut.encode()

    # How many records end before the cut: every parser must return them.
    complete, end = 0, 1
    for record in records:
        end += len(json.dumps(record, ensure_ascii=False))
        if end > len(cut):
            break
        complete += 1
        end += 2

    def stream() -> object:
        parser = StreamParser()
        for start in range(0, len(cut), CHUNK):
            parser.feed(cut[start : start + CHUNK])
            parser.snapshot()
        return parser.finish().value

    parsers: dict[str, Callable[[], object]] = {
        f"parse-broken-json {__version__}": lambda: parse_broken_json(cut),
        f"json_repair {version('json-repair')}": lambda: json_repair.repair_json(
            cut, return_objects=True
        ),
        f"jiter {version('jiter')}": lambda: jiter.from_json(data, partial_mode="trailing-strings"),
        "StreamParser, 4 KB chunks": stream,
    }
    altered: dict[str, int] = {}
    for name, parse in parsers.items():
        value = parse()
        if not isinstance(value, list) or len(value) < complete:
            raise SystemExit(f"{name} did not return the {complete} complete records")
        pairs = zip(value[:complete], records[:complete], strict=True)
        altered[name] = sum(got != want for got, want in pairs)

    print(f"Python {platform.python_version()}, {platform.system()} {platform.machine()}")
    print(
        f"{len(text.encode()) / 1024:.0f} KB document cut to {len(data) / 1024:.0f} KB,"
        f" {complete} complete records; best of {ROUNDS} rounds:"
    )
    for name, ms in best_ms(parsers).items():
        note = f"   {altered[name]} complete records altered" if altered[name] else ""
        print(f"  {name:<28}{ms:7.1f} ms{note}")
    print("  (StreamParser takes a snapshot after each chunk)")


if __name__ == "__main__":
    main()
