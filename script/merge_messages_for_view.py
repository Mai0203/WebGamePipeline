#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ORIGINAL_MESSAGE_KEYS = ("type", "text", "payload")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge one or more wrapped messages.json files into a viewer-friendly "
            "trajectory file while keeping only the original per-message fields."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input messages.json files in trajectory order.",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output JSON file path.",
    )
    parser.add_argument(
        "--drop-payload",
        action="store_true",
        help="Drop the original payload field and keep only type/text.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def simplify_message(message: dict[str, Any], *, keep_payload: bool) -> dict[str, Any]:
    if message.get("type") == "UserMessage":
        return {"text": message.get("text", "")}

    simplified: dict[str, Any] = {}
    for key in ORIGINAL_MESSAGE_KEYS:
        if key == "payload" and not keep_payload:
            continue
        if key in message:
            simplified[key] = message[key]
    return simplified


def load_round(path: Path, *, keep_payload: bool) -> dict[str, Any]:
    payload = load_json(path)

    if isinstance(payload, list):
        raw_messages = payload
        effective_query = None
    elif isinstance(payload, dict):
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError(f"{path} is missing a top-level messages array")
        effective_query = payload.get("effective_query")
    else:
        raise ValueError(f"{path} must contain a JSON object or JSON array")

    messages = []
    for index, message in enumerate(raw_messages):
        if not isinstance(message, dict):
            raise ValueError(f"{path} message #{index} is not a JSON object")
        messages.append(simplify_message(message, keep_payload=keep_payload))

    return {
        "source_file": str(path),
        "round_name": path.parent.name,
        "effective_query": effective_query,
        "messages": messages,
    }


def build_output(rounds: list[dict[str, Any]], *, keep_payload: bool) -> dict[str, Any]:
    merged_messages: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    cursor = 0

    for round_info in rounds:
        messages = round_info["messages"]
        next_cursor = cursor + len(messages)
        segments.append(
            {
                "round_name": round_info["round_name"],
                "source_file": round_info["source_file"],
                "effective_query": round_info["effective_query"],
                "start_index": cursor,
                "end_index_exclusive": next_cursor,
            }
        )
        merged_messages.extend(messages)
        cursor = next_cursor

    message_fields = ["type", "text"]
    if keep_payload:
        message_fields.append("payload")

    return {
        "format": "trajectory_view_v1",
        "message_fields": message_fields,
        "source_files": [round_info["source_file"] for round_info in rounds],
        "segments": segments,
        "messages": merged_messages,
    }


def main() -> None:
    args = parse_args()
    keep_payload = not args.drop_payload

    input_paths = [Path(value).expanduser().resolve() for value in args.inputs]
    output_path = Path(args.output).expanduser().resolve()

    rounds = [load_round(path, keep_payload=keep_payload) for path in input_paths]
    output_payload = build_output(rounds, keep_payload=keep_payload)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
