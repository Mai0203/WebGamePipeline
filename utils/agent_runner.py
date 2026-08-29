"""封装 `claude_agent_sdk` 的会话式调用过程。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
import logging
import os
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
except ImportError:  # pragma: no cover - 可选运行时依赖
    ClaudeAgentOptions = None
    ClaudeSDKClient = None


logger = logging.getLogger(__name__)
INITIALIZE_TIMEOUT_ENV = "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"


def ensure_sdk_available() -> None:
    """在真正调用 Agent 前确认运行依赖已经就绪。"""
    if ClaudeAgentOptions is None or ClaudeSDKClient is None:
        raise RuntimeError(
            "claude_agent_sdk is not available in the current environment. "
            "Please install it before running generation."
        )


def make_json_safe(value: Any) -> Any:
    """把任意对象递归转换成可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]
    if is_dataclass(value):
        return make_json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        try:
            return make_json_safe(value.model_dump(mode="json"))
        except TypeError:
            try:
                return make_json_safe(value.model_dump())
            except Exception:
                pass
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return {str(key): make_json_safe(item) for key, item in vars(value).items()}
        except Exception:
            pass
    return str(value)


def serialize_message(message: Any) -> dict[str, Any]:
    """把 SDK 返回的消息对象尽量序列化成稳定的 JSON 结构。"""
    record: dict[str, Any] = {
        "type": type(message).__name__,
        "text": str(message),
    }

    if hasattr(message, "model_dump"):
        try:
            record["payload"] = make_json_safe(message.model_dump(mode="json"))
        except TypeError:
            try:
                record["payload"] = make_json_safe(message.model_dump())
            except Exception:
                pass
        except Exception:
            pass
    elif hasattr(message, "__dict__"):
        try:
            record["payload"] = make_json_safe(dict(message.__dict__))
        except Exception:
            pass

    return record


def extract_final_text(messages: list[dict[str, Any]]) -> str:
    """优先提取 ResultMessage 的结果文本，兜底取最后一条消息文本。"""
    for message in reversed(messages):
        payload = message.get("payload") or {}
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()

    for message in reversed(messages):
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()

    return ""


