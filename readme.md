# Claude Agent SDK Web Game Pipeline

This project is a batch generation and multi-round optimization pipeline for web games built on `claude_agent_sdk`. It reads structured requirements, asks Claude to generate projects in local workspaces, optionally evaluates them with a verifier, and iterates based on previous results.

The current pipeline is:

```text
Input data
  -> Initial generation
  -> Verifier (optional)
  -> query_analysis creates an effective_query
  -> Continue optimizing the same project
  -> Save round snapshots and token usage
```

## Features

- Reads batches from `jsonl`, `csv`, and common Excel formats
- Generates `React + Vite + TypeScript` web games by default
- Reuses one main session per record to preserve implementation context across rounds
- Uses a forked session during optimization rounds to derive the next `effective_query`
- Supports asynchronous record-level concurrency and round-level resume
- Optionally runs multiple independent verifier requests and aggregates their results
- Saves the project, prompts, message trajectory, verifier result, and global JSONL snapshots for each round
- Summarizes token usage and cost from SDK `ResultMessage` objects
- Includes utilities for merging trajectories and analyzing token usage

## Repository Layout

```text
.
├── data/
│   └── query.jsonl                          # Example input
├── prompt/
│   ├── game_generation.md                   # Initial generation
│   ├── game_optimization_without_context.md # Later optimization rounds
│   ├── game_query_analysis.md               # Creates effective_query
│   └── game_session_bootstrap.md             # Restores context after resume
├── script/
│   ├── analyze_assistant_token_usage.py      # Token analysis and reports
│   └── merge_messages_for_view.py            # Merges round trajectories
├── utils/                                    # Config, sessions, artifacts, verifier
├── env_config.yml                            # Example runtime configuration
├── generate.py                               # Main entry point
├── requirements.txt
└── readme.md
```

> The current `env_config.yml` contains values for a specific runtime environment. It references `data/test_compact.jsonl`, `data/project_template.json`, and `prompt/game_optimization_with_context.md`, which are not included in this repository. Update the configuration before the first run. This repository currently ships only the `without_context` optimization template.

## Requirements

- Python 3.10 or later; the code uses `dataclass(slots=True)`
- A working Claude CLI installation, with `agent.cli_path` pointing to the executable
- An authenticated Claude CLI session, or credentials supplied through `agent.env`
- An accessible verifier HTTP service if verification is enabled

Install the dependencies:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install claude-agent-sdk
```

`requirements.txt` includes `requests`, `PyYAML`, and `openpyxl`. Install `claude-agent-sdk` separately. Reading legacy `.xls` files also requires `pandas` and a compatible Excel engine.

## Quick Start

Create a local configuration file:

```bash
cp env_config.yml env_config.local.yml
```

At minimum, update it as follows:

```yaml
project:
  output_dir: ./outputs
  max_concurrent_tasks: 1
  resume: true

input:
  path: ./data/query.jsonl
  data_id_field: data_id
  query_field: query
  data_id_prefix: ""
  limit: 1

agent:
  model:                         # Empty: use the CLI/environment default
  base_url:                      # Set this when using a custom gateway
  thinking:
    type: disabled
  max_turns: 200
  cli_path: /usr/local/bin/claude
  permission_mode: bypassPermissions
  setting_sources:
    - project
  initialize_timeout_ms: 180000
  initialize_max_retries: 2
  initialize_retry_sleep: 5
  manual_compact: false
  env: {}
  fork_session:
    reuse: true
    env: {}

prompts:
  selected: default
  generation_templates:
    default: ./prompt/game_generation.md
  optimization_context_mode: without_context
  optimization_templates:
    without_context: ./prompt/game_optimization_without_context.md
  query_analysis_template: ./prompt/game_query_analysis.md
  session_bootstrap_template: ./prompt/game_session_bootstrap.md
  project_template_file:        # Optional; leave empty when no template exists

verifier:
  enabled: false

optimization:
  enabled: true
  rounds: 1
  continue_when_verifier_fails: true
```

Start with a single-record smoke test that skips the verifier and optimization:

```bash
python generate.py \
  --config env_config.local.yml \
  --limit 1 \
  --disable-verifier \
  --disable-optimization
