"""批量生成 React 项目、保存产物并串联 verifier 与多轮优化的主入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.agent_runner import ClaudeAgentSession, create_agent_session
from utils.artifacts import (
    build_artifact_layout,
    build_round_paths,
    clone_project_dir,
    collect_project_files,
    materialize_project_files,
    prepare_workspace_root,
    prepare_empty_project_dir,
    snapshot_workspace_root,
    write_json,
)
from utils.config_loader import AppConfig, load_config
from utils.data_reader import InputRecord, load_input_records
from utils.jsonl_utils import AsyncJsonlWriter, load_jsonl_records
from utils.prompt_loader import load_prompt_template, render_prompt
from utils.verifier import run_verifier_async, summarize_verifier_result


logger = logging.getLogger("cc_project")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_logging() -> None:
    """初始化控制台日志输出。"""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def log_round(record: InputRecord, round_index: int, message: str) -> None:
    """输出带样本和轮次信息的日志。"""
    logger.info(
        "data_id=%s | round=%s | %s",
        record.data_id,
        round_name(round_index),
        message,
    )


def parse_args() -> argparse.Namespace:
    """定义命令行参数，允许在配置文件之外做临时覆盖。"""
    parser = argparse.ArgumentParser(
        description="Use claude_agent_sdk to batch-build and optimize React projects.",
    )
    parser.add_argument(
        "--config",
        default="env_config.yml",
        help="Path to the runtime config file. Default: env_config.yml",
    )
    parser.add_argument(
        "--input",
        help="Override the input file path declared in the config.",
    )
    parser.add_argument(
        "--output-dir",
        help="Override the output directory declared in the config.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        help="Override optimization rounds. 0 means initial generation only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit the number of input rows processed in this run.",
    )
    parser.add_argument(
        "--prompt-name",
        help="Override prompts.selected in the config.",
    )
    parser.add_argument(
        "--disable-verifier",
        action="store_true",
        help="Skip verifier requests even if verifier.enabled=true in config.",
    )
    parser.add_argument(
        "--disable-optimization",
        action="store_true",
        help="Run only the first generation round.",
    )
    return parser.parse_args()


def apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    """把命令行参数覆盖到配置对象上，便于单次运行调试。"""
    if args.input:
        config.input.path = Path(args.input).expanduser().resolve()
    if args.output_dir:
        config.project.output_dir = Path(args.output_dir).expanduser().resolve()
    if args.rounds is not None:
        config.optimization.rounds = max(0, args.rounds)
        config.optimization.enabled = config.optimization.rounds > 0
    if args.limit is not None:
        config.input.limit = max(0, args.limit)
    if args.prompt_name:
        config.prompts.selected = args.prompt_name
    if args.disable_verifier:
        config.verifier.enabled = False
    if args.disable_optimization:
        config.optimization.enabled = False
        config.optimization.rounds = 0


def round_name(round_index: int) -> str:
    return "generation" if round_index == 0 else f"optimization_{round_index}"


def total_rounds(config: AppConfig) -> int:
    if not config.optimization.enabled:
        return 1
    return 1 + max(0, config.optimization.rounds)


def build_round_key(record: dict[str, Any]) -> tuple[str, int] | None:
    data_id = record.get("data_id")
    round_index = record.get("round_index")
    if data_id is None or round_index is None:
        return None

    try:
        return str(data_id), int(round_index)
    except (TypeError, ValueError):
        return None


def index_round_records(jsonl_path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    index: dict[tuple[str, int], dict[str, Any]] = {}
    for record in load_jsonl_records(jsonl_path):
        key = build_round_key(record)
        if key is not None:
            index[key] = record
    return index


def build_token_usage_records(
    *,
    record: InputRecord,
    round_index: int,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 SDK 消息中提取 ResultMessage 的 token 和 cost 统计。"""
    usage_records: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        if message.get("type") != "ResultMessage":
            continue

        payload = message.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        model_usage = payload.get("model_usage")
        if not isinstance(model_usage, dict):
            model_usage = {}

        usage_records.append(
            {
                "timestamp": utcnow_iso(),
                "data_id": record.data_id,
                "round_index": round_index,
                "round_name": round_name(round_index),
                "stage": message.get("stage"),
                "message_index": message_index,
                "session_query_index": message.get("session_query_index"),
                "session_id": message.get("session_id") or payload.get("session_id"),
                "resume_session_id": message.get("resume_session_id"),
                "fork_session": message.get("fork_session", False),
                "subtype": payload.get("subtype"),
                "is_error": payload.get("is_error"),
                "duration_ms": payload.get("duration_ms"),
                "duration_api_ms": payload.get("duration_api_ms"),
                "num_turns": payload.get("num_turns"),
                "stop_reason": payload.get("stop_reason"),
                "total_cost_usd": payload.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_creation_input_tokens": usage.get(
                    "cache_creation_input_tokens"
                ),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "usage": usage,
                "model_usage": model_usage,
                "uuid": payload.get("uuid"),
                "api_error_status": payload.get("api_error_status"),
                "errors": payload.get("errors"),
            }
        )
    return usage_records


