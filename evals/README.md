# Evaluation

Two runners share the same adapter, `src.solution.handle_request(request_id, architecture)`.

## Starter runner: six public cases

```bash
python evals/run_public_evals.py --architecture single
python evals/run_public_evals.py --architecture staged
```

It validates the `ProcurementDecision` schema, checks the minimum expectations in `public_cases.json`, measures latency and writes `evals/results_<architecture>.csv` (git-ignored scratch output).

## Comparison harness: 26 cases, three architectures

```bash
python evals/run_comparison.py                                   # rules, single, staged on all 26 cases
python evals/run_comparison.py --architectures staged single --kinds public dataset
python evals/run_comparison.py --cases SYN-08 SYN-12 --tag injection --model openai/gpt-oss-120b
python evals/aggregate_results.py --tags gpt-oss-20b gpt-oss-20b-boundary --out gpt-oss-20b_all-26-cases
```

- **Cases** are in `extended_cases.json`: the 6 public cases, the 4 other dataset requests, and 16 synthetic edge cases with fixtures (threshold and date boundaries, injection inside vendor notes, unknown vendor, malformed and unavailable vendor-risk responses, unknown requester).
- **Ground truth** is hand-written from `data/procurement_policy.md`: acceptable actions, the exact approval set, required and optional risk flags, missing-information keywords.
- **Per-run checks**: correct action, exact approvals, exact flags, missing information, evidence grounded in called tools, safety (human review, no granted approval).
- **Telemetry**: LLM calls, tool calls, tokens, latency raw and excluding provider rate-limit waits, forced tool calls, fallbacks, dropped claims.
- **Outputs** in `results/`: `<tag>_cases.csv`, `<tag>_traces.json` (every tool and model call), `<tag>_summary.md/json`.
- A run whose model path fails falls back to the deterministic result and is counted as a fallback. If the provider's daily quota is exhausted the harness stops that architecture rather than report rules-only numbers as model results.

## Included results

| Tag | Model | Cases | Architectures |
|---|---|---|---|
| `gpt-oss-20b` | openai/gpt-oss-20b | 16 (10 requests + 6 LLM-relevant synthetic) | staged, single |
| `gpt-oss-20b-boundary` | openai/gpt-oss-20b | 10 (threshold, date, missing-field synthetic) | staged, single |
| `gpt-oss-20b_all-26-cases` | openai/gpt-oss-20b | 26 (merge of the two above) | staged, single |
| `gpt-oss-120b-replicate` | openai/gpt-oss-120b | 7 (public cases + DAT-08) | staged, single |
| `rules-all-26-cases` | none | 26 | rules only |

Batches exist because the free model tier caps each model at 200,000 tokens per day.

## Limits

Passing these checks is not a guarantee of production quality. Ground truth reflects one reader's interpretation of the policy, runs are single-shot at temperature 0, and the case set is small.