def preview_text(value: str, limit: int = 160) -> str:
    """压缩文本，避免日志过长。"""
    normalized = " ".join((value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def is_initialize_timeout_error(exc: Exception) -> bool:
    """识别 SDK 初始化握手超时，便于做定向重试。"""
    return "Control request timeout: initialize" in str(exc)


def configure_initialize_timeout(timeout_ms: int) -> int:
    """把 SDK 读取的初始化超时写入当前进程环境。"""
    normalized_timeout_ms = max(int(timeout_ms), 60000)
    timeout_text = str(normalized_timeout_ms)
    current = os.environ.get(INITIALIZE_TIMEOUT_ENV)
    if current != timeout_text:
        os.environ[INITIALIZE_TIMEOUT_ENV] = timeout_text
        logger.info("agent | 设置 %s=%s", INITIALIZE_TIMEOUT_ENV, timeout_text)
    return normalized_timeout_ms


def normalize_optional_text(value: Any) -> str | None:
    """把可选配置规整为非空字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class ClaudeAgentSession:
    """对 ClaudeSDKClient 的轻量包装，支持单 session 多次 query。"""

    def __init__(
        self,
        *,
        agent_config: Any,
        cwd: Path | str,
        resume_session_id: str | None = None,
        fork_session: bool = False,
        model: str | None = None,
        base_url: str | None = None,
        thinking: Any = None,
        session_env: dict[str, str] | None = None,
        session_label: str | None = None,
    ) -> None:
        ensure_sdk_available()
        self.agent_config = agent_config
        self.cwd = Path(cwd)
        self.resume_session_id = resume_session_id
        self.fork_session = fork_session
        self.model = normalize_optional_text(model)
        self.base_url = normalize_optional_text(base_url)
        self.thinking = make_json_safe(thinking) if thinking is not None else None
        self.session_env = {
            str(key): str(value) for key, value in (session_env or {}).items()
        }
        self.session_label = session_label or ("fork" if fork_session else "main")
        self.initialize_timeout_ms = configure_initialize_timeout(
            agent_config.initialize_timeout_ms
        )
        self.total_attempts = max(int(agent_config.initialize_max_retries), 0) + 1
        self.retry_sleep = max(float(agent_config.initialize_retry_sleep), 0.0)
        self._client: ClaudeSDKClient | None = None
        self._opened = False
        self._query_counter = 0
        self._session_id: str | None = None
        self._managed_forks: dict[str, ClaudeAgentSession] = {}

    def _resolve_model(self) -> str | None:
        if self.model:
            return self.model

        agent_model = normalize_optional_text(getattr(self.agent_config, "model", None))
        if agent_model:
            return agent_model

        return normalize_optional_text(
            self.session_env.get("ANTHROPIC_MODEL")
            or self.agent_config.env.get("ANTHROPIC_MODEL")
        )

    def _resolve_base_url(self) -> str | None:
        if self.base_url:
            return self.base_url

        agent_base_url = normalize_optional_text(
            getattr(self.agent_config, "base_url", None)
        )
        if agent_base_url:
            return agent_base_url

        return normalize_optional_text(
            self.session_env.get("ANTHROPIC_BASE_URL")
            or self.agent_config.env.get("ANTHROPIC_BASE_URL")
        )

    def _resolve_thinking(self) -> Any:
        if self.thinking is not None:
            return self.thinking
        return getattr(self.agent_config, "thinking", None)

    def _resolve_cli_thinking(self) -> Any:
        """返回需要传给 SDK/CLI 的 thinking 配置；disabled 对旧 CLI 不下发。"""
        resolved_thinking = self._resolve_thinking()
        if (
            isinstance(resolved_thinking, dict)
            and resolved_thinking.get("type") == "disabled"
        ):
            return None
        return resolved_thinking

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.agent_config.env)
        env.update(self.session_env)

        resolved_model = self._resolve_model()
        if resolved_model:
            env["ANTHROPIC_MODEL"] = resolved_model

        resolved_base_url = self._resolve_base_url()
        if resolved_base_url:
            env["ANTHROPIC_BASE_URL"] = resolved_base_url

        return env

    def _build_options(self) -> ClaudeAgentOptions:
        option_kwargs = {
            "max_turns": self.agent_config.max_turns,
            "cli_path": self.agent_config.cli_path,
            "env": self._build_env(),
            "setting_sources": self.agent_config.setting_sources,
            "cwd": str(self.cwd),
            "permission_mode": self.agent_config.permission_mode,
            "resume": self.resume_session_id,
            "fork_session": self.fork_session,
            "model": self._resolve_model(),
        }
        resolved_thinking = self._resolve_cli_thinking()
        if resolved_thinking is not None:
            option_kwargs["thinking"] = resolved_thinking
        return ClaudeAgentOptions(**option_kwargs)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def parent_session_id(self) -> str | None:
        return self.resume_session_id

    @property
    def is_fork_session(self) -> bool:
        return self.fork_session

    @property
    def is_open(self) -> bool:
        return self._opened and self._client is not None

    def _update_session_id(self, session_id: str | None) -> None:
        normalized_session_id = (session_id or "").strip()
        if not normalized_session_id:
            return
        self._session_id = normalized_session_id

    def create_fork(self, *, session_label: str | None = None) -> "ClaudeAgentSession":
        if not self._session_id:
            raise RuntimeError(
                "Cannot fork session before the parent session has produced a session_id."
            )
        fork_config = self.agent_config.fork_session
        return ClaudeAgentSession(
            agent_config=self.agent_config,
            cwd=self.cwd,
            resume_session_id=self._session_id,
            fork_session=True,
            model=fork_config.model,
            base_url=fork_config.base_url,
            thinking=fork_config.thinking,
            session_env=fork_config.env,
            session_label=session_label,
        )

    def get_or_create_managed_fork(self, *, key: str) -> tuple["ClaudeAgentSession", bool]:
        existing = self._managed_forks.get(key)
        if existing is not None:
            return existing, False

        forked_session = self.create_fork(session_label=key)
        self._managed_forks[key] = forked_session
        return forked_session, True

    async def run_prompt_in_fork(self, *, prompt: str, stage: str) -> dict[str, Any]:
        if self.agent_config.fork_session.reuse:
            forked_session, created = self.get_or_create_managed_fork(key=stage)
            logger.info(
                "agent | stage=%s | %s持久 fork session | parent_session_id=%s | fork_session_id=%s | cwd=%s | fork_model=%s | fork_base_url=%s | fork_thinking=%s",
                stage,
                "创建" if created else "复用",
                self._session_id,
                forked_session.session_id,
                self.cwd,
                forked_session._resolve_model(),
                forked_session._resolve_base_url(),
                forked_session._resolve_thinking(),
            )
            return await forked_session.run_prompt(prompt=prompt, stage=stage)

        forked_session = self.create_fork(session_label=stage)
        logger.info(
            "agent | stage=%s | 创建一次性 fork session | parent_session_id=%s | cwd=%s | fork_model=%s | fork_base_url=%s | fork_thinking=%s",
            stage,
            self._session_id,
            self.cwd,
            forked_session._resolve_model(),
            forked_session._resolve_base_url(),
            forked_session._resolve_thinking(),
        )
        try:
            return await forked_session.run_prompt(prompt=prompt, stage=stage)
        finally:
            await forked_session.aclose()
            logger.info(
                "agent | stage=%s | 已关闭一次性 fork session | parent_session_id=%s | fork_session_id=%s",
                stage,
                self._session_id,
                forked_session.session_id,
            )

    async def start(self) -> "ClaudeAgentSession":
        if self._opened:
            logger.info(
                "agent | 复用已有 session | session_label=%s | cwd=%s",
                self.session_label,
                self.cwd,
            )
            return self
        self._client = ClaudeSDKClient(options=self._build_options())
        try:
            await self._client.__aenter__()
            self._opened = True
        except Exception:
            self._client = None
            self._opened = False
            raise
        logger.info(
            "agent | 已创建 session | session_label=%s | cwd=%s | cli_path=%s | max_turns=%s | resume_session_id=%s | fork_session=%s | model=%s | base_url=%s | thinking=%s",
            self.session_label,
            self.cwd,
            self.agent_config.cli_path,
            self.agent_config.max_turns,
            self.resume_session_id,
            self.fork_session,
            self._resolve_model(),
            self._resolve_base_url(),
            self._resolve_thinking(),
        )
        return self

    async def aclose(self) -> None:
        if self._managed_forks:
            managed_forks = list(self._managed_forks.items())
            self._managed_forks.clear()
            for fork_key, forked_session in managed_forks:
                try:
                    await forked_session.aclose()
                finally:
                    logger.info(
                        "agent | 已关闭持久 fork session | parent_session_id=%s | fork_key=%s | fork_session_id=%s",
                        self._session_id,
                        fork_key,
                        forked_session.session_id,
                    )

        if not self._opened or self._client is None:
            return
        try:
            await self._client.__aexit__(None, None, None)
        finally:
            self._client = None
            self._opened = False
            logger.info(
                "agent | 已关闭 session | session_label=%s | cwd=%s",
                self.session_label,
                self.cwd,
            )

    async def restart(self) -> None:
        await self.aclose()
        await self.start()

    async def run_prompt(self, *, prompt: str, stage: str) -> dict[str, Any]:
        """在当前 session 中执行一次查询，并返回标准化消息结果。"""
        for attempt in range(1, self.total_attempts + 1):
            try:
                if not self._opened or self._client is None:
                    await self.start()

                logger.info(
                    "agent | stage=%s | session_query=%s | attempt=%s/%s | cwd=%s | initialize_timeout_ms=%s | prompt_chars=%s | resume_session_id=%s | fork_session=%s",
                    stage,
                    self._query_counter + 1,
                    attempt,
                    self.total_attempts,
                    self.cwd,
                    self.initialize_timeout_ms,
                    len(prompt),
                    self.resume_session_id,
                    self.fork_session,
                )

                await self._client.query(prompt)
                messages: list[dict[str, Any]] = []
                async for message in self._client.receive_response():
                    serialized = serialize_message(message)
                    session_id = extract_message_session_id(serialized)
                    self._update_session_id(session_id)
                    serialized["stage"] = stage
                    serialized["session_query_index"] = self._query_counter + 1
                    serialized["session_id"] = self._session_id
                    serialized["resume_session_id"] = self.resume_session_id
                    serialized["fork_session"] = self.fork_session
                    messages.append(serialized)
                    print(message)

                self._query_counter += 1
                final_text = extract_final_text(messages)
                logger.info(
                    "agent | stage=%s | session_query=%s | 完成 | message_count=%s | final_text=%s | session_id=%s",
                    stage,
                    self._query_counter,
                    len(messages),
                    preview_text(final_text),
                    self._session_id,
                )
                return {
                    "messages": messages,
                    "message_texts": [message["text"] for message in messages],
                    "message_count": len(messages),
                    "final_text": final_text,
                    "session_query_index": self._query_counter,
                    "session_id": self._session_id,
                    "resume_session_id": self.resume_session_id,
                    "fork_session": self.fork_session,
                }
            except Exception as exc:
                if is_initialize_timeout_error(exc) and attempt < self.total_attempts:
                    logger.warning(
                        "agent | initialize 超时 | stage=%s | attempt=%s/%s | %.1f 秒后重建 session 重试",
                        stage,
                        attempt,
                        self.total_attempts,
                        self.retry_sleep,
                    )
                    await self.aclose()
                    if self.retry_sleep > 0:
                        await asyncio.sleep(self.retry_sleep)
                    continue
                raise

        raise RuntimeError("Agent execution exhausted all retry attempts unexpectedly.")

    async def compact(self, *, stage: str = "manual_compact") -> dict[str, Any]:
        """对当前 session 发送一次 `/compact` 请求，手动触发上下文压缩。"""
        if not self._opened or self._client is None:
            await self.start()

        logger.info(
            "agent | stage=%s | session_query=%s | 开始手动 compact | cwd=%s | session_id=%s",
            stage,
            self._query_counter + 1,
            self.cwd,
            self._session_id,
        )

        await self._client.query("/compact")
        messages: list[dict[str, Any]] = []
        compact_metadata: dict[str, Any] | None = None
        async for message in self._client.receive_response():
            serialized = serialize_message(message)
            session_id = extract_message_session_id(serialized)
            self._update_session_id(session_id)
            serialized["stage"] = stage
            serialized["session_query_index"] = self._query_counter + 1
            serialized["session_id"] = self._session_id
            serialized["resume_session_id"] = self.resume_session_id
            serialized["fork_session"] = self.fork_session
            messages.append(serialized)

            boundary_metadata = extract_compact_boundary_metadata(serialized)
            if boundary_metadata is not None:
                compact_metadata = boundary_metadata
                print("Compaction completed")
                if "pre_tokens" in compact_metadata:
                    print("Pre-compaction tokens:", compact_metadata["pre_tokens"])
                if "trigger" in compact_metadata:
                    print("Trigger:", compact_metadata["trigger"])

        self._query_counter += 1
        logger.info(
            "agent | stage=%s | session_query=%s | compact 完成 | message_count=%s | compact_boundary=%s | session_id=%s",
            stage,
            self._query_counter,
            len(messages),
            compact_metadata is not None,
            self._session_id,
        )
        return {
            "messages": messages,
            "message_texts": [message["text"] for message in messages],
            "message_count": len(messages),
            "session_query_index": self._query_counter,
            "session_id": self._session_id,
            "resume_session_id": self.resume_session_id,
            "fork_session": self.fork_session,
            "compact_metadata": compact_metadata,
        }


async def run_agent(
    *,
    prompt: str,
    agent_config: Any,
    cwd: Path | str,
) -> dict[str, Any]:
    """向后兼容的一次性调用包装。"""
    session = ClaudeAgentSession(agent_config=agent_config, cwd=cwd)
    try:
        return await session.run_prompt(prompt=prompt, stage="single_query")
    finally:
        await session.aclose()


def create_agent_session(*, agent_config: Any, cwd: Path | str) -> ClaudeAgentSession:
    """创建一个可跨多轮复用的 Agent session。"""
    return ClaudeAgentSession(agent_config=agent_config, cwd=cwd)


def extract_message_session_id(message: dict[str, Any]) -> str | None:
    """从序列化后的消息中尽量提取 session_id。"""
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None

    session_id = payload.get("session_id") or payload.get("sessionId")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()

    nested_data = payload.get("data")
    if isinstance(nested_data, dict):
        nested_session_id = (
            nested_data.get("session_id") or nested_data.get("sessionId")
        )
        if isinstance(nested_session_id, str) and nested_session_id.strip():
            return nested_session_id.strip()

    return None


def extract_compact_boundary_metadata(message: dict[str, Any]) -> dict[str, Any] | None:
    """从 SDK 消息中识别 compact_boundary，并提取 compact_metadata。"""

    def _search(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if value.get("subtype") == "compact_boundary":
                data = value.get("data")
                if isinstance(data, dict):
                    compact_metadata = data.get("compact_metadata")
                    if isinstance(compact_metadata, dict):
                        return compact_metadata
                compact_metadata = value.get("compact_metadata")
                if isinstance(compact_metadata, dict):
                    return compact_metadata
                return {}
            for item in value.values():
                found = _search(item)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = _search(item)
                if found is not None:
                    return found
        return None

    return _search(message)
