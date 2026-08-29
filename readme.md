# Claude Agent SDK Web Game Pipeline

这是一个基于 `claude_agent_sdk` 的 Web 小游戏批量生成与多轮优化工具。它会读取结构化需求，让 Claude 在本地工作区中生成项目，可选调用 verifier 评估结果，并根据历史反馈继续迭代。

当前主流程：

```text
输入数据
  -> 首轮生成
  -> verifier（可选）
  -> query_analysis 生成 effective_query
  -> 在同一项目上继续优化
  -> 保存轮次快照与 token 统计
```

## 主要能力

- 支持批量读取 `jsonl`、`csv` 和常见 Excel 文件
- 默认让 Agent 生成 `React + Vite + TypeScript` Web 游戏
- 每个样本复用一个主 session，保留多轮实现上下文
- 优化轮通过 fork session 分析上一轮结果，生成本轮 `effective_query`
- 支持样本级异步并发和轮次级断点续跑
- 可选调用 verifier，多次独立评估后聚合结果
- 保存每轮项目、prompt、消息轨迹、verifier 结果及全局 JSONL 快照
- 汇总 SDK `ResultMessage` 的 token 和 cost，并提供轨迹整理、token 分析脚本

## 仓库结构

```text
.
├── data/
│   └── query.jsonl                         # 示例输入
├── prompt/
│   ├── game_generation.md                  # 首轮生成
│   ├── game_optimization_without_context.md  # 后续优化
│   ├── game_query_analysis.md              # 生成 effective_query
│   └── game_session_bootstrap.md            # resume 后注入历史上下文
├── script/
│   ├── analyze_assistant_token_usage.py     # token 分析及报告
│   └── merge_messages_for_view.py           # 合并多轮消息轨迹
├── utils/                                   # 配置、会话、产物和 verifier 工具
├── env_config.yml                           # 运行配置示例
├── generate.py                              # 主入口
├── requirements.txt
└── readme.md
```

> `env_config.yml` 目前包含特定运行环境的示例值，其中 `data/test_compact.jsonl`、`data/project_template.json` 和 `prompt/game_optimization_with_context.md` 未包含在本仓库中。首次运行前请按下文调整配置；仓库当前实际提供的是 `without_context` 优化模板。

## 环境要求

- Python 3.10 或更高版本；代码使用了 `dataclass(slots=True)`
- 可用的 Claude CLI，并确保 `agent.cli_path` 指向实际可执行文件
- 已完成 Claude CLI 登录，或通过 `agent.env` 配置所需凭证
- 如启用 verifier，需要可访问的 verifier HTTP 服务

安装依赖：

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install claude-agent-sdk
```

`requirements.txt` 包含 `requests`、`PyYAML` 和 `openpyxl`。`claude-agent-sdk` 需要单独安装；读取旧版 `.xls` 时还需安装 `pandas` 及相应 Excel engine。

## 快速开始

建议复制一份本地配置：

```bash
cp env_config.yml env_config.local.yml
```

至少修改以下项目：

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
  model:                         # 留空时使用 CLI/环境中的默认模型
  base_url:                      # 使用自定义网关时填写
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
  project_template_file:        # 可选；没有模板文件时保持为空

verifier:
  enabled: false

optimization:
  enabled: true
  rounds: 1
  continue_when_verifier_fails: true
```

先执行一次不带 verifier 和优化轮的单样本冒烟运行：

```bash
python generate.py \
  --config env_config.local.yml \
  --limit 1 \
  --disable-verifier \
  --disable-optimization
```

确认首轮正常后，再按配置执行完整流程：

```bash
python generate.py --config env_config.local.yml
```

## 命令行参数

命令行参数会覆盖配置文件中的对应值：

| 参数 | 作用 |
| --- | --- |
| `--config PATH` | 配置文件路径，默认 `env_config.yml` |
| `--input PATH` | 覆盖 `input.path` |
| `--output-dir PATH` | 覆盖 `project.output_dir` |
| `--rounds N` | 覆盖优化轮数；`0` 表示只生成首轮 |
| `--limit N` | 最多处理多少条有效记录 |
| `--prompt-name NAME` | 覆盖 `prompts.selected` |
| `--disable-verifier` | 本次运行关闭 verifier |
| `--disable-optimization` | 本次运行只执行首轮生成 |

