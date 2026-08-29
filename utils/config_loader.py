"""加载运行配置，并把原始字典转换为结构化配置对象。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

try:
    import yaml
except ImportError:  # pragma: no cover - 可选运行时依赖
    yaml = None


ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ThinkingConfigAdaptive(TypedDict):
    """让 Claude 自适应决定何时进入思考模式。"""

    type: Literal["adaptive"]


class ThinkingConfigEnabled(TypedDict):
    """显式开启思考模式，并指定 token 预算。"""

    type: Literal["enabled"]
    budget_tokens: int


class ThinkingConfigDisabled(TypedDict):
    """显式关闭思考模式。"""

    type: Literal["disabled"]


ThinkingConfig = (
    ThinkingConfigAdaptive | ThinkingConfigEnabled | ThinkingConfigDisabled
)


def default_evaluator_config() -> dict[str, Any]:
    """提供 verifier 的默认 evaluator 配置。"""
    return {
        "vlm": {
            "model": "gemini-3-pro-preview",
            "prompt_version": "v2",
        },
        "agent": {
            "model": "gemini-3-pro-preview",
            "prompt_version": "v2",
            "max_steps": 50,
            "timeout": 1200,
        },
    }


@dataclass(slots=True)
class ProjectConfig:
    """项目级执行配置。"""
    output_dir: Path
    max_concurrent_tasks: int = 2
    resume: bool = True


@dataclass(slots=True)
class InputConfig:
    """输入数据源及字段映射配置。"""
    path: Path
    data_id_field: str = "data_id"
    query_field: str = "query"
    data_id_prefix: str = ""
    limit: int | None = None


@dataclass(slots=True)
class AgentConfig:
    """Claude Agent 运行参数。"""
    model: str | None = None
    base_url: str | None = None
    thinking: ThinkingConfig | None = None
    max_turns: int = 200
    cli_path: str = "/usr/local/bin/claude"
    permission_mode: str = "bypassPermissions"
    setting_sources: list[str] = field(default_factory=lambda: ["project"])
    cwd: Path | None = None
    initialize_timeout_ms: int = 180000
    initialize_max_retries: int = 2
    initialize_retry_sleep: float = 5.0
    manual_compact: bool = True
    env: dict[str, str] = field(default_factory=dict)
    fork_session: "ForkSessionConfig" = field(default_factory=lambda: ForkSessionConfig())


@dataclass(slots=True)
class ForkSessionConfig:
    """query_analysis fork session 的专用配置。"""
    reuse: bool = True
    model: str | None = None
    base_url: str | None = None
    thinking: ThinkingConfig | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PromptConfig:
    """Prompt 模板选择与变量配置。"""
    selected: str
    generation_templates: dict[str, Path]
    optimization_context_mode: str
    optimization_templates: dict[str, Path]
    query_analysis_template: Path
    session_bootstrap_template: Path
    project_template_file: Path | None = None
    variables: dict[str, Any] = field(default_factory=dict)

    def get_generation_template(self) -> Path:
        """返回首轮生成要使用的 prompt 模板路径。"""
        try:
            return self.generation_templates[self.selected]
        except KeyError as exc:
            supported = ", ".join(sorted(self.generation_templates))
            raise ValueError(
                f"Unsupported prompts.selected={self.selected!r}. "
                f"Supported values: {supported}"
            ) from exc

    def get_optimization_template(self) -> Path:
        """根据配置返回优化轮次使用的 prompt 模板路径。"""
        try:
            return self.optimization_templates[self.optimization_context_mode]
        except KeyError as exc:
            supported = ", ".join(sorted(self.optimization_templates))
            raise ValueError(
                f"Unsupported prompts.optimization_context_mode="
                f"{self.optimization_context_mode!r}. "
                f"Supported values: {supported}"
            ) from exc


@dataclass(slots=True)
class VerifierConfig:
    """Verifier 服务相关配置。"""
    enabled: bool = False
    api_url: str = "http://localhost:8301/verify"
    agent_type: str = "static"
    timeout: int = 600
    max_retries: int = 3
    retry_sleep: float = 5.0
    request_count: int = 1
    default_language: str = "react"
    language_field: str = "L2"
    keep_existing_project: bool = False
    evaluator_config: dict[str, Any] = field(default_factory=default_evaluator_config)


@dataclass(slots=True)
class OptimizationConfig:
    """优化轮次控制配置。"""
    enabled: bool = True
    rounds: int = 2
    continue_when_verifier_fails: bool = True


@dataclass(slots=True)
class AppConfig:
    """整个应用的聚合配置对象。"""
    root_dir: Path
    project: ProjectConfig
    input: InputConfig
    agent: AgentConfig
    prompts: PromptConfig
    verifier: VerifierConfig
    optimization: OptimizationConfig


def expand_env_placeholders(value: Any) -> Any:
    """递归替换 `${ENV_NAME}` 形式的环境变量占位符。"""
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda match: os.environ.get(match.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_placeholders(item) for key, item in value.items()}
    return value


def resolve_path(base_dir: Path, value: str | Path | None) -> Path | None:
    """把相对路径解析为基于配置目录的绝对路径。"""
    if value is None:
        return None

    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def normalize_optional_text(value: Any) -> str | None:
    """把配置里的可选文本统一规整为非空字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_thinking_config(
    value: Any,
    *,
    field_name: str,
) -> ThinkingConfig | None:
    """解析 agent thinking 配置，并做基础校验。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping.")

    thinking_type = normalize_optional_text(value.get("type"))
    if thinking_type == "adaptive":
        extra_keys = set(value) - {"type"}
        if extra_keys:
            extras = ", ".join(sorted(extra_keys))
            raise ValueError(
                f"{field_name} does not allow extra keys for type='adaptive': {extras}"
            )
        return {"type": "adaptive"}

    if thinking_type == "disabled":
        extra_keys = set(value) - {"type"}
        if extra_keys:
            extras = ", ".join(sorted(extra_keys))
            raise ValueError(
                f"{field_name} does not allow extra keys for type='disabled': {extras}"
            )
        return {"type": "disabled"}

    if thinking_type == "enabled":
        extra_keys = set(value) - {"type", "budget_tokens"}
        if extra_keys:
            extras = ", ".join(sorted(extra_keys))
            raise ValueError(
                f"{field_name} does not allow extra keys for type='enabled': {extras}"
            )
        if "budget_tokens" not in value:
            raise ValueError(
                f"{field_name}.budget_tokens is required when type='enabled'."
            )
        budget_tokens = int(value["budget_tokens"])
        if budget_tokens <= 0:
            raise ValueError(
                f"{field_name}.budget_tokens must be a positive integer."
            )
        return {
            "type": "enabled",
            "budget_tokens": budget_tokens,
        }

    supported = "adaptive, enabled, disabled"
    raise ValueError(
        f"{field_name}.type must be one of: {supported}. "
        f"Received: {thinking_type!r}"
    )


def load_raw_config(config_path: Path) -> dict[str, Any]:
    """读取 yml/yaml/json 配置文件的原始内容。"""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")

    suffix = config_path.suffix.lower()
    if suffix in {".yml", ".yaml"}:
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to read .yml config files. "
                "Install it with `pip install PyYAML`."
            )
        with config_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    if suffix == ".json":
        with config_path.open("r", encoding="utf-8") as f:
            return json.load(f) or {}

    raise ValueError(
        f"Unsupported config file format: {config_path}. "
        "Use .yml, .yaml, or .json."
    )


def load_config(config_path: Path) -> AppConfig:
    """加载配置文件并转换为强类型配置对象。"""
    config_path = config_path.expanduser().resolve()
    root_dir = config_path.parent
    raw = expand_env_placeholders(load_raw_config(config_path))

    project_section = raw.get("project", {})
    input_section = raw.get("input", {})
    agent_section = raw.get("agent", {})
    prompts_section = raw.get("prompts", {})
    verifier_section = raw.get("verifier", {})
    optimization_section = raw.get("optimization", {})

    generation_templates_raw = prompts_section.get("generation_templates") or {
        "default": prompts_section.get("generation_template", "prompt/game_generation.md"),
    }
    generation_templates = {
        str(name): resolve_path(root_dir, str(path))
        for name, path in generation_templates_raw.items()
    }
    optimization_templates_raw = prompts_section.get("optimization_templates") or {
        "with_context": prompts_section.get(
            "optimization_with_context_template",
            prompts_section.get(
                "optimization_template",
                "prompt/game_optimization_with_context.md",
            ),
        ),
        "without_context": prompts_section.get(
            "optimization_without_context_template",
            "prompt/game_optimization_without_context.md",
        ),
    }
    optimization_templates = {
        str(name): resolve_path(root_dir, str(path))
        for name, path in optimization_templates_raw.items()
    }

    project = ProjectConfig(
        output_dir=resolve_path(root_dir, project_section.get("output_dir", "outputs")) or root_dir / "outputs",
        max_concurrent_tasks=int(project_section.get("max_concurrent_tasks", 2)),
        resume=bool(project_section.get("resume", True)),
    )
    input_config = InputConfig(
        path=resolve_path(root_dir, input_section.get("path", "data/example_input.jsonl")) or root_dir / "data/example_input.jsonl",
        data_id_field=str(input_section.get("data_id_field", "data_id")),
        query_field=str(input_section.get("query_field", "query")),
        data_id_prefix=str(input_section.get("data_id_prefix", "")),
        limit=int(input_section["limit"]) if input_section.get("limit") not in {None, ""} else None,
    )
    agent = AgentConfig(
        model=normalize_optional_text(agent_section.get("model")),
        base_url=normalize_optional_text(agent_section.get("base_url")),
        thinking=parse_thinking_config(
            agent_section.get("thinking"),
            field_name="agent.thinking",
        ),
        max_turns=int(agent_section.get("max_turns", 200)),
        cli_path=str(agent_section.get("cli_path", "/usr/local/bin/claude")),
        permission_mode=str(agent_section.get("permission_mode", "bypassPermissions")),
        setting_sources=list(agent_section.get("setting_sources", ["project"])),
        cwd=resolve_path(root_dir, agent_section.get("cwd")),
        initialize_timeout_ms=int(agent_section.get("initialize_timeout_ms", 180000)),
        initialize_max_retries=int(agent_section.get("initialize_max_retries", 2)),
        initialize_retry_sleep=float(agent_section.get("initialize_retry_sleep", 5.0)),
        manual_compact=bool(agent_section.get("manual_compact", True)),
        env={str(key): str(value) for key, value in agent_section.get("env", {}).items()},
        fork_session=ForkSessionConfig(
            reuse=bool((agent_section.get("fork_session") or {}).get("reuse", True)),
            model=normalize_optional_text((agent_section.get("fork_session") or {}).get("model")),
            base_url=normalize_optional_text((agent_section.get("fork_session") or {}).get("base_url")),
            thinking=parse_thinking_config(
                (agent_section.get("fork_session") or {}).get("thinking"),
                field_name="agent.fork_session.thinking",
            ),
            env={
                str(key): str(value)
                for key, value in ((agent_section.get("fork_session") or {}).get("env", {}) or {}).items()
            },
        ),
    )
    prompts = PromptConfig(
        selected=str(prompts_section.get("selected", "default")),
        generation_templates={name: path for name, path in generation_templates.items() if path is not None},
        optimization_context_mode=str(
            prompts_section.get("optimization_context_mode", "with_context")
        ),
        optimization_templates={
            name: path for name, path in optimization_templates.items() if path is not None
        },
        query_analysis_template=resolve_path(
            root_dir,
            prompts_section.get("query_analysis_template", "prompt/game_query_analysis.md"),
        )
        or root_dir / "prompt/game_query_analysis.md",
        session_bootstrap_template=resolve_path(
            root_dir,
            prompts_section.get("session_bootstrap_template", "prompt/game_session_bootstrap.md"),
        )
        or root_dir / "prompt/game_session_bootstrap.md",
        project_template_file=resolve_path(
            root_dir,
            prompts_section.get("project_template_file", "data/project_template.json"),
        ),
        variables=dict(prompts_section.get("variables", {})),
    )
    verifier = VerifierConfig(
        enabled=bool(verifier_section.get("enabled", False)),
        api_url=str(verifier_section.get("api_url", "http://localhost:8301/verify")),
        agent_type=str(verifier_section.get("agent_type", "static")),
        timeout=int(verifier_section.get("timeout", 600)),
        max_retries=int(verifier_section.get("max_retries", 3)),
        retry_sleep=float(verifier_section.get("retry_sleep", 5.0)),
        request_count=max(1, int(verifier_section.get("request_count", 1))),
        default_language=str(verifier_section.get("default_language", "react")),
        language_field=str(verifier_section.get("language_field", "L2")),
        keep_existing_project=bool(verifier_section.get("keep_existing_project", False)),
        evaluator_config=dict(
            verifier_section.get("evaluator_config", default_evaluator_config())
        ),
    )
    optimization = OptimizationConfig(
        enabled=bool(optimization_section.get("enabled", True)),
        rounds=int(optimization_section.get("rounds", 2)),
        continue_when_verifier_fails=bool(
            optimization_section.get("continue_when_verifier_fails", True)
        ),
    )

    return AppConfig(
        root_dir=root_dir,
        project=project,
        input=input_config,
        agent=agent,
        prompts=prompts,
        verifier=verifier,
        optimization=optimization,
    )
