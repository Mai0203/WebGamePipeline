"""JSONL 读写辅助工具。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterable


class AsyncJsonlWriter:
    """带异步锁的 JSONL 追加写入器，避免并发写冲突。"""
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = asyncio.Lock()

    async def append(self, record: dict[str, Any]) -> None:
        """把一条记录原子地追加到 JSONL 文件末尾。"""
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """逐行读取 JSONL，并在遇到非法 JSON 时抛出明确错误。"""
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no} in {path}: {exc}") from exc


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    """一次性加载整个 JSONL 文件。"""
    if not path.exists():
        return []
    return list(iter_jsonl(path))
