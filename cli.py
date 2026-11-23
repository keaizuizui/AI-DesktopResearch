#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小醉桌面研究 CLI：无 GUI 运行调研任务和评测题库。

用法：
    python cli.py run "分析中国无糖茶市场" --pages 30
    python cli.py eval --limit 3
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESEARCH_DIR = ROOT / "调研工具"
if not RESEARCH_DIR.exists():
    fallback = os.environ.get("XIAOZUI_RESEARCH_DIR", "")
    if not fallback:
        raise SystemExit("找不到调研工具目录，请设置 XIAOZUI_RESEARCH_DIR")
    RESEARCH_DIR = Path(fallback)
sys.path.insert(0, str(RESEARCH_DIR))

from config import load_config, apply_config_to_env  # noqa: E402
from engine import FusionResearchBot  # noqa: E402
from deliverables import DeliverableCompiler  # noqa: E402


def build_cfg(args) -> dict:
    """加载配置并应用 CLI 覆盖项。"""
    cfg = load_config(getattr(args, "preset", None))
    if getattr(args, "output_dir", None):
        cfg["output_dir"] = str(Path(args.output_dir).expanduser().resolve())
    if getattr(args, "pages", None):
        cfg["max_pages"] = args.pages
    if getattr(args, "mode", None):
        cfg["v15_mode_forced"] = args.mode == "deep"
    return cfg


def collect_metrics(bot, elapsed: float) -> dict:
    """从 bot 收集评测指标。"""
    ledger = getattr(bot, "claim_ledger", None)
    claims = ledger.get_all_claims() if ledger else []
    by_status = {}
    for c in claims:
        key = getattr(c.status, "value", str(c.status))
        by_status[key] = by_status.get(key, 0) + 1
    gate = getattr(bot, "_last_gate_result", None)
    quality = getattr(bot, "_last_quality_report", {}) or {}
    cost = None
    if getattr(bot, "cost_tracker", None):
        try:
            cost = bot.cost_tracker.get_total()
        except Exception:
            cost = None
    return {
        "elapsed_sec": elapsed,
        "materials": len(getattr(bot, "all_materials", []) or []),
        "chapters": len(getattr(bot, "chapters_md", []) or []),
        "claims_total": len(claims),
        "claims_by_status": by_status,
        "support_ratio": quality.get("citation_support_ratio"),
        "gate_passed": getattr(gate, "passed", None),
        "gate_summary": getattr(gate, "summary", ""),
        "quality_overall": quality.get("overall_quality"),
        "cost_estimated": cost,
        "coverage_notes": list(getattr(bot, "_coverage_notes", []) or []),
    }


def run_single(task: str, cfg: dict, deliver: bool = True) -> dict:
    """跑一个任务并返回指标；交付失败只记录，不掩盖调研结果。"""
    apply_config_to_env(cfg)
    started = time.time()
    bot = FusionResearchBot(cfg, gui=None)
    asyncio.run(bot.run(task))
    metrics = collect_metrics(bot, round(time.time() - started, 1))
    metrics["task"] = task
    metrics["report_title"] = getattr(bot, "report_title", "")
    if deliver:
        try:
            compiler = DeliverableCompiler(cfg["output_dir"], cfg, bot.llm)
            path = compiler.compile_word(
                bot.report_title,
                bot.chapters_md,
                bot.add_action_plan(),
                source_appendix=bot._get_source_appendix(),
                coverage_notes=getattr(bot, "_coverage_notes", None),
            )
            metrics["report_path"] = str(path)
        except Exception as e:
            metrics["deliver_error"] = f"{type(e).__name__}: {e}"
    return metrics


def _require_key(cfg: dict) -> bool:
    if cfg.get("llm_api_key"):
        return True
    print("未配置 llm_api_key。请先在 GUI 的【配置】中保存，或设置 research_config.json。")
    return False


def cmd_run(args) -> int:
    cfg = build_cfg(args)
    if args.dry_run:
        print(json.dumps({
            "output_dir": cfg.get("output_dir"),
            "max_pages": cfg.get("max_pages"),
            "v15_mode_forced": cfg.get("v15_mode_forced", False),
            "has_llm_key": bool(cfg.get("llm_api_key")),
        }, ensure_ascii=False, indent=2))
        return 0
    if not _require_key(cfg):
        return 2
    metrics = run_single(args.task, cfg, deliver=not args.no_deliver)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