TOKEN_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def numeric_value(value: Any) -> float | None:
    """把可选数值规整为 float，无法解析时返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def add_usage_totals(target: dict[str, Any], usage: dict[str, Any]) -> None:
    """累加 ResultMessage usage 中的核心 token 字段。"""
    for key in TOKEN_USAGE_KEYS:
        value = numeric_value(usage.get(key))
        if value is None:
            continue
        target[key] = target.get(key, 0) + int(value)


def build_token_usage_summary(
    *,
    record: InputRecord,
    output_dir: Path,
    usage_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """把一个 data_id 的 ResultMessage token/cost 汇总为一行 JSONL 记录。"""
    previous_cost_by_session: dict[str, float] = {}
    result_messages: list[dict[str, Any]] = []
    stages: dict[str, dict[str, Any]] = {}
    total_usage: dict[str, Any] = {}
    total_cost_usd = 0.0

    for index, usage_record in enumerate(usage_records):
        usage = usage_record.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        session_id = str(usage_record.get("session_id") or "")
        cumulative_cost = numeric_value(usage_record.get("total_cost_usd"))
        cost_delta = None
        if cumulative_cost is not None:
            previous_cost = previous_cost_by_session.get(session_id)
            if previous_cost is None or cumulative_cost < previous_cost:
                cost_delta = cumulative_cost
            else:
                cost_delta = cumulative_cost - previous_cost
            previous_cost_by_session[session_id] = cumulative_cost
            total_cost_usd += cost_delta

        stage = str(usage_record.get("stage") or "unknown")
        stage_summary = stages.setdefault(
            stage,
            {
                "result_message_count": 0,
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )
        stage_summary["result_message_count"] += 1
        if cost_delta is not None:
            stage_summary["cost_usd"] += cost_delta

        add_usage_totals(stage_summary, usage)
        add_usage_totals(total_usage, usage)

        result_messages.append(
            {
                "index": index,
                "round_index": usage_record.get("round_index"),
                "round_name": usage_record.get("round_name"),
                "stage": stage,
                "session_query_index": usage_record.get("session_query_index"),
                "session_id": usage_record.get("session_id"),
                "resume_session_id": usage_record.get("resume_session_id"),
                "fork_session": usage_record.get("fork_session", False),
                "subtype": usage_record.get("subtype"),
                "is_error": usage_record.get("is_error"),
                "duration_ms": usage_record.get("duration_ms"),
                "duration_api_ms": usage_record.get("duration_api_ms"),
                "num_turns": usage_record.get("num_turns"),
                "stop_reason": usage_record.get("stop_reason"),
                "total_cost_usd_cumulative": cumulative_cost,
                "cost_delta_usd": cost_delta,
                "usage": usage,
                "model_usage": usage_record.get("model_usage") or {},
                "uuid": usage_record.get("uuid"),
                "api_error_status": usage_record.get("api_error_status"),
                "errors": usage_record.get("errors"),
            }
        )

    return {
        "timestamp": utcnow_iso(),
        "data_id": record.data_id,
        "query": record.query,
        "output_dir": str(output_dir),
        "result_message_count": len(result_messages),
        "total_cost_usd": total_cost_usd,
        "total_usage": total_usage,
        "stages": stages,
        "result_messages": result_messages,
    }


def stringify_prompt_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def load_project_template_text(config: AppConfig) -> str:
    """读取首轮生成用的项目模板 JSON 文本。"""
    template_path = config.prompts.project_template_file
    if template_path is None:
        return ""
    if not template_path.exists():
        raise FileNotFoundError(f"Project template file does not exist: {template_path}")
    return template_path.read_text(encoding="utf-8")


def compress_query_preview(query_text: str, limit: int = 120) -> str:
    """压缩 query 文本，避免历史摘要过长。"""
    normalized = " ".join((query_text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def preview_text(value: str, limit: int = 160) -> str:
    """压缩任意文本到适合日志输出的一行。"""
    normalized = " ".join((value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def directory_has_entries(path: Path) -> bool:
    """判断目录是否存在且包含至少一个条目。"""
    if not path.exists():
        return False
    return any(path.iterdir())


def build_history_summary(history: list[dict[str, Any]]) -> str:
    """把前几轮的结果压缩成 prompt 可直接消费的文本。"""
    if not history:
        return "首轮生成，无历史轮次。"

    lines: list[str] = []
    for item in history:
        parsed = item.get("parsed", {})
        verifier = item.get("verifier")
        paths = item.get("paths", {})
        current_round_name = parsed.get("round_name", f"round_{parsed.get('round_index')}")
        effective_query = compress_query_preview(
            str(parsed.get("effective_query") or parsed.get("query") or "")
        )
        lines.append(
            f"- {current_round_name}: "
            f"status={parsed.get('status', 'unknown')}, "
            f"effective_query={effective_query}, "
            f"file_count={parsed.get('project_file_count', 0)}, "
            f"project_dir={parsed.get('project_dir', '')}, "
            f"verifier_file={paths.get('verifier_file', '')}"
            )
        if verifier:
            if verifier.get("error"):
                lines.append(f"  verifier_error={verifier['error']}")
            else:
                lines.append(
                    "  verifier_scores="
                    f"installation:{verifier.get('installation_score')}, "
                    f"running:{verifier.get('running_score')}, "
                    f"aesthetics:{verifier.get('aesthetics_score')}, "
                    f"functional:{verifier.get('functional_score')}"
                )
        resume_seed = parsed.get("resume_seed") or {}
        if resume_seed.get("source_project_dir"):
            lines.append(
                f"  resume_source_project={resume_seed.get('source_project_dir')}"
            )
        if resume_seed.get("copied_cc_trajectory_file"):
            lines.append(
                "  resume_cc_trajectory="
                f"{resume_seed.get('copied_cc_trajectory_file')}"
            )
    return "\n".join(lines)


def build_history_manifest(history: list[dict[str, Any]]) -> str:
    """把所有历史轮次转换成结构化 JSON 文本，供后续轮次直接参考。"""
    manifest_rounds: list[dict[str, Any]] = []
    for item in history:
        project_record = item.get("project_record") or {}
        files = project_record.get("files", {}) or {}
        manifest_rounds.append(
            {
                "round_index": item.get("round_index"),
                "round_name": item.get("round_name"),
                "status": item.get("parsed", {}).get("status"),
                "project_dir": item.get("project_dir"),
                "artifact_paths": item.get("paths", {}),
                "round_metadata": item.get("parsed"),
                "verifier_result": item.get("verifier"),
                "project_files_summary": {
                    "file_count": len(files),
                    "file_paths": sorted(files.keys()),
                },
            }
        )

    payload = {
        "history_round_count": len(manifest_rounds),
        "history_rounds": manifest_rounds,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_effective_query_from_file(effective_query_file: Path) -> str:
    """直接读取 query_analysis 产出的 effective_query.txt。"""
    if not effective_query_file.exists():
        raise FileNotFoundError(
            f"query_analysis did not write effective_query.txt: {effective_query_file}"
        )

    effective_query = effective_query_file.read_text(encoding="utf-8").strip()
    if not effective_query:
        raise RuntimeError(
            f"query_analysis wrote an empty effective_query.txt: {effective_query_file}"
        )

    return effective_query


def build_query_analysis_prompt(
    *,
    config: AppConfig,
    record: InputRecord,
    round_index: int,
    previous_verifier: dict[str, Any] | None,
    effective_query_file: Path,
) -> str:
    """让模型先分析历史结果，再产出下一轮真正要执行的改进需求。"""
    context = {
        "data_id": record.data_id,
        "round_name": round_name(round_index),
        "original_query": record.query,
        "effective_query_file": str(effective_query_file),
        "previous_verifier_result": stringify_prompt_value(previous_verifier),
        "verifier_feedback": summarize_verifier_result(previous_verifier),
    }
    for key, value in config.prompts.variables.items():
        context[key] = stringify_prompt_value(value)
    return render_prompt(
        load_prompt_template(config.prompts.query_analysis_template),
        context,
    )


def build_session_bootstrap_prompt(
    config: AppConfig,
    record: InputRecord,
    history: list[dict[str, Any]],
) -> str:
    """当 resume 复用历史轮次后，用摘要把这些历史注入到新的 client session。"""
    context = {
        "data_id": record.data_id,
        "original_query": record.query,
        "history_summary": build_history_summary(history),
        "history_manifest": build_history_manifest(history),
    }
    for key, value in config.prompts.variables.items():
        context[key] = stringify_prompt_value(value)
    return render_prompt(
        load_prompt_template(config.prompts.session_bootstrap_template),
        context,
    )


def build_prompt_bundle(
    *,
    effective_query: str,
    execution_prompt: str,
    analysis_prompt: str | None = None,
) -> str:
    """把本轮所有 prompt 信息合并到一个调试文件里。"""
    sections = [f"=== effective_query ===\n{effective_query}"]
    if analysis_prompt:
        sections.append(f"=== query_analysis_prompt ===\n{analysis_prompt}")
    sections.append(f"=== execution_prompt ===\n{execution_prompt}")
    return "\n\n".join(sections) + "\n"


def build_messages_payload(
    *,
    status: str,
    effective_query: str,
    steps: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    error: str | None = None,
) -> dict[str, Any]:
    """组织单轮消息轨迹，兼容分析与执行两个阶段。"""
    return {
        "status": status,
        "effective_query": effective_query,
        "error": error,
        "step_count": len(steps),
        "steps": steps,
        "message_count": len(messages),
        "messages": messages,
        "timestamp": utcnow_iso(),
    }


def build_history_entry(
    *,
    round_index: int,
    paths,
    project_record: dict[str, Any] | None,
    parsed: dict[str, Any],
    verifier: dict[str, Any] | None,
) -> dict[str, Any]:
    """统一组织单轮历史信息，供后续轮次复用。"""
    effective_query_file = paths.round_root / "effective_query.txt"
    return {
        "round_index": round_index,
        "round_name": round_name(round_index),
        "project_dir": str(paths.project_dir),
        "paths": {
            "round_root": str(paths.round_root),
            "project_dir": str(paths.project_dir),
            "prompt_file": str(paths.prompt_file),
            "messages_file": str(paths.messages_file),
            "verifier_file": str(paths.verifier_file),
            "effective_query_file": str(effective_query_file),
        },
        "project_record": project_record,
        "parsed": parsed,
        "verifier": verifier,
    }


def build_prompt_context(
    record: InputRecord,
    config: AppConfig,
    round_index: int,
    project_dir: Path,
    history: list[dict[str, Any]],
    previous_verifier: dict[str, Any] | None,
    effective_query: str,
) -> dict[str, str]:
    """整理 prompt 模板变量，统一首轮生成和后续优化轮次的上下文。"""
    project_template_file = config.prompts.project_template_file
    project_template_text = ""
    if round_index == 0 and project_template_file is not None:
        project_template_text = load_project_template_text(config)

    context = {
        "data_id": record.data_id,
        "query": effective_query,
        "original_query": record.query,
        "effective_query": effective_query,
        "workspace_dir": str(project_dir),
        "output_dir": str(project_dir),
        "round_index": str(round_index),
        "round_name": round_name(round_index),
        "history_summary": build_history_summary(history),
        "verifier_feedback": summarize_verifier_result(previous_verifier),
        "project_template_file": str(project_template_file) if project_template_file else "",
        "project_template_json": project_template_text,
    }

    for key, value in config.prompts.variables.items():
        context[key] = stringify_prompt_value(value)

    return context


def build_project_record(
    record: InputRecord,
    round_index: int,
    prompt_name: str,
    project_dir: Path,
    files: dict[str, str],
    effective_query: str,
) -> dict[str, Any]:
    payload = dict(record.payload)
    payload.update(
        {
            "data_id": record.data_id,
            "query": record.query,
            "effective_query": effective_query,
            "round_index": round_index,
            "round_name": round_name(round_index),
            "prompt_name": prompt_name,
            "project_dir": str(project_dir),
            "status": "success",
            "files": files,
            "timestamp": utcnow_iso(),
        }
    )
    return payload


def can_reuse_existing_round(
    *,
    config: AppConfig,
    existing_project: dict[str, Any] | None,
    existing_verifier: dict[str, Any] | None,
) -> tuple[bool, str]:
    """判断当前轮次是否可以直接复用历史结果。"""
    if not existing_project:
        return False, "缺少 project_files 快照"
    if not config.verifier.enabled:
        return True, "当前运行未启用 verifier"
    if not existing_verifier:
        return False, "当前运行启用了 verifier，但历史 verifier 结果缺失"
    if existing_verifier.get("error"):
        return False, "历史 verifier 结果为失败，需要重跑"
    existing_request_count = int(existing_verifier.get("request_count") or 1)
    configured_request_count = max(
        1,
        int(getattr(config.verifier, "request_count", 1)),
    )
    if existing_request_count < configured_request_count:
        return (
            False,
            "历史 verifier 请求次数少于当前配置，需要重跑",
        )
    return True, "历史项目和 verifier 结果都可复用"


def build_parsed_record(
    *,
    record: InputRecord,
    round_index: int,
    prompt_name: str,
    status: str,
    project_dir: Path,
    project_file_count: int,
    verifier_record: dict[str, Any] | None,
    effective_query: str,
    error: str | None = None,
) -> dict[str, Any]:
    verifier_status = "disabled"
    verifier_scores: dict[str, Any] | None = None
    verifier_error: str | None = None

    if verifier_record is not None:
        verifier_status = "error" if verifier_record.get("error") else "success"
        verifier_error = verifier_record.get("error")
        verifier_scores = {
            "installation": verifier_record.get("installation_score"),
            "running": verifier_record.get("running_score"),
            "aesthetics": verifier_record.get("aesthetics_score"),
            "functional": verifier_record.get("functional_score"),
        }

    return {
        "data_id": record.data_id,
        "query": record.query,
        "effective_query": effective_query,
        "round_index": round_index,
        "round_name": round_name(round_index),
        "prompt_name": prompt_name,
        "status": status,
        "error": error,
        "project_dir": str(project_dir),
        "project_file_count": project_file_count,
        "verifier_status": verifier_status,
        "verifier_error": verifier_error,
        "verifier_scores": verifier_scores,
        "timestamp": utcnow_iso(),
    }


def average_numeric(values: list[Any]) -> float | None:
    """计算可解析数值的平均值。"""
    numbers = [numeric_value(value) for value in values]
    clean_numbers = [value for value in numbers if value is not None]
    if not clean_numbers:
        return None
    return sum(clean_numbers) / len(clean_numbers)


def aggregate_verifier_results(
    *,
    record: InputRecord,
    round_index: int,
    verifier_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """把同一轮多次 verifier 请求聚合成一条可落盘记录。"""
    successful_results = [
        result for result in verifier_results if not result.get("error")
    ]
    request_errors = [
        {
            "verifier_request_index": result.get("verifier_request_index", index),
            "error": result.get("error"),
        }
        for index, result in enumerate(verifier_results, start=1)
        if result.get("error")
    ]
    score_source = successful_results or verifier_results
    aggregate: dict[str, Any] = {
        "data_id": record.data_id,
        "query": record.query,
        "round_index": round_index,
        "round_name": round_name(round_index),
        "request_count": len(verifier_results),
        "successful_request_count": len(successful_results),
        "failed_request_count": len(verifier_results) - len(successful_results),
        "request_errors": request_errors,
        "verifier_results": verifier_results,
        "error": None if successful_results else "All verifier requests failed.",
        "timestamp": utcnow_iso(),
    }

    if verifier_results:
        last_result = verifier_results[-1]
        aggregate.update(
            {
                "language": last_result.get("language"),
                "agent_type": last_result.get("agent_type"),
                "project_path": last_result.get("project_path"),
                "response_format": "multi_request",
                "raw_response": [
                    result.get("raw_response") for result in verifier_results
                ],
            }
        )

    for key in ("installation", "running", "aesthetics", "functional"):
        aggregate[f"{key}_score"] = average_numeric(
            [result.get(f"{key}_score") for result in score_source]
        )
        aggregate[f"{key}_reason"] = "\n".join(
            (
                f"[request {result.get('verifier_request_index', index)}] "
                f"{result.get(f'{key}_reason')}"
            )
            for index, result in enumerate(verifier_results, start=1)
            if result.get(f"{key}_reason")
        ) or None

    return aggregate


async def run_verifier_requests_async(
    *,
    record: InputRecord,
    round_index: int,
    files: dict[str, str],
    verifier_config: Any,
) -> dict[str, Any]:
    """按配置多次请求 verifier，并聚合所有结果。"""
    verifier_results: list[dict[str, Any]] = []
    request_count = max(1, int(getattr(verifier_config, "request_count", 1)))
    for request_index in range(1, request_count + 1):
        logger.info(
            "data_id=%s | round=%s | verifier 请求 %s/%s",
            record.data_id,
            round_name(round_index),
            request_index,
            request_count,
        )
        result = await run_verifier_async(
            record=record,
            round_index=round_index,
            files=files,
            verifier_config=verifier_config,
        )
        result["verifier_request_index"] = request_index
        result["verifier_request_count"] = request_count
        verifier_results.append(result)

    return aggregate_verifier_results(
        record=record,
        round_index=round_index,
        verifier_results=verifier_results,
    )


async def process_round(
    *,
    record: InputRecord,
    config: AppConfig,
    writers: dict[str, AsyncJsonlWriter],
    token_usage_records: list[dict[str, Any]],
    round_index: int,
    previous_project_dir: Path | None,
    history: list[dict[str, Any]],
    previous_verifier: dict[str, Any] | None,
    agent_session: ClaudeAgentSession,
) -> dict[str, Any]:
    """执行单轮任务，包括需求分析、Agent 调用、产物落盘和 verifier。"""
    artifact_layout = build_artifact_layout(config.project.output_dir)
    paths = build_round_paths(artifact_layout.output_dir, record.data_id, round_index)
    prompt_name = config.prompts.selected if round_index == 0 else "optimization"
    template_path = (
        config.prompts.get_generation_template()
        if round_index == 0
        else config.prompts.get_optimization_template()
    )
    optimization_mode = (
        "generation"
        if round_index == 0
        else config.prompts.optimization_context_mode
    )
    effective_query_file = paths.main_root / "effective_query.txt"
    log_round(
        record,
        round_index,
        f"prompt_name={prompt_name} | template={template_path} | mode={optimization_mode}",
    )

    if round_index == 0:
        prepare_workspace_root(paths.main_root, preserve_project=False)
        prepare_empty_project_dir(paths.main_project_dir)
        log_round(record, round_index, f"已准备首轮 main 项目目录: {paths.main_project_dir}")
    else:
        if previous_project_dir is None:
            raise RuntimeError("Cannot run optimization without a previous project directory.")
        prepare_workspace_root(paths.main_root, preserve_project=True)
        if not directory_has_entries(paths.main_project_dir):
            clone_project_dir(previous_project_dir, paths.main_project_dir)
            log_round(
                record,
                round_index,
                f"main 项目目录为空，已从上一轮快照恢复: {previous_project_dir} -> {paths.main_project_dir}",
            )
        log_round(
            record,
            round_index,
            "当前轮次将在 main 目录中继续优化；"
            f"workspace={paths.main_project_dir} | previous_snapshot={previous_project_dir}",
        )

    effective_query = record.query
    analysis_prompt_text: str | None = None
    execution_prompt_text = ""
    collected_messages: list[dict[str, Any]] = []
    step_summaries: list[dict[str, Any]] = []
    last_final_text = ""

    try:
        if round_index > 0:
            if effective_query_file.exists():
                effective_query_file.unlink()
            analysis_prompt_text = build_query_analysis_prompt(
                config=config,
                record=record,
                round_index=round_index,
                previous_verifier=previous_verifier,
                effective_query_file=effective_query_file,
            )
            log_round(
                record,
                round_index,
                "开始分析历史结果，并直接写入本轮 effective_query.txt；"
                f"将从主 session fork "
                f"{'持久复用' if config.agent.fork_session.reuse else '一次性'}"
                f" 分支 | parent_session_id={agent_session.session_id}",
            )
            analysis_output = await agent_session.run_prompt_in_fork(
                prompt=analysis_prompt_text,
                stage="query_analysis",
            )
            token_usage_records.extend(
                build_token_usage_records(
                    record=record,
                    round_index=round_index,
                    messages=analysis_output["messages"],
                ),
            )
            collected_messages.extend(analysis_output["messages"])
            last_final_text = analysis_output["final_text"]
            effective_query = load_effective_query_from_file(effective_query_file)
            step_summaries.append(
                {
                    "stage": "query_analysis",
                    "message_count": analysis_output["message_count"],
                    "session_query_index": analysis_output["session_query_index"],
                    "session_id": analysis_output.get("session_id"),
                    "resume_session_id": analysis_output.get("resume_session_id"),
                    "fork_session": analysis_output.get("fork_session", False),
                    "effective_query_file": str(effective_query_file),
                    "final_text": analysis_output["final_text"],
                    "effective_query": effective_query,
                }
            )
            log_round(record, round_index, f"已生成本轮改进 query: {effective_query}")
        else:
            step_summaries.append(
                {
                    "stage": "query_analysis",
                    "status": "skipped",
                    "effective_query": effective_query,
                }
            )
            log_round(
                record,
                round_index,
                f"首轮直接使用原始 query 作为 effective_query: {preview_text(effective_query)}",
            )

        context = build_prompt_context(
            record=record,
            config=config,
            round_index=round_index,
            project_dir=paths.main_project_dir,
            history=history,
            previous_verifier=previous_verifier,
            effective_query=effective_query,
        )
        execution_prompt_text = render_prompt(load_prompt_template(template_path), context)
        paths.main_prompt_file.write_text(
            build_prompt_bundle(
                effective_query=effective_query,
                execution_prompt=execution_prompt_text,
                analysis_prompt=analysis_prompt_text,
            ),
            encoding="utf-8",
        )
        effective_query_file.write_text(effective_query, encoding="utf-8")
        log_round(
            record,
            round_index,
            f"已生成 prompt 并写入 main 目录: {paths.main_prompt_file} | effective_query_file={effective_query_file}",
        )

        log_round(record, round_index, "开始调用 claude_agent_sdk 执行当前轮")
        execution_output = await agent_session.run_prompt(
            prompt=execution_prompt_text,
            stage="implementation",
        )
        token_usage_records.extend(
            build_token_usage_records(
                record=record,
                round_index=round_index,
                messages=execution_output["messages"],
            ),
        )
        collected_messages.extend(execution_output["messages"])
        last_final_text = execution_output["final_text"]
        step_summaries.append(
            {
                "stage": "implementation",
                "message_count": execution_output["message_count"],
                "session_query_index": execution_output["session_query_index"],
                "session_id": execution_output.get("session_id"),
                "resume_session_id": execution_output.get("resume_session_id"),
                "fork_session": execution_output.get("fork_session", False),
                "final_text": execution_output["final_text"],
            }
        )
        log_round(
            record,
            round_index,
            "Agent 调用完成，"
            f"累计收到 {len(collected_messages)} 条消息 | "
            f"final_text={preview_text(last_final_text)}",
        )
    except Exception as exc:
        error_message = str(exc)
        logger.exception(
            "data_id=%s | round=%s | Agent 调用失败",
            record.data_id,
            round_name(round_index),
        )
        if not paths.main_prompt_file.exists():
            paths.main_prompt_file.write_text(
                build_prompt_bundle(
                    effective_query=effective_query,
                    execution_prompt=execution_prompt_text,
                    analysis_prompt=analysis_prompt_text,
                ),
                encoding="utf-8",
            )
        effective_query_file.write_text(effective_query, encoding="utf-8")
        write_json(
            paths.main_messages_file,
            build_messages_payload(
                status="error",
                effective_query=effective_query,
                steps=step_summaries,
                messages=collected_messages,
                error=error_message,
            ),
        )

        parsed_record = build_parsed_record(
            record=record,
            round_index=round_index,
            prompt_name=prompt_name,
            project_dir=paths.project_dir,
            status="error",
            project_file_count=0,
            verifier_record=None,
            effective_query=effective_query,
            error=error_message,
        )
        snapshot_workspace_root(paths.main_root, paths.round_root)
        log_round(record, round_index, f"已将 main 目录错误现场快照到: {paths.round_root}")

        return {
            "status": "error",
            "round_index": round_index,
            "project_dir": paths.project_dir,
            "paths": paths,
            "project_record": None,
            "parsed": parsed_record,
            "verifier": None,
            "history_entry": build_history_entry(
                round_index=round_index,
                paths=paths,
                project_record=None,
                parsed=parsed_record,
                verifier=None,
            ),
        }

    write_json(
        paths.main_messages_file,
        build_messages_payload(
            status="success",
            effective_query=effective_query,
            steps=step_summaries,
            messages=collected_messages,
        ),
    )
    log_round(record, round_index, f"消息轨迹已写入 main 目录: {paths.main_messages_file}")

    project_json = collect_project_files(paths.main_project_dir)
    project_file_count = len(project_json["files"])
    log_round(record, round_index, f"已收集项目文件: {project_file_count} 个")
    if not project_json["files"]:
        error_message = f"No files written under {paths.main_project_dir}"
        logger.error(
            "data_id=%s | round=%s | 未检测到项目文件输出: %s",
            record.data_id,
            round_name(round_index),
            paths.main_project_dir,
        )

        parsed_record = build_parsed_record(
            record=record,
            round_index=round_index,
            prompt_name=prompt_name,
            status="error",
            project_dir=paths.project_dir,
            project_file_count=0,
            verifier_record=None,
            effective_query=effective_query,
            error=error_message,
        )
        snapshot_workspace_root(paths.main_root, paths.round_root)
        log_round(record, round_index, f"已将 main 目录快照到: {paths.round_root}")

        return {
            "status": "error",
            "round_index": round_index,
            "project_dir": paths.project_dir,
            "paths": paths,
            "project_record": None,
            "parsed": parsed_record,
            "verifier": None,
            "history_entry": build_history_entry(
                round_index=round_index,
                paths=paths,
                project_record=None,
                parsed=parsed_record,
                verifier=None,
            ),
        }

    project_record = build_project_record(
        record=record,
        round_index=round_index,
        prompt_name=prompt_name,
        project_dir=paths.project_dir,
        files=project_json["files"],
        effective_query=effective_query,
    )
    verifier_record = None
    if config.verifier.enabled:
        log_round(record, round_index, "开始调用 verifier")
        verifier_record = await run_verifier_requests_async(
            record=record,
            round_index=round_index,
            files=project_json["files"],
            verifier_config=config.verifier,
        )
        write_json(paths.main_verifier_file, verifier_record)
        log_round(
            record,
            round_index,
            f"verifier 完成，结果已写入 main 目录: {paths.main_verifier_file}",
        )

    parsed_record = build_parsed_record(
        record=record,
        round_index=round_index,
        prompt_name=prompt_name,
        status="success",
        project_dir=paths.project_dir,
        project_file_count=project_file_count,
        verifier_record=verifier_record,
        effective_query=effective_query,
    )
    snapshot_workspace_root(paths.main_root, paths.round_root)
    log_round(record, round_index, f"已将 main 目录快照到: {paths.round_root}")

    await writers["project_files"].append(project_record)
    if verifier_record is not None:
        await writers["verifier"].append(verifier_record)

    log_round(record, round_index, "轮次完成")

    return {
        "status": "success",
        "round_index": round_index,
        "project_dir": paths.project_dir,
        "paths": paths,
        "project_record": project_record,
        "parsed": parsed_record,
        "verifier": verifier_record,
        "history_entry": build_history_entry(
            round_index=round_index,
            paths=paths,
            project_record=project_record,
            parsed=parsed_record,
            verifier=verifier_record,
        ),
    }


async def process_record(
    *,
    record: InputRecord,
    config: AppConfig,
    writers: dict[str, AsyncJsonlWriter],
    project_index: dict[tuple[str, int], dict[str, Any]],
    verifier_index: dict[tuple[str, int], dict[str, Any]],
    row_index: int,
    row_total: int,
) -> dict[str, Any]:
    """按顺序执行单个样本的全部轮次，并处理断点续跑。"""
    logger.info(
        "[%s/%s] data_id=%s | 开始处理",
        row_index,
        row_total,
        record.data_id,
    )

    total_requested_rounds = total_rounds(config)
    history: list[dict[str, Any]] = []
    round_results: list[dict[str, Any]] = []
    previous_project_dir: Path | None = None
    previous_verifier: dict[str, Any] | None = None
    allow_reuse_from_history = True
    record_workspace_dir = config.project.output_dir / record.data_id
    record_workspace_dir.mkdir(parents=True, exist_ok=True)
    token_usage_records: list[dict[str, Any]] = []
    agent_session: ClaudeAgentSession | None = None
    bootstrap_attempted = False

    try:
        for round_index in range(total_requested_rounds):
            key = (record.data_id, round_index)
            existing_project = project_index.get(key)
            existing_verifier = verifier_index.get(key)
            paths = build_round_paths(config.project.output_dir, record.data_id, round_index)
            can_reuse, reuse_reason = can_reuse_existing_round(
                config=config,
                existing_project=existing_project,
                existing_verifier=existing_verifier,
            )

            if (
                allow_reuse_from_history
                and config.project.resume
                and can_reuse
            ):
                if not paths.project_dir.exists():
                    materialize_project_files(
                        paths.project_dir,
                        existing_project.get("files", {}),
                    )
                    logger.info(
                        "[%s/%s] data_id=%s | round=%s | 已从 project_files.jsonl 恢复项目目录",
                        row_index,
                        row_total,
                        record.data_id,
                        round_name(round_index),
                    )

                previous_project_dir = paths.project_dir
                previous_verifier = existing_verifier
                prepare_workspace_root(paths.main_root, preserve_project=False)
                clone_project_dir(paths.project_dir, paths.main_project_dir)
                if existing_verifier:
                    write_json(paths.main_verifier_file, existing_verifier)
                logger.info(
                    "[%s/%s] data_id=%s | round=%s | 已从历史项目和 verifier 结果同步 main 目录: %s",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                    paths.main_root,
                )
                existing_parsed = build_parsed_record(
                    record=record,
                    round_index=round_index,
                    prompt_name=str(
                        existing_project.get(
                            "prompt_name",
                            config.prompts.selected if round_index == 0 else "optimization",
                        )
                    ),
                    status="success",
                    project_dir=paths.project_dir,
                    project_file_count=len(existing_project.get("files", {}) or {}),
                    verifier_record=existing_verifier,
                    effective_query=str(
                        existing_project.get("effective_query")
                        or existing_project.get("query")
                        or record.query
                    ),
                )
                history.append(
                    build_history_entry(
                        round_index=round_index,
                        paths=paths,
                        project_record=existing_project,
                        parsed=existing_parsed,
                        verifier=existing_verifier,
                    )
                )
                round_results.append(
                    {
                        "round_index": round_index,
                        "status": "skipped",
                        "project_dir": str(paths.project_dir),
                    }
                )
                logger.info(
                    "[%s/%s] data_id=%s | round=%s | 已跳过，复用历史成功结果: %s",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                    reuse_reason,
                )
                continue
            elif config.project.resume and allow_reuse_from_history:
                logger.info(
                    "[%s/%s] data_id=%s | round=%s | 不复用历史结果，原因: %s",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                    reuse_reason,
                )
            elif config.project.resume and not allow_reuse_from_history:
                logger.info(
                    "[%s/%s] data_id=%s | round=%s | 上游轮次已重跑，本轮强制重跑以保持上下文一致",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                )

            if agent_session is None:
                logger.info(
                    "[%s/%s] data_id=%s | 创建 ClaudeSDKClient session | cwd=%s",
                    row_index,
                    row_total,
                    record.data_id,
                    record_workspace_dir,
                )
                agent_session = create_agent_session(
                    agent_config=config.agent,
                    cwd=record_workspace_dir,
                )

            if history and allow_reuse_from_history and not bootstrap_attempted:
                bootstrap_attempted = True
                logger.info(
                    "[%s/%s] data_id=%s | 开始向新 session 注入历史上下文 | history_round_count=%s",
                    row_index,
                    row_total,
                    record.data_id,
                    len(history),
                )
                try:
                    bootstrap_output = await agent_session.run_prompt(
                        prompt=build_session_bootstrap_prompt(
                            config,
                            record,
                            history,
                        ),
                        stage="history_bootstrap",
                    )
                    token_usage_records.extend(
                        build_token_usage_records(
                            record=record,
                            round_index=round_index,
                            messages=bootstrap_output["messages"],
                        ),
                    )
                    logger.info(
                        "[%s/%s] data_id=%s | 已向当前 session 注入 %s 条历史轮次上下文 | messages=%s",
                        row_index,
                        row_total,
                        record.data_id,
                        len(history),
                        bootstrap_output["message_count"],
                    )
                except Exception:
                    logger.exception(
                        "[%s/%s] data_id=%s | 历史上下文注入失败，将仅依赖磁盘历史产物继续执行",
                        row_index,
                        row_total,
                        record.data_id,
                    )

            result = await process_round(
                record=record,
                config=config,
                writers=writers,
                token_usage_records=token_usage_records,
                round_index=round_index,
                previous_project_dir=previous_project_dir,
                history=history,
                previous_verifier=previous_verifier,
                agent_session=agent_session,
            )
            allow_reuse_from_history = False
            round_results.append(
                {
                    "round_index": round_index,
                    "status": result["status"],
                    "project_dir": str(result["project_dir"]),
                }
            )
            history.append(result["history_entry"])

            if result["status"] != "success":
                logger.error(
                    "[%s/%s] data_id=%s | round=%s | 执行失败",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                )
                break

            if not config.agent.manual_compact:
                logger.info(
                    "[%s/%s] data_id=%s | round=%s | manual_compact=false，跳过手动 compact",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                )
            elif agent_session.is_open:
                try:
                    compact_output = await agent_session.compact(
                        stage=f"post_{round_name(round_index)}_compact"
                    )
                    token_usage_records.extend(
                        build_token_usage_records(
                            record=record,
                            round_index=round_index,
                            messages=compact_output["messages"],
                        ),
                    )
                    logger.info(
                        "[%s/%s] data_id=%s | round=%s | 手动 compact 完成 | messages=%s | metadata=%s",
                        row_index,
                        row_total,
                        record.data_id,
                        round_name(round_index),
                        compact_output["message_count"],
                        compact_output.get("compact_metadata"),
                    )
                except Exception:
                    logger.exception(
                        "[%s/%s] data_id=%s | round=%s | 手动 compact 失败，将继续处理后续轮次",
                        row_index,
                        row_total,
                        record.data_id,
                        round_name(round_index),
                    )
            else:
                logger.warning(
                    "[%s/%s] data_id=%s | round=%s | session 未打开，跳过手动 compact",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                )

            previous_project_dir = result["project_dir"]
            previous_verifier = result["verifier"]

            if (
                result["verifier"]
                and result["verifier"].get("error")
                and not config.optimization.continue_when_verifier_fails
            ):
                logger.warning(
                    "[%s/%s] data_id=%s | round=%s | verifier 失败且配置要求停止后续轮次",
                    row_index,
                    row_total,
                    record.data_id,
                    round_name(round_index),
                )
                break

            logger.info(
                "[%s/%s] data_id=%s | round=%s | 执行成功",
                row_index,
                row_total,
                record.data_id,
                round_name(round_index),
            )
    finally:
        if agent_session is not None:
            logger.info(
                "[%s/%s] data_id=%s | 关闭 ClaudeSDKClient session",
                row_index,
                row_total,
                record.data_id,
            )
            await agent_session.aclose()

    await writers["token_usage"].append(
        build_token_usage_summary(
            record=record,
            output_dir=config.project.output_dir,
            usage_records=token_usage_records,
        )
    )

    return {
        "data_id": record.data_id,
        "query": record.query,
        "rounds": round_results,
    }


async def process_record_with_semaphore(
    semaphore: asyncio.Semaphore,
    **kwargs: Any,
) -> dict[str, Any]:
    async with semaphore:
        return await process_record(**kwargs)


async def async_main() -> None:
    """加载配置、并发执行全部样本。"""
    args = parse_args()
    config = load_config(Path(args.config))
    apply_cli_overrides(config, args)

    artifact_layout = build_artifact_layout(config.project.output_dir)
    writers = {
        "project_files": AsyncJsonlWriter(artifact_layout.project_files_jsonl),
        "verifier": AsyncJsonlWriter(artifact_layout.verifier_result_jsonl),
        "token_usage": AsyncJsonlWriter(artifact_layout.token_usage_jsonl),
    }

    rows = load_input_records(config.input)
    project_index = index_round_records(artifact_layout.project_files_jsonl)
    verifier_index = index_round_records(artifact_layout.verifier_result_jsonl)

    logger.info("已加载输入样本: %s | input=%s", len(rows), config.input.path)
    logger.info("输出目录: %s", config.project.output_dir)
    logger.info("每个样本总轮次: %s", total_rounds(config))
    logger.info("resume: %s", config.project.resume)
    logger.info("verifier.enabled: %s", config.verifier.enabled)
    logger.info("verifier.api_url: %s", config.verifier.api_url)
    logger.info("project_files.jsonl: %s", artifact_layout.project_files_jsonl)
    logger.info("verifier_result.jsonl: %s", artifact_layout.verifier_result_jsonl)
    logger.info("token_usage.jsonl: %s", artifact_layout.token_usage_jsonl)

    semaphore = asyncio.Semaphore(config.project.max_concurrent_tasks)
    tasks = [
        process_record_with_semaphore(
            semaphore,
            record=row,
            config=config,
            writers=writers,
            project_index=project_index,
            verifier_index=verifier_index,
            row_index=index + 1,
            row_total=len(rows),
        )
        for index, row in enumerate(rows)
    ]
    await asyncio.gather(*tasks) if tasks else []

    logger.info("Run finished.")


def main() -> None:
    setup_logging()
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