```

After the initial round succeeds, run the complete configured pipeline:

```bash
python generate.py --config env_config.local.yml
```

## Command-Line Options

Command-line options override their corresponding configuration values:

| Option | Description |
| --- | --- |
| `--config PATH` | Configuration file; defaults to `env_config.yml` |
| `--input PATH` | Override `input.path` |
| `--output-dir PATH` | Override `project.output_dir` |
| `--rounds N` | Override the number of optimization rounds; `0` runs only generation |
| `--limit N` | Process at most N valid records |
| `--prompt-name NAME` | Override `prompts.selected` |
| `--disable-verifier` | Disable the verifier for this run |
| `--disable-optimization` | Run only the initial generation round |

Examples:

```bash
python generate.py --config env_config.local.yml --limit 5
python generate.py --config env_config.local.yml --rounds 2
python generate.py --config env_config.local.yml --output-dir ./outputs_debug
```

## Input Formats

Supported file extensions:

- `.jsonl`
- `.csv`
- `.xlsx`, `.xlsm`, `.xltx`, and `.xltm`
- `.xls` with `pandas` installed

Each record must contain at least `data_id` and `query`. Change the field names with `input.data_id_field` and `input.query_field`. Records with empty required values are skipped, and `data_id_prefix` filters records by ID prefix.

JSONL example:

```json
{"data_id":"game_001","query":"Build a 2048 game with score tracking and smooth tile animations.","L2":"react"}
```

`L2` is the default verifier language field. If it is absent, the pipeline uses `verifier.default_language`.

## Execution Model

The active workspace for each record is always:

```text
<output_dir>/<data_id>/main/project
```

Directories such as `round_0` and `round_1` are snapshots created after a round finishes. The Agent does not use them as its active write target.

A record is processed as follows:

1. The initial round uses the original `query`, and the main session generates the project in `main/project`.
2. The pipeline collects text files and, when enabled, sends a project snapshot to the verifier.
3. Starting with the second round, the main session creates a fork for `query_analysis`.
4. The fork writes only `main/effective_query.txt`; the main session then runs the optimization prompt.
5. After a successful round, the pipeline snapshots all of `main` into the corresponding `round_n` directory.
6. When `manual_compact=true`, the pipeline sends `/compact` to the main session after each successful round.

With `agent.fork_session.reuse=true`, one persistent analysis fork is reused within the same record. When it is `false`, the pipeline creates and closes a temporary fork for every optimization round. A fork can override `model`, `base_url`, `thinking`, and `env`; unset values inherit the main Agent configuration.

## Core Configuration

All relative paths are resolved from the directory containing the configuration file. `${ENV_NAME}` placeholders are recursively replaced with environment variable values when the configuration is loaded. An undefined variable becomes an empty string.

### `project`

- `output_dir`: root directory for generated artifacts
- `max_concurrent_tasks`: number of records processed concurrently; rounds within one record remain sequential
- `resume`: reuse previously completed rounds when possible

### `agent`

- `model` and `base_url`: model and API endpoint for the main session
- `thinking`: accepts `adaptive`, `disabled`, or `enabled` with a positive `budget_tokens` value
- `max_turns`, `cli_path`, `permission_mode`, and `setting_sources`: passed to the Claude Agent SDK
- `initialize_timeout_ms`: SDK initialization timeout, normalized to at least 60 seconds
- `initialize_max_retries` and `initialize_retry_sleep`: retries only SDK initialize timeouts
- `manual_compact`: manually compact the main session after each successful round
- `env`: environment variables passed to the Claude CLI; do not commit real credentials
- `fork_session`: dedicated configuration for the `query_analysis` branch

### `prompts`

- `selected`: name of the initial generation template
- `generation_templates`: mapping from template names to generation prompt files
- `optimization_context_mode`: selects the matching entry in `optimization_templates`
- `optimization_templates`: mapping of optimization modes to prompt files
- `query_analysis_template`: prompt that derives the next `effective_query`
- `session_bootstrap_template`: prompt that restores history into a new main session after resume
- `project_template_file`: optional project template JSON loaded only in the first round; a missing file causes an error
- `variables`: custom prompt values converted to strings before rendering

Prompts use `{{variable_name}}` placeholders. Common variables include `effective_query`, `original_query`, `workspace_dir`, `round_name`, `history_summary`, `verifier_feedback`, and `project_template_json`.

### `verifier`

Complete example:

```yaml
verifier:
  enabled: true
  api_url: http://127.0.0.1:8301/verify
  agent_type: interactive_video
  timeout: 1800
  max_retries: 3
  retry_sleep: 5
  request_count: 1
  default_language: react
  language_field: L2
  keep_existing_project: false
  evaluator_config:
    vlm:
      model: gemini-3-pro-preview
      prompt_version: v2
    agent:
      model: gemini-3-pro-preview
      prompt_version: v2
      max_steps: 50
      timeout: 1200
