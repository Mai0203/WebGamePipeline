"""加载并渲染 prompt 模板。"""

from __future__ import annotations

import re
from pathlib import Path


PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def load_prompt_template(path: Path) -> str:
    """读取 prompt 模板文本。"""
    if not path.exists():
        raise FileNotFoundError(f"Prompt template does not exist: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, context: dict[str, str]) -> str:
    """把 `{{variable}}` 占位符替换成上下文里的实际值。"""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, "")

    return PLACEHOLDER_PATTERN.sub(replace, template)
