#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
from html import escape
from pathlib import Path
from typing import Any


METRICS = (
    ("input_tokens", ("input_tokens",)),
    ("cache_creation_input_tokens", ("cache_creation_input_tokens",)),
    ("cache_read_input_tokens", ("cache_read_input_tokens",)),
    (
        "cache_creation_ephemeral_5m_input_tokens",
        ("cache_creation", "ephemeral_5m_input_tokens"),
    ),
    (
        "cache_creation_ephemeral_1h_input_tokens",
        ("cache_creation", "ephemeral_1h_input_tokens"),
    ),
    ("output_tokens", ("output_tokens",)),
)
TOP_LEVEL_SUM_METRICS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
STAGE_COLORS = {
    "implementation": "#d97706",
    "query_analysis": "#2563eb",
    "unknown": "#6b7280",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze AssistantMessage token usage step by step from one or more "
            "trajectory messages.json files."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input messages.json files in step order.",
    )
    parser.add_argument(
        "--json-output",
        required=True,
        help="Output JSON path for the compact analysis payload.",
    )
    parser.add_argument(
        "--csv-output",
        required=True,
        help="Output CSV path for the compact step-wise metrics table.",
    )
    parser.add_argument(
        "--html-output",
        help="Optional self-contained HTML report with stage-aware charts.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(content)


def get_nested_int(payload: dict[str, Any], path: tuple[str, ...]) -> int:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return 0
        current = current.get(key)

    if current is None:
        return 0
    if isinstance(current, bool):
        return int(current)
    if isinstance(current, (int, float)):
        return int(current)
    return 0


def load_messages_file(path: Path) -> tuple[list[dict[str, Any]], str]:
    payload = load_json(path)

    if isinstance(payload, list):
        messages = payload
        format_kind = "legacy_flat_messages"
    elif isinstance(payload, dict):
        messages = payload.get("messages")
        format_kind = "wrapped_messages"
    else:
        raise ValueError(f"{path} must contain a JSON object or JSON array")

    if not isinstance(messages, list):
        raise ValueError(f"{path} is missing a messages array")

    return messages, format_kind


def empty_metric_sum() -> dict[str, int]:
    return {metric: 0 for metric, _ in METRICS}


def build_total_sum(metric_sums: dict[str, int]) -> dict[str, int]:
    return {
        **metric_sums,
        "total_tokens": sum(metric_sums[name] for name in TOP_LEVEL_SUM_METRICS),
    }


def add_step_to_summary(summary: dict[str, Any], step: dict[str, Any]) -> None:
    summary["step_count"] += 1
    for metric, _ in METRICS:
        summary["sum"][metric] += step[metric]


def finalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        **summary,
        "sum": build_total_sum(summary["sum"]),
    }


