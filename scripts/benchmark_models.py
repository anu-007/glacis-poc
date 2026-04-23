"""
LLM Model Benchmark: gpt-4o-mini vs gpt-4.1-nano
==================================================

Measures per model, per test case, per run:
  - Accuracy       : correct classification vs ground truth label
  - Latency        : wall-clock time per API call (seconds)
  - Tokens         : prompt, completion, and cached tokens
  - Caching        : cached token count and cost savings vs no-cache
  - Cost           : input / output / total cost in USD

Usage:
    python -m scripts.benchmark_models
    python -m scripts.benchmark_models --runs 5
    python -m scripts.benchmark_models --runs 3 --out results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens) — source: platform.openai.com/docs/pricing
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {
        "input":        0.15,    # non-cached input tokens
        "cached_input": 0.08,   # cached input tokens (50 % discount)
        "output":       0.60,
    },
    "gpt-5.4-nano": {
        "input":        0.20,    # non-cached input tokens
        "cached_input": 0.02,   # cached input tokens (75 % discount)
        "output":       1.25,
    },
}

# ---------------------------------------------------------------------------
# Ground-truth test cases
# ---------------------------------------------------------------------------

TEST_CASES: list[dict[str, Any]] = [
    # ── SHIPMENT: standard fields ────────────────────────────────────────────
    {
        "name": "shipment_standard",
        "expected": "SHIPMENT",
        "payload": {
            "vendorId": "fedex-corp",
            "trackingNumber": "FX-998877",
            "status": "TRANSIT",
            "timestamp": "2024-06-01T12:00:00Z",
        },
    },
    {
        "name": "shipment_delivered_extra_fields",
        "expected": "SHIPMENT",
        "payload": {
            "vendorId": "dhl-express",
            "trackingNumber": "DHL-20240601",
            "status": "DELIVERED",
            "timestamp": "2024-06-05T18:45:00Z",
            "signedBy": "John Doe",          # extra — must be dropped
        },
    },
    {
        "name": "shipment_exception",
        "expected": "SHIPMENT",
        "payload": {
            "vendorId": "carrier-x",
            "trackingNumber": "CX-20240601",
            "status": "EXCEPTION",
            "timestamp": "2024-06-03T09:15:00Z",
            "reason": "address_not_found",   # extra — must be dropped
            "retryCount": 2,                 # extra — must be dropped
        },
    },
    {
        "name": "shipment_snake_case_status_synonym",
        "expected": "SHIPMENT",
        "payload": {
            "vendor_id": "acme-logistics",         # maps to vendorId
            "tracking_number": "ACME-5599",        # maps to trackingNumber
            "ship_status": "in_transit",           # synonym → TRANSIT
            "shipped_at": "2024-06-01T08:00:00Z",  # maps to timestamp
            "origin_city": "Mumbai",               # extra — must be dropped
        },
    },
    {
        "name": "shipment_unmappable_status",
        "expected": "UNCLASSIFIED",
        "payload": {
            "vendorId": "carrier-x",
            "trackingNumber": "CX-12345",
            "status": "pending_pickup",      # cannot map → UNCLASSIFIED
            "scheduledDate": "2024-06-10",
        },
    },
    {
        "name": "shipment_missing_tracking_number",
        "expected": "UNCLASSIFIED",
        "payload": {
            "vendorId": "mystery-vendor",
            "status": "TRANSIT",
            "timestamp": "2024-06-01T00:00:00Z",
            "shipmentRef": "REF-ONLY",       # no trackingNumber → UNCLASSIFIED
        },
    },
    # ── INVOICE: standard fields ─────────────────────────────────────────────
    {
        "name": "invoice_standard",
        "expected": "INVOICE",
        "payload": {
            "vendorId": "supplier-99",
            "invoiceId": "INV-2024-007",
            "amount": 4850.75,
            "currency": "USD",
        },
    },
    {
        "name": "invoice_alt_fields_string_amount_lowercase_currency",
        "expected": "INVOICE",
        "payload": {
            "vendor_id": "euro-parts-gmbh",  # maps to vendorId
            "invoice_number": "EP-20240601", # maps to invoiceId
            "total": "1299.99",              # string → cast to float
            "currency_code": "eur",          # lowercase → EUR
            "due_date": "2024-07-15",        # extra — must be dropped
            "tax_rate": 0.19,                # extra — must be dropped
        },
    },
    {
        "name": "invoice_non_usd_currency",
        "expected": "INVOICE",
        "payload": {
            "vendorId": "india-supplier",
            "invoiceId": "IS-INV-888",
            "amount": 75000.00,
            "currency": "INR",
            "gstNumber": "29ABCDE1234F1Z5",  # extra — must be dropped
        },
    },
    # ── UNCLASSIFIED ─────────────────────────────────────────────────────────
    {
        "name": "unclassified_order_event",
        "expected": "UNCLASSIFIED",
        "payload": {
            "eventType": "order_created",
            "orderId": "ORD-9001",
            "customerId": "CUST-77",
            "total": 320.00,
            "items": [{"sku": "SKU-A", "qty": 3}],
        },
    },
    {
        "name": "unclassified_heartbeat",
        "expected": "UNCLASSIFIED",
        "payload": {
            "type": "heartbeat",
            "source": "monitoring-agent",
            "ts": 1717200000,
        },
    },
]

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CallResult:
    model: str
    test_name: str
    run: int
    expected: str
    got: str
    correct: bool
    schema_valid: bool
    latency_s: float
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    cache_savings_usd: float
    error: str | None = None


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _call_model(
    client: Any,
    model: str,
    payload: dict[str, Any],
    system_prompt: str,
    few_shot: list[dict],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Single API call; returns (output_dict, usage_dict)."""
    start = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            *few_shot,
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    latency = time.perf_counter() - start

    content = response.choices[0].message.content or "{}"
    output = json.loads(content)

    usage = response.usage
    print("usage", usage)
    cached = 0
    # gpt-4o / gpt-4.1 family: cached tokens under prompt_tokens_details.cached_tokens
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details:
        cached = getattr(prompt_details, "cached_tokens", 0) or 0
    # gpt-5 family: cached tokens under input_tokens_details.cached_tokens
    if not cached:
        input_details = getattr(usage, "input_tokens_details", None)
        if input_details:
            cached = getattr(input_details, "cached_tokens", 0) or 0

    return output, {
        "latency_s": latency,
        "prompt_tokens": usage.prompt_tokens,
        "cached_tokens": cached,
        "completion_tokens": usage.completion_tokens,
    }


