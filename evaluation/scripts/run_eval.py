"""CryptoSage 实战任务一：自动评测脚本（示例实现）。

对评测样本集顺序执行一轮完整评测：
  1. 调用本地 CryptoSage API 产出分析（POST /api/analyze → 轮询 /api/report/{id}）；
  2. 规则校验（schema / 合规 / 低置信观望 / 重复文本 等 hard 项）；
  3. LLM-as-judge 按 evaluation/rubric.md 对 6 个维度逐项打分并给出失败模式标签；
  4. 可选第二 judge + 分歧仲裁（见 evaluation/scripts/README.md）；
  5. 输出 results/{meta.json, raw/, scores.json} 并打印汇总表。

用法：
  python evaluation/scripts/run_eval.py \\
      --dataset evaluation/dataset/samples.jsonl \\
      --out evaluation/results \\
      --api http://127.0.0.1:8000

依赖：httpx、openai（项目 requirements 已包含）。环境变量复用 .env：
  HY3_API_KEY / HY3_BASE_URL / HY3_SLOW_MODEL（默认 judge 模型），
  可选 EVAL_JUDGE_MODEL、EVAL_JUDGE_SECOND_MODEL / DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from openai import OpenAI

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover - dotenv 可选
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUBRIC = REPO_ROOT / "evaluation" / "rubric.md"

DIMENSIONS = ["D1", "D2", "D3", "D4", "D5", "D6"]

SYSTEM_PROMPT = (
    "你是一位严格的评测员。你将审阅一份加密货币多 Agent 研判系统产出的分析报告，"
    "并依据评分细则（rubric）逐维度打分。规则：只依据输出中可观察的证据评分，"
    "不引入系统外知识判断行情对错；分数必须为 1-5 的整数；"
    '最后只输出一个 JSON 对象，格式：{"D1":1,"D2":1,"D3":1,"D4":1,"D5":1,"D6":1,'
    '"labels":["标签"],"rationale":"一句话理由"}。'
)


def _rule_checks(report: dict) -> list[str]:
    """确定性规则校验（hard 项）。命中规则视为一次失败，写入 violations。"""
    violations: list[str] = []
    if not isinstance(report, dict):
        return ["empty_report"]

    # 1) 合规：免责声明
    text_blob = json.dumps(report, ensure_ascii=False)
    if "不构成" not in text_blob and "不构成投资建议" not in text_blob and not str(
        report.get("disclaimer") or ""
    ).strip():
        violations.append("missing_disclaimer")

    # 2) 尾部风险
    bsr = report.get("black_swan_risks")
    if not isinstance(bsr, list) or not bsr:
        violations.append("missing_black_swan_risks")

    # 3) 低置信应观望
    try:
        conf = float(report.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    advice = report.get("position_advice") or {}
    action = str(advice.get("action") or "")
    mode = str(report.get("execution_mode") or report.get("forecast_mode") or "")
    if conf < 0.60 and "观望" not in action and "observe" not in mode:
        violations.append("low_confidence_not_observe")

    # 4) 明显重复文本
    findings = report.get("key_findings")
    if isinstance(findings, list) and len(findings) >= 2:
        unique = {re.sub(r"\s+", "", str(f)) for f in findings if f}
        if unique and len(unique) / len(findings) < 0.6:
            violations.append("duplicate_rambling")

    # 5) 关键字段类型
    if not isinstance(report.get("key_levels"), dict):
        violations.append("missing_key_levels")
    return violations


def _pick_labels(violations: list[str]) -> list[str]:
    mapping = {
        "missing_disclaimer": "compliance_miss",
        "missing_black_swan_risks": "compliance_miss",
        "low_confidence_not_observe": "overconfident",
        "duplicate_rambling": "empty_flail",
        "missing_key_levels": "schema_error",
    }
    return sorted({mapping.get(v, "rule_flag") for v in violations})


def _digest_report(report: dict) -> dict:
    """裁剪给 judge 的报告摘要，控制 token 又保留关键证据。"""
    keys = [
        "direction", "confidence", "direction_score", "market_state", "weighted_scores",
        "key_findings", "key_levels", "risk_assessment", "risk_boundary",
        "black_swan_risks", "position_advice", "analysis", "disclaimer",
        "execution_mode", "forecast_mode", "confidence_calibration",
    ]
    digest = {k: report.get(k) for k in keys if k in report}
    pool = report.get("evidence_pool")
    if isinstance(pool, list):
        digest["evidence_pool_summary"] = [
            {
                "agent": e.get("agent"), "bias": e.get("bias"),
                "score": e.get("score"), "confidence": e.get("confidence"),
                "data_quality": e.get("data_quality"), "data_source": e.get("data_source"),
                "evidence": (e.get("evidence") or [])[:5],
            }
            for e in pool[:10] if isinstance(e, dict)
        ]
    return digest


def _judge(
    api_key: str, base_url: str, model: str, rubric_text: str, sample: dict, report: dict
) -> dict:
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)
    user_prompt = (
        f"【样本】id={sample['id']} type={sample['type']} symbol={sample['symbol']}\n"
        f"【用户问题】{sample['query']}\n"
        f"【压测重点】{sample.get('eval_focus')}\n"
        f"【人工预判应观察到的质量信号】（用于核对，不直接作为打分依据）\n"
        f"{json.dumps(sample.get('expected_quality_signals', []), ensure_ascii=False, indent=2)}\n\n"
        f"【评分细则】\n{rubric_text}\n\n"
        f"【系统输出报告摘要】\n{json.dumps(_digest_report(report), ensure_ascii=False, indent=2)[:12000]}\n\n"
        "请逐维度打分并给出失败模式标签（labels 可为空数组）。只输出 JSON。"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=1200,
    )
    content = resp.choices[0].message.content or "{}"
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        raise ValueError(f"judge 输出非 JSON: {content[:200]}")
    return json.loads(m.group(0))


def _looks_valid_scores(scores: dict) -> bool:
    return all(str(d) in scores and isinstance(scores.get(str(d)), int) for d in DIMENSIONS)


async def run_one_sample(client: httpx.AsyncClient, api: str, sample: dict) -> dict:
    """调用应用 → 轮询报告 → 返回服务端响应。"""
    url = f"{api}/api/analyze"
    resp = await client.post(url, json={"symbol": sample["symbol"], "query": sample["query"]})
    resp.raise_for_status()
    task_id = resp.json().get("task_id")
    deadline = time.time() + 180
    report_url = f"{api}/api/report/{task_id}"
    while time.time() < deadline:
        r = await client.get(report_url)
        data = r.json()
        status = data.get("status")
        if status == "done":
            return {"ok": True, "task_id": task_id, "report": data.get("report", {})}
        if status in ("expired",):
            return {"ok": False, "task_id": task_id, "error": f"report {status}"}
        await asyncio.sleep(2.0)
    return {"ok": False, "task_id": task_id, "error": "timeout polling report"}


def _apply_arbitration(result: dict, primary: dict, secondary: dict | None) -> dict:
    if secondary is None:
        return result
    conflict = [
        d for d in DIMENSIONS
        if abs(int(primary.get(d, 0)) - int(secondary.get(d, 0))) > 1
    ]
    result["judge_split"] = {"primary": primary, "secondary": secondary, "conflict_dims": conflict}
    if conflict:
        result["arbitrated"] = True  # 仲裁逻辑在 judge() 外层按需调用（可人工或二次慢思考）
    return result


async def evaluate_sample(
    http: httpx.AsyncClient, api: str, sample: dict, rubric_text: str, cfg: dict
) -> dict:
    run = await run_one_sample(http, api, sample)
    row: dict = {
        "id": sample["id"], "type": sample["type"], "symbol": sample["symbol"],
        "query": sample["query"], "eval_focus": sample.get("eval_focus", []),
    }
    if not run["ok"]:
        row.update({"run_error": run.get("error"), "D1": 1, "D2": 1, "D3": 1,
                    "D4": 1, "D5": 1, "D6": 1, "labels": ["empty_flail"], "hard": ["run_failed"]})
        return row

    report = run["report"] or {}
    violations = _rule_checks(report)
    row["hard"] = violations
    labels = _pick_labels(violations)

    try:
        primary = await asyncio.to_thread(
            _judge, cfg["primary_key"], cfg["primary_base"], cfg["primary_model"],
            rubric_text, sample, report,
        )
        if not _looks_valid_scores(primary):
            raise ValueError("judge 维度缺失或类型错误")
    except Exception as exc:  # judge 失败不能静默：标记并将该样本记为失败
        row.update({
            "judge_error": str(exc)[:300],
            "D1": 1, "D2": 1, "D3": 1, "D4": 1, "D5": 1, "D6": 1,
            "labels": ["empty_flail"], "task_id": run["task_id"],
        })
        return row

    secondary = None
    if cfg.get("second_key"):
        try:
            secondary = await asyncio.to_thread(
                _judge, cfg["second_key"], cfg["second_base"], cfg["second_model"],
                rubric_text, sample, report,
            )
        except Exception as exc:
            row["judge_second_error"] = str(exc)[:200]

    for d in DIMENSIONS:
        row[d] = int(primary.get(d, 1))
    row["labels"] = sorted(set(labels) | {str(x) for x in primary.get("labels", []) if str(x)})
    row["rationale"] = str(primary.get("rationale", ""))[:400]
    row["task_id"] = run["task_id"]
    row = _apply_arbitration(row, primary, secondary)
    return row


async def main() -> int:
    parser = argparse.ArgumentParser(description="CryptoSage 实战任务一评测脚本")
    parser.add_argument("--dataset", required=True, help="样本集 jsonl 路径")
    parser.add_argument("--out", default=str(REPO_ROOT / "evaluation" / "results"), help="结果输出目录")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="本地 CryptoSage API 地址")
    parser.add_argument("--workers", type=int, default=1, help="并发样本数（后端默认并发上限 4）")
    args = parser.parse_args()

    dataset = Path(args.dataset)
    if not dataset.exists():
        print(f"[错误] 找不到样本集: {dataset}", file=sys.stderr)
        return 2
    samples = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not samples:
        print("[错误] 样本集为空", file=sys.stderr)
        return 2

    rubric_text = Path(DEFAULT_RUBRIC).read_text(encoding="utf-8") if Path(DEFAULT_RUBRIC).exists() else "（未找到 rubric.md，请用 --rubric 指定）"

    primary_key = os.getenv("HY3_API_KEY", "")
    if not primary_key:
        print("[错误] 需要 HY3_API_KEY（.env 中配置）作为 judge Key", file=sys.stderr)
        return 2
    cfg = {
        "primary_key": primary_key,
        "primary_base": os.getenv("HY3_BASE_URL", "https://tokenhub.tencentmaas.com/v1"),
        "primary_model": os.getenv("EVAL_JUDGE_MODEL") or os.getenv("HY3_SLOW_MODEL") or "hy3-slow",
        "second_key": os.getenv("DEEPSEEK_API_KEY", "") or "",
        "second_base": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        "second_model": os.getenv("EVAL_JUDGE_SECOND_MODEL", "deepseek-v4-pro"),
    }

    out_dir = Path(args.out)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(max(1, args.workers))

    async def guarded(sample: dict) -> dict:
        async with sem:
            async with httpx.AsyncClient(timeout=30) as http:
                row = await evaluate_sample(http, args.api, sample, rubric_text, cfg)
        if row.get("task_id"):
            raw = {}
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    r = await http.get(f"{args.api}/api/report/{row['task_id']}")
                    raw = r.json()
            except Exception:
                raw = {"fetch_error": True}
            (out_dir / "raw" / f"{row['id']}.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return row

    rows = await asyncio.gather(*[guarded(s) for s in samples])

    (out_dir / "scores.json").write_text(
        json.dumps({"meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api": args.api, "model": cfg["primary_model"], "dataset": str(dataset),
            "sample_count": len(samples),
        }, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 打印汇总
    header = f"{'id':<7}{'type':<11}{'D1':>3}{'D2':>3}{'D3':>3}{'D4':>3}{'D5':>3}{'D6':>3}{'SUM':>5}  labels"
    print(header)
    print("-" * len(header))
    for r in rows:
        if any(str(d) in r and isinstance(r.get(str(d)), int) for d in DIMENSIONS):
            s = sum(int(r.get(d, 1)) for d in DIMENSIONS[:5])
            print(f"{r['id']:<7}{r['type']:<11}"
                  f"{int(r.get('D1',1)):>3}{int(r.get('D2',1)):>3}{int(r.get('D3',1)):>3}"
                  f"{int(r.get('D4',1)):>3}{int(r.get('D5',1)):>3}{int(r.get('D6',1)):>3}{s:>5}  {','.join(r.get('labels', []) or [])}")
        else:
            print(f"{r['id']:<7}{r['type']:<11}  (run_error={r.get('run_error') or r.get('judge_error')})")
    print(f"\n完成。输出目录: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
