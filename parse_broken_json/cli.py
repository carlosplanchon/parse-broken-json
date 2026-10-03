"""Command-line interface: ``parse-broken-json [file] [options]``."""

from __future__ import annotations

import argparse
import json
import sys

from . import BrokenJSONError, __version__, parse_broken_json_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parse-broken-json",
        description="Repair broken or truncated JSON and print it as valid JSON.",
    )
    parser.add_argument("file", nargs="?", help="input file (default: standard input)")
    parser.add_argument(
        "-o", "--output", help="write the result to this file instead of standard output"
    )
    parser.add_argument(
        "-i", "--inline", action="store_true", help="overwrite the input file with the result"
    )
    parser.add_argument(
        "--indent", type=int, default=2, help="indentation of the output (default: 2)"
    )
    parser.add_argument("--compact", action="store_true", help="single-line output")
    parser.add_argument(
        "--keys", help="comma-separated list of keys to keep, at every nesting level"
    )
    parser.add_argument("--strict", action="store_true", help="fail instead of repairing")
    parser.add_argument(
        "--stream-stable",
        action="store_true",
        help="drop the values that may still grow at the end of the input",
    )
    parser.add_argument(
        "--multiple",
        choices=("auto", "all", "first", "longest"),
        default="auto",
        help="what to return when the text holds several values (default: auto)",
    )
    parser.add_argument("--report", action="store_true", help="list the repairs on standard error")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.inline and not args.file:
        print("error: --inline needs a file", file=sys.stderr)
        return 2
    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as fp:
            text = fp.read()
    else:
        text = sys.stdin.read()
    keys = [key.strip() for key in args.keys.split(",")] if args.keys else None
    try:
        result = parse_broken_json_result(
            text,
            keys,
            strict=args.strict,
            stream_stable=args.stream_stable,
            multiple=args.multiple,
        )
    except BrokenJSONError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not result.found:
        print("error: no JSON value found", file=sys.stderr)
        return 1
    if args.report:
        for repair in result.repairs:
            print(f"{repair.position}: {repair.message}", file=sys.stderr)
    indent = None if args.compact else args.indent
    out = json.dumps(result.value, ensure_ascii=False, indent=indent) + "\n"
    target = args.file if args.inline else args.output
    if target:
        with open(target, "w", encoding="utf-8") as fp:
            fp.write(out)
    else:
        sys.stdout.write(out)
    return 0