def _calculate_cost(
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
) -> tuple[float, float, float, float]:
    """Return (input_cost, output_cost, total_cost, cache_savings_usd)."""
    p = MODEL_PRICING[model]
    non_cached = prompt_tokens - cached_tokens
    input_cost = (non_cached * p["input"] + cached_tokens * p["cached_input"]) / 1_000_000
    output_cost = completion_tokens * p["output"] / 1_000_000
    # Savings = what it would have cost without caching
    full_input_cost = prompt_tokens * p["input"] / 1_000_000
    savings = full_input_cost - input_cost
    return input_cost, output_cost, input_cost + output_cost, savings


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(api_key: str, runs: int = 3) -> list[CallResult]:
    """Run all test cases against all models for `runs` iterations each."""
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("openai package not installed.  Run: pip install openai")

    from app.llm import SYSTEM_PROMPT, FEW_SHOT_MESSAGES
    from app.schemas import parse_llm_output

    client = OpenAI(api_key=api_key)
    results: list[CallResult] = []

    for model in MODEL_PRICING:
        print(f"\n{'=' * 65}")
        print(f"  Model : {model}   ({runs} run(s) × {len(TEST_CASES)} cases)")
        print(f"{'=' * 65}")

        for run_idx in range(runs):
            print(f"\n  ── Run {run_idx + 1}/{runs} ──")
            for tc in TEST_CASES:
                label = tc["name"].ljust(42)
                print(f"    {label}", end="", flush=True)

                error: str | None = None
                got = "ERROR"
                schema_valid = False
                usage_data: dict[str, Any] = {}

                try:
                    output, usage_data = _call_model(
                        client, model, tc["payload"], SYSTEM_PROMPT, FEW_SHOT_MESSAGES
                    )
                    got = output.get("type", "ERROR")
                    try:
                        parse_llm_output(output)
                        schema_valid = True
                    except Exception as exc:
                        error = f"Schema: {exc}"
                except Exception as exc:
                    error = str(exc)

                pt  = usage_data.get("prompt_tokens", 0)
                ct  = usage_data.get("cached_tokens", 0)
                ot  = usage_data.get("completion_tokens", 0)
                lat = usage_data.get("latency_s", 0.0)

                in_c, out_c, total_c, savings = _calculate_cost(model, pt, ct, ot)

                result = CallResult(
                    model=model,
                    test_name=tc["name"],
                    run=run_idx + 1,
                    expected=tc["expected"],
                    got=got,
                    correct=got == tc["expected"],
                    schema_valid=schema_valid,
                    latency_s=round(lat, 3),
                    prompt_tokens=pt,
                    cached_tokens=ct,
                    completion_tokens=ot,
                    input_cost_usd=round(in_c, 7),
                    output_cost_usd=round(out_c, 7),
                    total_cost_usd=round(total_c, 7),
                    cache_savings_usd=round(savings, 7),
                    error=error,
                )
                results.append(result)

                status    = "✅" if result.correct else "❌"
                cache_str = f"cached={ct}tok" if ct else "cold       "
                print(
                    f"{status} {got:<16} {lat:.2f}s  {cache_str}  ${total_c:.6f}"
                )

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _model_stats(results: list[CallResult], model: str) -> dict[str, Any]:
    rows = [r for r in results if r.model == model]
    if not rows:
        return {}
    correct      = [r for r in rows if r.correct]
    cached_rows  = [r for r in rows if r.cached_tokens > 0]
    latencies    = [r.latency_s for r in rows]
    costs        = [r.total_cost_usd for r in rows]
    savings      = [r.cache_savings_usd for r in rows]
    prompt_toks  = [r.prompt_tokens for r in rows]
    cached_toks  = [r.cached_tokens for r in rows]
    comp_toks    = [r.completion_tokens for r in rows]
    return {
        "accuracy_pct":           round(100 * len(correct) / len(rows), 1),
        "avg_latency_s":          round(statistics.mean(latencies), 3),
        "p50_latency_s":          round(statistics.median(latencies), 3),
        "avg_prompt_tokens":      round(statistics.mean(prompt_toks)),
        "avg_cached_tokens":      round(statistics.mean(cached_toks)),
        "avg_completion_tokens":  round(statistics.mean(comp_toks)),
        "cache_hit_rate_pct":     round(100 * len(cached_rows) / len(rows), 1),
        "avg_cost_per_event_usd": round(statistics.mean(costs), 7),
        "cost_per_1k_events_usd": round(statistics.mean(costs) * 1_000, 4),
        "total_cost_usd":         round(sum(costs), 6),
        "total_cache_savings_usd":round(sum(savings), 6),
    }


