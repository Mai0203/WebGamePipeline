"""调用本地 verifier 服务并整理返回结果。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover - 可选运行时依赖
    requests = None


EMPTY_RESPONSE_MARKER = "returned empty response"
EVALUATION_KEYS = ("installation", "running", "aesthetics", "functional")
logger = logging.getLogger("cc_project.verifier")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_local_api_url(api_url: str) -> bool:
    """判断 verifier 是否是本地地址，便于禁用系统代理。"""
    hostname = (urlparse(api_url).hostname or "").strip().lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def should_retry_for_empty_response(result: dict[str, Any]) -> bool:
    """某些 verifier 会返回空评分文案，这类结果允许重试。"""
    aesthetics_reason = str(result.get("aesthetics_reason") or "")
    functional_reason = str(result.get("functional_reason") or "")
    marker = EMPTY_RESPONSE_MARKER.lower()
    return marker in aesthetics_reason.lower() or marker in functional_reason.lower()


def populate_scores_from_response(
    *,
    response_json: dict[str, Any],
    result: dict[str, Any],
) -> str:
    """兼容旧版 evaluations 嵌套结构与新版平铺 score/reason 结构。"""
    evaluations = response_json.get("evaluations")
    if isinstance(evaluations, dict) and evaluations:
        for key in EVALUATION_KEYS:
            item = evaluations.get(key, {}) or {}
            if isinstance(item, dict):
                result[f"{key}_score"] = item.get("score")
                result[f"{key}_reason"] = item.get("reason")
        return "evaluations"

    found_flat_fields = False
    for key in EVALUATION_KEYS:
        score_key = f"{key}_score"
        reason_key = f"{key}_reason"
        if score_key in response_json or reason_key in response_json:
            found_flat_fields = True
        result[score_key] = response_json.get(score_key)
        result[reason_key] = response_json.get(reason_key)

    return "flat" if found_flat_fields else "unknown"


def build_payload(
    *,
    data_id: str,
    query: str,
    files: dict[str, str],
    language: str,
    verifier_config,
) -> dict[str, Any]:
    """把当前项目文件和元信息组装成 verifier 请求体。"""
    if not files:
        raise ValueError("project files are empty")
    if not query.strip():
        raise ValueError("query is empty")

    return {
        "code": files,
        "query": query,
        "language": language,
        "project_id": data_id,
        "agent_type": verifier_config.agent_type,
        "keep_existing_project": verifier_config.keep_existing_project,
        "evaluator_config": verifier_config.evaluator_config,
    }


def init_result(
    *,
    data_id: str,
    query: str,
    round_index: int,
    language: str,
    verifier_config,
) -> dict[str, Any]:
    """初始化 verifier 结果结构，保证输出字段稳定。"""
    return {
        "data_id": data_id,
        "query": query,
        "round_index": round_index,
        "round_name": "generation" if round_index == 0 else f"optimization_{round_index}",
        "language": language,
        "agent_type": verifier_config.agent_type,
        "project_path": None,
        "response_format": None,
        "installation_score": None,
        "installation_reason": None,
        "running_score": None,
        "running_reason": None,
        "aesthetics_score": None,
        "aesthetics_reason": None,
        "functional_score": None,
        "functional_reason": None,
        "raw_response": None,
        "error": None,
        "attempts": 0,
        "timestamp": utcnow_iso(),
    }


def call_verifier_with_retry(
    *,
    api_url: str,
    payload: dict[str, Any],
    verifier_config,
    result: dict[str, Any],
) -> dict[str, Any]:
    """请求 verifier，并按配置处理超时、异常和空响应重试。"""
    if requests is None:
        raise RuntimeError(
            "requests is not available in the current environment. "
            "Install it with `pip install requests` before enabling verifier."
        )

    session = requests.Session()
    request_kwargs: dict[str, Any] = {}
    use_direct_local_connection = is_local_api_url(api_url)
    if use_direct_local_connection:
        # 本地服务通常不需要走代理，否则可能被错误转发。
        session.trust_env = False
        request_kwargs["proxies"] = {"http": "", "https": ""}
        logger.info("verifier | 使用本地直连模式: api_url=%s", api_url)
    else:
        logger.info("verifier | 使用默认网络环境: api_url=%s", api_url)

    total_attempts = verifier_config.max_retries + 1
    last_error = None

    for attempt in range(1, total_attempts + 1):
        result["attempts"] = attempt
        try:
            logger.info(
                "verifier | project_id=%s | attempt=%s/%s | 发起请求",
                payload.get("project_id"),
                attempt,
                total_attempts,
            )
            response = session.post(
                api_url,
                json=payload,
                timeout=verifier_config.timeout,
                **request_kwargs,
            )
            logger.info(
                "verifier | project_id=%s | attempt=%s/%s | 收到响应 status=%s",
                payload.get("project_id"),
                attempt,
                total_attempts,
                response.status_code,
            )
            response.raise_for_status()
            response_json = response.json()

            result["raw_response"] = response_json
            result["project_path"] = response_json.get("project_path")
            result["error"] = response_json.get("error")
            result["response_format"] = populate_scores_from_response(
                response_json=response_json,
                result=result,
            )

            if should_retry_for_empty_response(result):
                last_error = "Verifier returned empty response in evaluator reason."
                logger.warning(
                    "verifier | project_id=%s | attempt=%s/%s | 命中空响应重试条件",
                    payload.get("project_id"),
                    attempt,
                    total_attempts,
                )
                if attempt < total_attempts:
                    time.sleep(verifier_config.retry_sleep)
                    continue

            return result
        except requests.exceptions.ProxyError as exc:
            last_error = f"ProxyError: {exc}"
            logger.warning(
                "verifier | project_id=%s | attempt=%s/%s | 代理错误: %s",
                payload.get("project_id"),
                attempt,
                total_attempts,
                exc,
            )
            if attempt < total_attempts:
                time.sleep(verifier_config.retry_sleep)
        except requests.exceptions.ConnectionError as exc:
            last_error = f"ConnectionError: {exc}"
            logger.warning(
                "verifier | project_id=%s | attempt=%s/%s | 连接错误: %s",
                payload.get("project_id"),
                attempt,
                total_attempts,
                exc,
            )
            if attempt < total_attempts:
                time.sleep(verifier_config.retry_sleep)
        except requests.exceptions.Timeout as exc:
            last_error = f"Timeout: {exc}"
            logger.warning(
                "verifier | project_id=%s | attempt=%s/%s | 请求超时: %s",
                payload.get("project_id"),
                attempt,
                total_attempts,
                exc,
            )
            if attempt < total_attempts:
                time.sleep(verifier_config.retry_sleep)
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            response_text = ""
            if exc.response is not None:
                try:
                    response_text = exc.response.text[:500]
                except Exception:
                    response_text = "<unavailable>"
            last_error = (
                f"HTTPError(status={status_code}): {response_text or exc}"
            )
            logger.warning(
                "verifier | project_id=%s | attempt=%s/%s | HTTP 错误 status=%s body=%s",
                payload.get("project_id"),
                attempt,
                total_attempts,
                status_code,
                response_text,
            )
            if attempt < total_attempts:
                time.sleep(verifier_config.retry_sleep)
        except ValueError as exc:
            last_error = f"JSONDecodeError: {exc}"
            logger.warning(
                "verifier | project_id=%s | attempt=%s/%s | 响应 JSON 解析失败: %s",
                payload.get("project_id"),
                attempt,
                total_attempts,
                exc,
            )
            if attempt < total_attempts:
                time.sleep(verifier_config.retry_sleep)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "verifier | project_id=%s | attempt=%s/%s | 未分类异常: %s",
                payload.get("project_id"),
                attempt,
                total_attempts,
                exc,
            )
            if attempt < total_attempts:
                time.sleep(verifier_config.retry_sleep)

    result["error"] = f"Failed after {total_attempts} attempts. Error: {last_error}"
    logger.error(
        "verifier | project_id=%s | 最终失败: %s",
        payload.get("project_id"),
        result["error"],
    )
    return result


def run_verifier(
    *,
    record,
    round_index: int,
    files: dict[str, str],
    verifier_config,
) -> dict[str, Any]:
    """执行单轮 verifier 校验。"""
    language = (
        record.payload.get(verifier_config.language_field) or verifier_config.default_language
    )
    payload = build_payload(
        data_id=record.data_id,
        query=record.query,
        files=files,
        language=str(language),
        verifier_config=verifier_config,
    )
    result = init_result(
        data_id=record.data_id,
        query=record.query,
        round_index=round_index,
        language=str(language),
        verifier_config=verifier_config,
    )
    return call_verifier_with_retry(
        api_url=verifier_config.api_url,
        payload=payload,
        verifier_config=verifier_config,
        result=result,
    )


async def run_verifier_async(
    *,
    record,
    round_index: int,
    files: dict[str, str],
    verifier_config,
) -> dict[str, Any]:
    """在线程中执行 verifier，避免阻塞事件循环，允许样本间并行请求。"""
    return await asyncio.to_thread(
        run_verifier,
        record=record,
        round_index=round_index,
        files=files,
        verifier_config=verifier_config,
    )


def summarize_single_verifier_result(result: dict[str, Any], *, label: str = "") -> str:
    """把 verifier 结果压缩成适合放进下一轮 prompt 的文本。"""
    if result.get("error"):
        return f"{label}Verifier 执行失败：{result['error']}"

    return "\n".join(
        [
            f"{label}Verifier 评分概览：",
            f"- installation: score={result.get('installation_score')}, reason={result.get('installation_reason')}",
            f"- running: score={result.get('running_score')}, reason={result.get('running_reason')}",
            f"- aesthetics: score={result.get('aesthetics_score')}, reason={result.get('aesthetics_reason')}",
            f"- functional: score={result.get('functional_score')}, reason={result.get('functional_reason')}",
        ]
    )


def summarize_verifier_result(result: dict[str, Any] | None) -> str:
    """把 verifier 结果压缩成适合放进下一轮 prompt 的文本。"""
    if result is None:
        return "尚无 verifier 反馈。"

    verifier_results = result.get("verifier_results")
    if isinstance(verifier_results, list):
        lines = [
            "Verifier 多次请求综合反馈：",
            f"- request_count={result.get('request_count')}",
            f"- successful_request_count={result.get('successful_request_count')}",
            f"- failed_request_count={result.get('failed_request_count')}",
            "- aggregate_scores: "
            f"installation={result.get('installation_score')}, "
            f"running={result.get('running_score')}, "
            f"aesthetics={result.get('aesthetics_score')}, "
            f"functional={result.get('functional_score')}",
        ]
        if result.get("error"):
            lines.append(f"- aggregate_error={result.get('error')}")
        request_errors = result.get("request_errors")
        if isinstance(request_errors, list) and request_errors:
            lines.append(f"- request_errors={request_errors}")
        for index, item in enumerate(verifier_results, start=1):
            if isinstance(item, dict):
                lines.append("")
                lines.append(summarize_single_verifier_result(item, label=f"第 {index} 次 - "))
        return "\n".join(lines)

    return summarize_single_verifier_result(result)
