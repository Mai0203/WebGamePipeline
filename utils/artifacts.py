"""负责项目产物目录、快照和 JSON 文件的组织与持久化。"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXCLUDED_DIRS = {"node_modules", "dist", ".git", "__pycache__"}
EXCLUDED_FILES: set[str] = set()


@dataclass(slots=True)
class ArtifactLayout:
    """全局输出目录下的核心产物文件布局。"""
    output_dir: Path
    project_files_jsonl: Path
    verifier_result_jsonl: Path
    token_usage_jsonl: Path


@dataclass(slots=True)
class RoundPaths:
    """单个样本单轮执行的目录与文件路径集合。"""
    record_root: Path
    main_root: Path
    main_project_dir: Path
    main_prompt_file: Path
    main_messages_file: Path
    main_verifier_file: Path
    round_root: Path
    project_dir: Path
    prompt_file: Path
    messages_file: Path
    verifier_file: Path


def build_artifact_layout(output_dir: Path) -> ArtifactLayout:
    """初始化输出目录，并确保全局 JSONL 文件存在。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    layout = ArtifactLayout(
        output_dir=output_dir,
        project_files_jsonl=output_dir / "project_files.jsonl",
        verifier_result_jsonl=output_dir / "verifier_result.jsonl",
        token_usage_jsonl=output_dir / "token_usage.jsonl",
    )
    for path in (
        layout.project_files_jsonl,
        layout.verifier_result_jsonl,
        layout.token_usage_jsonl,
    ):
        path.touch(exist_ok=True)
    return layout


def build_round_paths(output_dir: Path, data_id: str, round_index: int) -> RoundPaths:
    """根据样本 ID 和轮次生成固定的产物路径。"""
    record_root = output_dir / data_id
    main_root = record_root / "main"
    round_root = record_root / f"round_{round_index}"
    return RoundPaths(
        record_root=record_root,
        main_root=main_root,
        main_project_dir=main_root / "project",
        main_prompt_file=main_root / "prompt.txt",
        main_messages_file=main_root / "messages.json",
        main_verifier_file=main_root / "verifier_result.json",
        round_root=round_root,
        project_dir=round_root / "project",
        prompt_file=round_root / "prompt.txt",
        messages_file=round_root / "messages.json",
        verifier_file=round_root / "verifier_result.json",
    )


def prepare_workspace_root(workspace_root: Path, preserve_project: bool = False) -> None:
    """为 main 工作目录做轮次级清理，可选择保留当前项目目录。"""
    if workspace_root.exists():
        for child in workspace_root.iterdir():
            if preserve_project and child.name == "project":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    workspace_root.mkdir(parents=True, exist_ok=True)
    if preserve_project:
        (workspace_root / "project").mkdir(parents=True, exist_ok=True)


def prepare_empty_project_dir(project_dir: Path) -> None:
    """为当前轮次准备一个全新的项目目录。"""
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)


def clone_project_dir(src: Path, dst: Path) -> None:
    """把上一轮项目复制成下一轮的起点目录。"""
    if not src.exists():
        raise FileNotFoundError(f"Source project directory does not exist: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("node_modules", "dist", "__pycache__"),
    )


def snapshot_workspace_root(src: Path, dst: Path) -> None:
    """把当前 main 工作目录快照到指定 round 目录，跳过重型生成目录。"""
    if not src.exists():
        raise FileNotFoundError(f"Source workspace directory does not exist: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("node_modules", "__pycache__"),
    )


def collect_project_files(project_dir: Path) -> dict[str, Any]:
    """扫描项目目录并收集可序列化的文本文件内容。"""
    files: dict[str, str] = {}
    if not project_dir.exists():
        return {"status": "error", "files": files}

    for file_path in sorted(project_dir.rglob("*")):
        if file_path.is_dir():
            continue
        if any(part in EXCLUDED_DIRS for part in file_path.parts):
            continue
        if file_path.name in EXCLUDED_FILES:
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # 二进制文件不写入 JSONL 快照，避免序列化和体积问题。
            continue
        files[file_path.relative_to(project_dir).as_posix()] = content

    return {
        "status": "success",
        "files": files,
    }


def materialize_project_files(project_dir: Path, files: dict[str, str]) -> None:
    """根据快照内容重新还原项目目录。"""
    prepare_empty_project_dir(project_dir)
    for relative_path, content in files.items():
        file_path = project_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    """以 UTF-8 和缩进格式写出 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
