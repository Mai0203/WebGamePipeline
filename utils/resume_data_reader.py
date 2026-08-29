"""读取 resume 模式输入，并解析已有项目目录与 cc 轨迹路径。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data_reader import (
    InputRecord,
    normalize_cell,
    read_csv_rows,
    read_excel_rows,
    validate_record,
)
from .jsonl_utils import iter_jsonl


PROJECT_DIR_FIELDS = (
    "project_dir",
    "project_path",
    "existing_project_dir",
    "existing_project_path",
    "source_project_dir",
    "source_project_path",
)

CC_TRAJECTORY_FIELDS = (
    "cc_trajectory_path",
    "trajectory_path",
    "cc_trace_path",
    "trace_path",
    "messages_path",
    "messages_file",
)


@dataclass(slots=True)
class ResumeInputRecord:
    """resume 模式下单条样本的标准结构。"""

    data_id: str
    query: str
    project_dir: Path
    cc_trajectory_path: Path | None
    payload: dict[str, Any]

    def to_input_record(self) -> InputRecord:
        """转换成现有主流程可直接复用的 `InputRecord`。"""
        return InputRecord(
            data_id=self.data_id,
            query=self.query,
            payload=dict(self.payload),
        )


def pick_first_value(payload: dict[str, Any], field_names: tuple[str, ...]) -> tuple[str | None, str]:
    """按候选字段顺序取出首个非空值。"""
    for field_name in field_names:
        if field_name not in payload:
            continue
        value = normalize_cell(payload.get(field_name))
        if value:
            return field_name, value
    return None, ""


def resolve_row_path(base_dir: Path, raw_value: str) -> Path:
    """把输入行里的相对路径解析到输入文件所在目录。"""
    path = Path(raw_value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def validate_resume_record(
    payload: dict[str, Any],
    *,
    input_base_dir: Path,
    data_id_field: str,
    query_field: str,
    data_id_prefix: str,
) -> ResumeInputRecord | None:
    """校验 resume 输入行，并解析项目目录与轨迹路径。"""
    base_record = validate_record(
        payload,
        data_id_field=data_id_field,
        query_field=query_field,
        data_id_prefix=data_id_prefix,
    )
    if base_record is None:
        return None

    project_dir_field, project_dir_value = pick_first_value(payload, PROJECT_DIR_FIELDS)
    if not project_dir_field or not project_dir_value:
        raise ValueError(
            "Resume input record missing required project path field. "
            f"Supported field names: {', '.join(PROJECT_DIR_FIELDS)}"
        )

    project_dir = resolve_row_path(input_base_dir, project_dir_value)
    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory does not exist: {project_dir}")
    if not project_dir.is_dir():
        raise NotADirectoryError(f"Project path is not a directory: {project_dir}")

    trajectory_field, trajectory_value = pick_first_value(payload, CC_TRAJECTORY_FIELDS)
    cc_trajectory_path: Path | None = None
    if trajectory_field and trajectory_value:
        cc_trajectory_path = resolve_row_path(input_base_dir, trajectory_value)
        if not cc_trajectory_path.exists():
            raise FileNotFoundError(
                f"CC trajectory file does not exist: {cc_trajectory_path}"
            )
        if not cc_trajectory_path.is_file():
            raise ValueError(
                f"CC trajectory path must be a file, not a directory: {cc_trajectory_path}"
            )

    normalized_payload = dict(base_record.payload)
    normalized_payload["project_dir"] = str(project_dir)
    if project_dir_field != "project_dir":
        normalized_payload[project_dir_field] = str(project_dir)
    if cc_trajectory_path is not None:
        normalized_payload["cc_trajectory_path"] = str(cc_trajectory_path)
        if trajectory_field and trajectory_field != "cc_trajectory_path":
            normalized_payload[trajectory_field] = str(cc_trajectory_path)

    return ResumeInputRecord(
        data_id=base_record.data_id,
        query=base_record.query,
        project_dir=project_dir,
        cc_trajectory_path=cc_trajectory_path,
        payload=normalized_payload,
    )


def load_resume_input_records(config) -> list[ResumeInputRecord]:
    """读取 resume 模式输入列表。"""
    path = config.path
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    if config.limit == 0:
        return []

    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        raw_rows = list(iter_jsonl(path))
    elif suffix == ".csv":
        raw_rows = read_csv_rows(path)
    elif suffix in {".xlsx", ".xls", ".xlsm", ".xltx", ".xltm"}:
        raw_rows = read_excel_rows(path)
    else:
        raise ValueError(
            f"Unsupported input file format: {path}. "
            "Supported: .jsonl, .csv, .xlsx, .xls"
        )

    records: list[ResumeInputRecord] = []
    for payload in raw_rows:
        record = validate_resume_record(
            payload,
            input_base_dir=path.parent,
            data_id_field=config.data_id_field,
            query_field=config.query_field,
            data_id_prefix=config.data_id_prefix,
        )
        if record is None:
            continue
        records.append(record)
        if config.limit is not None and len(records) >= config.limit:
            break

    return records