def summarize_by_stage(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for step in steps:
        stage = step["stage"]
        if stage not in summaries:
            summaries[stage] = {"stage": stage, "step_count": 0, "sum": empty_metric_sum()}
        add_step_to_summary(summaries[stage], step)

    order = {"query_analysis": 0, "implementation": 1}
    return [
        finalize_summary(summary)
        for summary in sorted(summaries.values(), key=lambda item: order.get(item["stage"], 99))
    ]


def analyze_input(
    path: Path,
    *,
    start_global_step: int,
    running_sums: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    messages, format_kind = load_messages_file(path)
    round_name = path.parent.name
    steps: list[dict[str, Any]] = []
    source_summary = {"round": round_name, "step_count": 0, "sum": empty_metric_sum()}
    global_step = start_global_step

    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "AssistantMessage":
            continue

        global_step += 1
        payload_dict = message.get("payload")
        usage = {}
        if isinstance(payload_dict, dict) and isinstance(payload_dict.get("usage"), dict):
            usage = payload_dict["usage"]

        stage = message.get("stage") or "unknown"
        if format_kind == "legacy_flat_messages" and stage == "unknown":
            # Older init_exp-style traces are flat single-stage execution logs.
            stage = "implementation"

        row: dict[str, Any] = {
            "step": global_step,
            "round": round_name,
            "stage": stage,
        }

        for metric, metric_path in METRICS:
            value = get_nested_int(usage, metric_path)
            running_sums[metric] += value
            row[metric] = value

        row["total_tokens"] = sum(row[name] for name in TOP_LEVEL_SUM_METRICS)
        steps.append(row)
        add_step_to_summary(source_summary, row)

    return steps, finalize_summary(source_summary), global_step


def build_csv_rows(
    steps: list[dict[str, Any]], total_sum: dict[str, int]
) -> tuple[list[str], list[dict[str, Any]]]:
    fieldnames = ["step", "round", "stage"] + [metric for metric, _ in METRICS] + [
        "total_tokens"
    ]
    rows = [{name: step.get(name) for name in fieldnames} for step in steps]
    rows.append(
        {
            "step": "SUM",
            "round": "",
            "stage": "",
            **{metric: total_sum[metric] for metric, _ in METRICS},
            "total_tokens": total_sum["total_tokens"],
        }
    )
    return fieldnames, rows


def format_int(value: int) -> str:
    return f"{int(value):,}"


def build_summary_table(title: str, key_name: str, rows: list[dict[str, Any]]) -> str:
    headers = [key_name, "step_count"] + [metric for metric, _ in METRICS] + ["total_tokens"]
    header_html = "".join(f"<th>{escape(header)}</th>" for header in headers)

    body_rows: list[str] = []
    for row in rows:
        label = escape(str(row[key_name]))
        cells = [f"<td>{label}</td>", f"<td>{row['step_count']}</td>"]
        for metric, _ in METRICS:
            cells.append(f"<td>{format_int(row['sum'][metric])}</td>")
        cells.append(f"<td>{format_int(row['sum']['total_tokens'])}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        f"<section class='summary-block'><h2>{escape(title)}</h2>"
        "<div class='table-wrap'><table>"
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div></section>"
    )


def build_stage_ranges(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not steps:
        return []

    ranges: list[dict[str, Any]] = []
    start = 0
    current_stage = steps[0]["stage"]

    for index in range(1, len(steps) + 1):
        next_stage = steps[index]["stage"] if index < len(steps) else None
        if next_stage != current_stage:
            ranges.append(
                {
                    "stage": current_stage,
                    "start_index": start,
                    "end_index": index - 1,
                }
            )
            start = index
            current_stage = next_stage

    return ranges


def build_round_boundaries(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    for index in range(1, len(steps)):
        if steps[index]["round"] != steps[index - 1]["round"]:
            boundaries.append({"index": index, "round": steps[index]["round"]})
    return boundaries


def build_chart_svg(metric: str, steps: list[dict[str, Any]]) -> str:
    width = 1080
    height = 260
    left = 64
    right = 20
    top = 18
    bottom = 34
    plot_width = width - left - right
    plot_height = height - top - bottom

    values = [int(step[metric]) for step in steps]
    y_max = max(values) if values else 0
    if y_max <= 0:
        y_max = 1

    def x_at(index: int) -> float:
        if len(steps) <= 1:
            return left + plot_width / 2
        return left + (plot_width * index / (len(steps) - 1))

    def y_at(value: int) -> float:
        return top + plot_height - (plot_height * value / y_max)

    def band_start(index: int) -> float:
        if len(steps) <= 1:
            return left
        step_width = plot_width / (len(steps) - 1)
        if index == 0:
            return left
        return x_at(index) - step_width / 2

    def band_end(index: int) -> float:
        if len(steps) <= 1:
            return left + plot_width
        step_width = plot_width / (len(steps) - 1)
        if index == len(steps) - 1:
            return left + plot_width
        return x_at(index) + step_width / 2

    grid_lines: list[str] = []
    for tick in range(5):
        value = int(y_max * tick / 4)
        y = y_at(value)
        grid_lines.append(
            f"<line x1='{left}' y1='{y:.2f}' x2='{left + plot_width}' y2='{y:.2f}' "
            "stroke='#d6d3d1' stroke-width='1'/>"
        )
        grid_lines.append(
            f"<text x='{left - 10}' y='{y + 4:.2f}' text-anchor='end' "
            "font-size='11' fill='#57534e'>"
            f"{escape(format_int(value))}</text>"
        )

    ranges = build_stage_ranges(steps)
    stage_bands = []
    for item in ranges:
        stage = item["stage"]
        x0 = band_start(item["start_index"])
        x1 = band_end(item["end_index"])
        color = STAGE_COLORS.get(stage, STAGE_COLORS["unknown"])
        stage_bands.append(
            f"<rect x='{x0:.2f}' y='{top}' width='{x1 - x0:.2f}' height='{plot_height}' "
            f"fill='{color}' opacity='0.08'/>"
        )

    round_lines = []
    for boundary in build_round_boundaries(steps):
        if len(steps) <= 1:
            continue
        step_width = plot_width / (len(steps) - 1)
        x = x_at(boundary["index"]) - step_width / 2
        round_lines.append(
            f"<line x1='{x:.2f}' y1='{top}' x2='{x:.2f}' y2='{top + plot_height}' "
            "stroke='#44403c' stroke-dasharray='4 4' stroke-width='1.5'/>"
        )
        round_lines.append(
            f"<text x='{x + 6:.2f}' y='{top + 14:.2f}' font-size='11' fill='#44403c'>"
            f"{escape(boundary['round'])}</text>"
        )

    if steps:
        path_points = " ".join(
            f"{x_at(index):.2f},{y_at(value):.2f}" for index, value in enumerate(values)
        )
        line = (
            f"<polyline fill='none' stroke='#111827' stroke-width='2' "
            f"points='{path_points}'/>"
        )
    else:
        line = ""

    point_dots = []
    for index, step in enumerate(steps):
        stage = step["stage"]
        color = STAGE_COLORS.get(stage, STAGE_COLORS["unknown"])
        value = step[metric]
        point_dots.append(
            f"<circle cx='{x_at(index):.2f}' cy='{y_at(value):.2f}' r='3.2' "
            f"fill='{color}' stroke='white' stroke-width='1'>"
            f"<title>step={step['step']} | round={step['round']} | stage={stage} | {metric}={value}</title>"
            "</circle>"
        )

    axis_labels = []
    if steps:
        label_positions = [0, len(steps) // 2, len(steps) - 1]
        seen = set()
        for index in label_positions:
            if index in seen:
                continue
            seen.add(index)
            axis_labels.append(
                f"<text x='{x_at(index):.2f}' y='{height - 10}' text-anchor='middle' "
                "font-size='11' fill='#57534e'>"
                f"{steps[index]['step']}</text>"
            )

    return (
        "<section class='chart-block'>"
        f"<h2>{escape(metric)}</h2>"
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{escape(metric)} chart'>"
        "<rect x='0' y='0' width='100%' height='100%' fill='white'/>"
        f"{''.join(stage_bands)}"
        f"{''.join(grid_lines)}"
        f"{''.join(round_lines)}"
        f"<line x1='{left}' y1='{top + plot_height}' x2='{left + plot_width}' y2='{top + plot_height}' "
        "stroke='#78716c' stroke-width='1.5'/>"
        f"<line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_height}' "
        "stroke='#78716c' stroke-width='1.5'/>"
        f"{line}"
        f"{''.join(point_dots)}"
        f"{''.join(axis_labels)}"
        f"</svg></section>"
    )


def build_stage_chart_sections(
    steps: list[dict[str, Any]], stage_sums: list[dict[str, Any]]
) -> str:
    metrics_for_charts = [metric for metric, _ in METRICS] + ["total_tokens"]
    sections: list[str] = []

    for stage_summary in stage_sums:
        stage = stage_summary["stage"]
        stage_steps = [step for step in steps if step["stage"] == stage]
        if not stage_steps:
            continue

        color = STAGE_COLORS.get(stage, STAGE_COLORS["unknown"])
        charts = "".join(build_chart_svg(metric, stage_steps) for metric in metrics_for_charts)
        sections.append(
            "<section class='stage-section'>"
            "<div class='stage-header'>"
            f"<div class='stage-badge' style='background:{color}'></div>"
            f"<div><h2>{escape(stage)}</h2>"
            f"<p>{stage_summary['step_count']} steps, total_tokens = {format_int(stage_summary['sum']['total_tokens'])}</p>"
            "</div></div>"
            f"{charts}"
            "</section>"
        )

    return "".join(sections)


def build_html_report(analysis_payload: dict[str, Any]) -> str:
    steps = analysis_payload["steps"]
    round_sums = analysis_payload["round_sums"]
    stage_sums = analysis_payload["stage_sums"]
    total_sum = analysis_payload["sum"]

    stage_legend = "".join(
        "<span class='legend-item'>"
        f"<span class='legend-dot' style='background:{STAGE_COLORS.get(item['stage'], STAGE_COLORS['unknown'])}'></span>"
        f"{escape(item['stage'])}"
        "</span>"
        for item in stage_sums
    )

    overview = (
        "<section class='overview'>"
        f"<div class='card'><div class='card-label'>step_count</div><div class='card-value'>{analysis_payload['step_count']}</div></div>"
        f"<div class='card'><div class='card-label'>total_tokens</div><div class='card-value'>{format_int(total_sum['total_tokens'])}</div></div>"
        f"<div class='card'><div class='card-label'>input_tokens</div><div class='card-value'>{format_int(total_sum['input_tokens'])}</div></div>"
        f"<div class='card'><div class='card-label'>output_tokens</div><div class='card-value'>{format_int(total_sum['output_tokens'])}</div></div>"
        "</section>"
    )

    stage_sections = build_stage_chart_sections(steps, stage_sums)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Assistant Token Usage Report</title>
  <style>
    :root {{
      --bg: #f7f4ef;
      --panel: #fffdf8;
      --text: #1c1917;
      --muted: #57534e;
      --line: #d6d3d1;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, #fef3c7 0, transparent 28%),
        radial-gradient(circle at top right, #dbeafe 0, transparent 24%),
        linear-gradient(180deg, #faf7f2 0%, var(--bg) 100%);
    }}
    .page {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 34px;
      line-height: 1.1;
    }}
    p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 14px;
      color: var(--muted);
      font-size: 14px;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .legend-dot {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
      display: inline-block;
    }}
    .overview {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin: 22px 0 28px;
    }}
    .card, .summary-block, .chart-block {{
      background: var(--panel);
      border: 1px solid rgba(120, 113, 108, 0.2);
      border-radius: 18px;
      box-shadow: 0 12px 28px rgba(28, 25, 23, 0.06);
    }}
    .card {{
      padding: 16px 18px;
    }}
    .card-label {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 6px;
    }}
    .card-value {{
      font-size: 28px;
      line-height: 1;
      font-weight: 700;
    }}
    .summary-grid {{
      display: grid;
      gap: 18px;
    }}
    .summary-block {{
      padding: 16px 18px 18px;
    }}
    .summary-block h2, .chart-block h2 {{
      margin: 0 0 12px;
      font-size: 20px;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      min-width: 860px;
      font-size: 13px;
    }}
    th, td {{
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      text-align: right;
      white-space: nowrap;
    }}
    th:first-child, td:first-child {{
      text-align: left;
    }}
    .chart-block {{
      padding: 16px 18px 12px;
      margin-top: 18px;
    }}
    .stage-section {{
      margin-top: 28px;
      padding-top: 8px;
      border-top: 1px solid rgba(120, 113, 108, 0.25);
    }}
    .stage-header {{
      display: flex;
      align-items: center;
      gap: 14px;
      margin: 0 0 10px;
    }}
    .stage-header h2 {{
      margin: 0 0 4px;
      font-size: 24px;
    }}
    .stage-badge {{
      width: 14px;
      height: 54px;
      border-radius: 999px;
      flex: 0 0 auto;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.6);
    }}
    svg {{
      width: 100%;
      height: auto;
      display: block;
    }}
  </style>
</head>
<body>
  <main class="page">
    <h1>Assistant Token Usage Report</h1>
    <p>每个 AssistantMessage 是一个 step。图表现在按阶段拆开：<code>query_analysis</code> 和 <code>implementation</code> 分别单独绘制。</p>
    <div class="legend">{stage_legend}</div>
    {overview}
    <div class="summary-grid">
      {build_summary_table("By Stage", "stage", stage_sums)}
      {build_summary_table("By Round", "round", round_sums)}
      {build_summary_table("Overall Sum", "label", [{"label": "all", "step_count": analysis_payload["step_count"], "sum": total_sum}])}
    </div>
    {stage_sections}
  </main>
</body>
</html>
"""


def main() -> None:
    args = parse_args()
    input_paths = [Path(value).expanduser().resolve() for value in args.inputs]
    json_output = Path(args.json_output).expanduser().resolve()
    csv_output = Path(args.csv_output).expanduser().resolve()

    running_sums = empty_metric_sum()
    all_steps: list[dict[str, Any]] = []
    round_sums: list[dict[str, Any]] = []
    global_step = 0

    for path in input_paths:
        steps, source_summary, global_step = analyze_input(
            path,
            start_global_step=global_step,
            running_sums=running_sums,
        )
        all_steps.extend(steps)
        round_sums.append(source_summary)

    total_sum = build_total_sum(running_sums)
    stage_sums = summarize_by_stage(all_steps)

    analysis_payload = {
        "format": "assistant_token_usage_simple_v2",
        "source_files": [str(path) for path in input_paths],
        "metrics": [metric for metric, _ in METRICS],
        "step_count": len(all_steps),
        "round_sums": round_sums,
        "stage_sums": stage_sums,
        "sum": total_sum,
        "steps": all_steps,
    }

    write_text(json_output, json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n")

    fieldnames, csv_rows = build_csv_rows(all_steps, total_sum)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    with csv_output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    if args.html_output:
        html_output = Path(args.html_output).expanduser().resolve()
        write_text(html_output, build_html_report(analysis_payload))


if __name__ == "__main__":
    main()