def print_report(results: list[CallResult]) -> None:
    models     = list(MODEL_PRICING.keys())
    stats      = {m: _model_stats(results, m) for m in models}
    test_names = list(dict.fromkeys(r.test_name for r in results))
    W = 65 + 24 * (len(models) - 1)

    print(f"\n\n{'=' * W}")
    print("  BENCHMARK REPORT")
    print(f"{'=' * W}")

    # ── Per-test accuracy + latency matrix ───────────────────────────────────
    print(f"\n  {'Test Case':<44}", end="")
    for m in models:
        print(f"  {m:<24}", end="")
    print(f"\n  {'─' * 44}", end="")
    for _ in models:
        print(f"  {'─' * 24}", end="")
    print()

    for name in test_names:
        print(f"  {name:<44}", end="")
        for m in models:
            rows = [r for r in results if r.model == m and r.test_name == name]
            if not rows:
                print(f"  {'n/a':<24}", end="")
                continue
            pct = 100 * sum(1 for r in rows if r.correct) / len(rows)
            lat = statistics.mean(r.latency_s for r in rows)
            mark = "✅" if pct == 100 else ("⚠️ " if pct > 0 else "❌")
            print(f"  {mark} {pct:3.0f}%  {lat:.2f}s           ", end="")
        print()

    # ── Summary table ────────────────────────────────────────────────────────
    metric_labels = [
        ("accuracy_pct",            "Accuracy (%)"),
        ("avg_latency_s",           "Avg Latency (s)"),
        ("p50_latency_s",           "P50 Latency (s)"),
        ("avg_prompt_tokens",       "Avg Prompt Tokens"),
        ("avg_cached_tokens",       "Avg Cached Tokens"),
        ("avg_completion_tokens",   "Avg Completion Tokens"),
        ("cache_hit_rate_pct",      "Cache Hit Rate (%)"),
        ("avg_cost_per_event_usd",  "Avg Cost / Event ($)"),
        ("cost_per_1k_events_usd",  "Cost / 1K Events ($)"),
        ("total_cost_usd",          "Total Benchmark Cost ($)"),
        ("total_cache_savings_usd", "Total Cache Savings ($)"),
    ]

    print(f"\n  {'─' * W}")
    print(f"  {'Metric':<36}", end="")
    for m in models:
        print(f"  {m:<24}", end="")
    print(f"\n  {'─' * W}")

    for key, label in metric_labels:
        print(f"  {label:<36}", end="")
        for m in models:
            val = stats[m].get(key, "n/a")
            fmt = f"{val:<24.4f}" if isinstance(val, float) else f"{val!s:<24}"
            print(f"  {fmt}", end="")
        print()

    print(f"\n{'═' * W}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark gpt-4o-mini vs gpt-4.1-nano on accuracy, latency, tokens, caching, and cost."
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Number of runs per model per test case (default: 3). "
             "Multiple runs reveal prompt caching — run 1 is cold, runs 2+ hit cache.",
    )
    parser.add_argument(
        "--out", type=str, default=None,
        help="Optional path to save raw results as JSON (e.g. results.json).",
    )
    args = parser.parse_args()

    from app.config import settings
    api_key = settings.openai_api_key
    if not api_key:
        sys.exit("OPENAI_API_KEY is not set. Add it to your .env file.")

    total_calls = len(MODEL_PRICING) * len(TEST_CASES) * args.runs
    print(f"\nModels      : {', '.join(MODEL_PRICING)}")
    print(f"Test cases  : {len(TEST_CASES)}")
    print(f"Runs        : {args.runs}  (run 1 = cold cache; run 2+ = warm cache)")
    print(f"Total calls : {total_calls}")

    results = run_benchmark(api_key=api_key, runs=args.runs)
    print_report(results)

    if args.out:
        with open(args.out, "w") as fh:
            json.dump([asdict(r) for r in results], fh, indent=2)
        print(f"Raw results saved → {args.out}\n")


if __name__ == "__main__":
    main()


#python -m scripts.benchmark_models --runs 3 --out results.json