例如：

```bash
python generate.py --config env_config.local.yml --limit 5
python generate.py --config env_config.local.yml --rounds 2
python generate.py --config env_config.local.yml --output-dir ./outputs_debug
```

## 输入格式

支持以下后缀：

- `.jsonl`
- `.csv`
- `.xlsx`、`.xlsm`、`.xltx`、`.xltm`
- `.xls`（需要 `pandas`）

每条记录至少需要 `data_id` 和 `query`。字段名可通过 `input.data_id_field`、`input.query_field` 修改。空值记录会被跳过，`data_id_prefix` 可用于按 ID 前缀过滤。

JSONL 示例：

```json
{"data_id":"game_001","query":"Build a 2048 game with score tracking and smooth tile animations.","L2":"react"}
```

`L2` 是默认的 verifier 语言字段；不存在时使用 `verifier.default_language`。

## 执行模型

每个样本的活跃工作区固定为：

```text
<output_dir>/<data_id>/main/project
```

`round_0`、`round_1` 等目录是轮次完成后的快照，不是 Agent 的活跃写入目录。

单个样本的执行过程如下：

1. 首轮使用原始 `query`，主 session 在 `main/project` 中生成项目。
2. 收集文本文件；启用 verifier 时发送项目快照并保存评估结果。
3. 从第二轮开始，由主 session fork 出 `query_analysis` session。
4. fork session 只写 `main/effective_query.txt`，主 session 再执行优化 prompt。
5. 成功后将整个 `main` 快照到对应的 `round_n`。
6. `manual_compact=true` 时，每个成功轮次后向主 session 发送 `/compact`。

`agent.fork_session.reuse=true` 会在同一条样本内复用持久分析分支；设为 `false` 时，每轮创建并关闭一次性 fork。fork 可单独覆盖 `model`、`base_url`、`thinking` 和 `env`，未设置项继承主 Agent 配置。

## 核心配置

所有相对路径都以配置文件所在目录为基准。字符串中的 `${ENV_NAME}` 会在加载配置时递归替换为环境变量；未定义变量会替换为空字符串。

### `project`

- `output_dir`：输出根目录
- `max_concurrent_tasks`：同时处理的样本数；同一样本内的轮次仍按顺序执行
- `resume`：是否复用已有成功轮次

### `agent`

- `model`、`base_url`：主 session 使用的模型和 API 地址
- `thinking`：支持 `adaptive`、`disabled`，或带正整数 `budget_tokens` 的 `enabled`
- `max_turns`、`cli_path`、`permission_mode`、`setting_sources`：透传给 Claude Agent SDK
- `initialize_timeout_ms`：初始化超时，代码会保证最少为 60 秒
- `initialize_max_retries`、`initialize_retry_sleep`：只重试 SDK initialize 超时
- `manual_compact`：每轮成功后是否手动压缩主 session 上下文
- `env`：传给 Claude CLI 的环境变量；不要把真实密钥提交到仓库
- `fork_session`：`query_analysis` 专用分支配置

### `prompts`

- `selected`：首轮生成模板名称
- `generation_templates`：名称到生成 prompt 的映射
- `optimization_context_mode`：选择 `optimization_templates` 中的同名模板
- `optimization_templates`：优化 prompt 映射
- `query_analysis_template`：提炼本轮 `effective_query` 的 prompt
- `session_bootstrap_template`：resume 后向新主 session 注入历史的 prompt
- `project_template_file`：可选项目模板 JSON，仅在首轮读取；文件不存在会直接报错
- `variables`：自定义 prompt 变量，值会转换为字符串后参与模板替换

Prompt 使用 `{{variable_name}}` 占位符。常用变量包括 `effective_query`、`original_query`、`workspace_dir`、`round_name`、`history_summary`、`verifier_feedback`、`project_template_json`。