```

- `request_count`: independent verifier requests per round; successful numeric scores are averaged and reasons are combined by request
- `max_retries`: additional attempts for each independent request
- `continue_when_verifier_fails`: defined under `optimization`; controls whether later rounds continue after the verifier ultimately fails
- Local `localhost`, `127.0.0.1`, and `::1` endpoints bypass environment proxies
- Responses may use either `evaluations.<dimension>.score/reason` or flat `*_score` and `*_reason` fields

### `optimization`

- `enabled`: enable optimization rounds
- `rounds`: number of optimization rounds; the total round count is `1 + rounds`
- `continue_when_verifier_fails`: continue with later rounds after a verifier failure

## Output Artifacts

Typical layout:

```text
<output_dir>/
├── project_files.jsonl
├── verifier_result.jsonl
├── token_usage.jsonl
└── game_001/
    ├── main/
    │   ├── effective_query.txt
    │   ├── prompt.txt
    │   ├── messages.json
    │   ├── verifier_result.json         # Present when the verifier is enabled
    │   └── project/
    ├── round_0/
    │   └── ...                          # Initial main snapshot
    └── round_1/
        └── ...                          # First optimization snapshot
```

- `project_files.jsonl`: text-file snapshot for each successful round; it can restore a missing project directory
- `verifier_result.jsonl`: aggregated result for every evaluated round
- `token_usage.jsonl`: one entry per processed record, grouped by stage and session with token and cost totals
- `prompt.txt`: debug bundle containing the effective query, analysis prompt, and implementation prompt
- `messages.json`: serialized SDK messages from the analysis and implementation stages

`node_modules`, `dist`, `.git`, and `__pycache__` are excluded from `project_files.jsonl`. All three global JSONL files are append-only, so repeated runs preserve earlier entries. When building a resume index, the last entry for each `(data_id, round_index)` wins.

## Resume Rules

Resume works at `(data_id, round_index)` granularity:

- A `project_files.jsonl` snapshot must exist
- If the verifier is enabled for the current run, a verifier result without `error` must also exist
- The historical verifier `request_count` must be at least the currently configured value
- If the project directory is missing, it is restored from `project_files.jsonl`
- Once an earlier round is actually rerun, all later rounds are rerun to keep context consistent
- Before continuing after reused historical rounds, `game_session_bootstrap.md` injects history into the new main session; if bootstrap fails, execution continues using artifacts on disk

To force a clean rerun, set `project.resume` to `false` or choose a new `output_dir`. Existing `round_n` directories are replaced when new snapshots are created.

## Utility Scripts

Merge message files in round order into a trajectory file suitable for viewers:

```bash
python script/merge_messages_for_view.py \
  outputs/game_001/round_0/messages.json \
  outputs/game_001/round_1/messages.json \
  --output outputs/game_001/trajectory.json
```

Add `--drop-payload` to retain only the compact `type` and `text` fields.

Analyze `AssistantMessage` token usage across one or more `messages.json` files and produce JSON, CSV, and optionally HTML reports:

```bash
python script/analyze_assistant_token_usage.py \
  outputs/game_001/round_0/messages.json \
  outputs/game_001/round_1/messages.json \
  --json-output outputs/game_001/token_analysis.json \
  --csv-output outputs/game_001/token_analysis.csv \
  --html-output outputs/game_001/token_analysis.html
```

## Troubleshooting

- `dataclass() got an unexpected keyword argument 'slots'`: Python is older than 3.10.
- `Input file does not exist`: update `input.path`; the included example is `data/query.jsonl`.
- `Project template file does not exist`: provide the referenced JSON file or leave `project_template_file` empty.
- `Prompt template does not exist`: this repository does not include the `with_context` template; use `without_context` or add your own template.
- `claude_agent_sdk is not available`: install `claude-agent-sdk` in the same Python environment used to run the pipeline.
- SDK initialize timeout: verify `agent.cli_path`, authentication, and network access, then increase `initialize_timeout_ms` if needed.
- Verifier connection failure: verify the service URL or use `--disable-verifier` while debugging the generation pipeline.