def _load_questions(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("questions", [])
    if not isinstance(data, list) or not data:
        raise SystemExit("题库格式错误：需要非空数组或 {\"questions\": [...]}")
    return data


def _write_summary(results_dir: Path, run_id: str, results: list):
    lines = [
        f"# 评测结果 {run_id}",
        "",
        f"- 题目数：{len(results)}",
        f"- 成功：{sum(1 for r in results if r.get('ok'))}",
        "",
        "| ID | 状态 | 耗时(s) | 素材 | 声明 | 支持率 | 门禁 | 成本估算 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        m = r.get("metrics", {})
        lines.append(
            f"| {r['id']} | {'OK' if r.get('ok') else 'FAIL'} | "
            f"{m.get('elapsed_sec', '—')} | {m.get('materials', '—')} | "
            f"{m.get('claims_total', '—')} | {m.get('support_ratio', '—')} | "
            f"{'通过' if m.get('gate_passed') else '不通过' if m.get('gate_passed') is False else '—'} | "
            f"{m.get('cost_estimated', '—')} |"
        )
    (results_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_eval(args) -> int:
    questions = _load_questions(args.questions)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        questions = [q for q in questions if q.get("id") in wanted]
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print("没有匹配的题目")
        return 2

    if args.dry_run:
        for q in questions:
            print(f"{q.get('id')}: {q.get('task')}")
        print(f"共 {len(questions)} 题；题库校验通过")
        return 0

    cfg = build_cfg(args)
    if not _require_key(cfg):
        return 2

    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = Path(args.results_dir) / run_id
    results_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for q in questions:
        qid = q.get("id") or f"Q{len(results) + 1}"
        local_cfg = dict(cfg)
        if q.get("pages"):
            local_cfg["max_pages"] = int(q["pages"])
        if q.get("mode"):
            local_cfg["v15_mode_forced"] = q["mode"] == "deep"
        print(f"[评测] {qid} 开始：{q.get('task')}")
        try:
            metrics = run_single(q["task"], local_cfg, deliver=not args.no_deliver)
            results.append({"id": qid, "ok": True, "metrics": metrics})
            print(f"[评测] {qid} 完成：素材 {metrics['materials']} 条，"
                  f"声明 {metrics['claims_total']} 条，支持率 {metrics['support_ratio']}")
        except Exception as e:
            results.append({"id": qid, "ok": False, "error": f"{type(e).__name__}: {e}"})
            print(f"[评测] {qid} 失败：{type(e).__name__}: {e}")

    (results_dir / "metrics.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_summary(results_dir, run_id, results)
    print(f"评测完成：{results_dir}")
    return 0 if all(r.get("ok") for r in results) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="xiaozui-research", description="小醉桌面研究 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--preset", default=None, help="配置预设名")
    common.add_argument("--output-dir", default=None, help="输出目录")
    common.add_argument("--pages", type=int, default=None, help="目标页数")
    common.add_argument("--mode", choices=["regular", "deep"], default=None, help="调研模式")

    p_run = sub.add_parser("run", parents=[common], help="运行单个调研任务")
    p_run.add_argument("task", help="调研任务文本")
    p_run.add_argument("--no-deliver", action="store_true", help="不生成 Word 交付物")
    p_run.add_argument("--dry-run", action="store_true", help="只打印配置，不调用模型")
    p_run.set_defaults(func=cmd_run)

    p_eval = sub.add_parser("eval", parents=[common], help="运行评测题库")
    p_eval.add_argument("--questions", default=str(ROOT / "eval" / "questions.json"))
    p_eval.add_argument("--limit", type=int, default=None)
    p_eval.add_argument("--only", default=None, help="只跑指定 id，逗号分隔")
    p_eval.add_argument("--results-dir", default=str(ROOT / "eval" / "results"))
    p_eval.add_argument("--no-deliver", action="store_true")
    p_eval.add_argument("--dry-run", action="store_true")
    p_eval.set_defaults(func=cmd_eval)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