### `verifier`

完整示例：

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

- `request_count`：每轮独立请求次数；成功结果取均值，reason 按请求合并
- `max_retries`：每次独立请求失败后的额外重试次数
- `continue_when_verifier_fails`：在 `optimization` 中控制 verifier 最终失败后是否继续
- 本地 `localhost`、`127.0.0.1`、`::1` 地址会关闭环境代理并直连
- 返回值兼容 `evaluations.<dimension>.score/reason` 和平铺的 `*_score`、`*_reason`

### `optimization`

- `enabled`：是否执行优化轮
- `rounds`：优化轮数量；总轮数为 `1 + rounds`
- `continue_when_verifier_fails`：verifier 失败时是否继续后续轮次

## 输出产物

典型目录：

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
    │   ├── verifier_result.json        # 启用 verifier 时生成
    │   └── project/
    ├── round_0/
    │   └── ...                         # 首轮 main 快照
    └── round_1/
        └── ...                         # 第一轮优化快照
```

- `project_files.jsonl`：成功轮次的文本文件快照，可用于恢复项目目录
- `verifier_result.jsonl`：每个已评估轮次的聚合结果
- `token_usage.jsonl`：每个已处理样本一行，按 stage、session 汇总 token 和 cost
- `prompt.txt`：`effective_query`、分析 prompt 和执行 prompt 的调试合集
- `messages.json`：分析与实现阶段的序列化 SDK 消息

`node_modules`、`dist`、`.git`、`__pycache__` 不会写入 `project_files.jsonl`。三个全局 JSONL 均采用追加写入，重复运行会保留历史记录；resume 建索引时，同一 `(data_id, round_index)` 使用最后一条记录。

## Resume 规则

resume 以 `(data_id, round_index)` 为粒度判断：

- 必须存在 `project_files.jsonl` 项目快照
- 当前启用 verifier 时，还必须存在无 `error` 的 verifier 结果
- 历史 verifier 的 `request_count` 不能少于当前配置
- 项目目录缺失时，会从 `project_files.jsonl` 恢复
- 任一上游轮次实际重跑后，后续轮次会全部重跑，避免上下文不一致
- 复用历史轮次后继续新轮次时，会先用 `game_session_bootstrap.md` 注入历史；注入失败时退回磁盘产物继续执行

如需强制重跑，可将 `project.resume` 设为 `false`，或换一个新的 `output_dir`。注意：旧的 `round_n` 目录会被新快照替换。

## 辅助脚本

按轮次顺序合并消息，生成更适合轨迹查看器消费的文件：

```bash
python script/merge_messages_for_view.py \
  outputs/game_001/round_0/messages.json \
  outputs/game_001/round_1/messages.json \
  --output outputs/game_001/trajectory.json
```

加 `--drop-payload` 可只保留精简的 `type` 和 `text`。

分析一个或多个 `messages.json` 中的 AssistantMessage token，并输出 JSON、CSV 和可选 HTML 报告：

```bash
python script/analyze_assistant_token_usage.py \
  outputs/game_001/round_0/messages.json \
  outputs/game_001/round_1/messages.json \
  --json-output outputs/game_001/token_analysis.json \
  --csv-output outputs/game_001/token_analysis.csv \
  --html-output outputs/game_001/token_analysis.html
```

## 常见问题

- `dataclass() got an unexpected keyword argument 'slots'`：当前 Python 低于 3.10。
- `Input file does not exist`：修改 `input.path`；仓库示例数据是 `data/query.jsonl`。
- `Project template file does not exist`：提供对应 JSON，或将 `project_template_file` 留空。
- `Prompt template does not exist`：仓库未提供 `with_context` 模板，请使用 `without_context` 或自行补充模板。
- `claude_agent_sdk is not available`：安装 `claude-agent-sdk`，并确认运行命令使用的是同一个 Python 环境。
- initialize 超时：检查 `agent.cli_path`、认证和网络，再按需增大 `initialize_timeout_ms`。
- verifier 连接失败：确认服务地址；调试生成链路时可使用 `--disable-verifier`。
