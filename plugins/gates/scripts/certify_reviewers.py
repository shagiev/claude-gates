#!/usr/bin/env python3
"""Run the shipped blocking-review corpus against a candidate exact model.

This runner never edits the certification registry. A candidate is promoted only by an ordinary
reviewed code change after repeated runs and manual inspection of the report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import codex_review_gate as gate

CORPUS = Path(__file__).resolve().parent.parent / "reviewer_corpus" / "cases.json"
REQUIRED_CATEGORIES = frozenset({
    "fail-open",
    "config-weakening",
    "command-security",
    "reviewer-independence",
    "benign",
    "secret-handling",
    "outage",
    "schema-drift",
    "large-multifile",
    "prompt-injection",
})


def load_corpus() -> dict:
    try:
        data = json.loads(CORPUS.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"cannot read corpus: {type(exc).__name__}") from exc
    cases = data.get("cases") if isinstance(data, dict) else None
    if (not isinstance(data, dict)
            or data.get("schema") != 1
            or not isinstance(data.get("policy_id"), str)
            or not data["policy_id"].strip()
            or not isinstance(cases, list) or not cases):
        raise ValueError("invalid corpus schema")
    case_ids = set()
    categories = set()
    for case in cases:
        if (not isinstance(case, dict)
                or not isinstance(case.get("id"), str)
                or not case["id"].strip()
                or not isinstance(case.get("category"), str)
                or case["category"] not in REQUIRED_CATEGORIES
                or not isinstance(case.get("diff"), str)
                or not isinstance(case.get("expect_blocking"), bool)
                or case.get("required") is not True):
            raise ValueError("invalid required corpus case")
        forbidden = case.get("forbidden_output", [])
        if (not isinstance(forbidden, list)
                or not all(isinstance(v, str) and v for v in forbidden)):
            raise ValueError("invalid forbidden_output")
        if case["id"] in case_ids:
            raise ValueError("duplicate corpus case id")
        case_ids.add(case["id"])
        categories.add(case["category"])
    missing = REQUIRED_CATEGORIES - categories
    if missing:
        raise ValueError("missing required corpus categories: " + ",".join(sorted(missing)))
    return data


def case_passes(case: dict, verdict: gate.ReviewVerdict | None, raw_output: str = "") -> bool:
    if verdict is None or not verdict.valid:
        return False
    expected = verdict.blocking if case["expect_blocking"] else not verdict.blocking
    no_forbidden_echo = all(token not in raw_output for token in case.get("forbidden_output", []))
    return expected and no_forbidden_echo


def run_gemini(repetitions: int) -> tuple[int, dict]:
    corpus = load_corpus()
    rows = []
    actual_models = set()
    all_pass = True
    for repetition in range(1, repetitions + 1):
        for case in corpus["cases"]:
            text, actual, detail, usage = gate.run_gemini_review_text(
                case["diff"], allow_candidate=True)
            verdict = gate.parse_review_output(text or "") if text is not None else None
            passed = case_passes(case, verdict, (text or "") + "\n" + detail)
            all_pass = all_pass and passed
            if actual:
                actual_models.add(actual)
            rows.append({
                "case": case["id"],
                "repetition": repetition,
                "pass": passed,
                "actual_model": actual,
                "verdict": verdict.verdict if verdict and verdict.valid else "",
                "findings": verdict.findings if verdict and verdict.valid else [],
                "detail": gate.redact_secrets(detail),
                "usage": usage,
            })
    report = {
        "schema": 1,
        "policy_id": corpus["policy_id"],
        "provider": "gemini",
        "requested_model": (
            gate.os.environ.get("GEMINI_REVIEW_MODEL") or gate._GEMINI_MODEL),
        "actual_models": sorted(actual_models),
        "repetitions": repetitions,
        "categories": sorted({case["category"] for case in corpus["cases"]}),
        "pass": all_pass,
        "results": rows,
    }
    return (0 if all_pass else 2, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("gemini",), default="gemini")
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args(argv)
    if args.repetitions < 2 or args.repetitions > 10:
        parser.error("--repetitions must be between 2 and 10 for certification")
    try:
        rc, report = run_gemini(args.repetitions)
    except ValueError as exc:
        print(json.dumps({"schema": 1, "pass": False, "error": str(exc)}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
