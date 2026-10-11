Model: `qwen/qwen3.8-27b`, 26 cases x 1 run(s) per architecture

| Metric | Rules only (no LLM) |
|---|---:|
| Cases passing every check | 25/26 (96.2%) |
| Public minimum checks (starter runner) | 6/6 |
| Correct next action | 96.2% |
| Approvals exactly right | 100.0% |
| Risk flags exactly right | 100.0% |
| Missing information right | 100.0% |
| Evidence grounded in tool results | 100.0% |
| Safety (human review, no granted approval) | 100.0% |
| Policy failures (cases) | 0 |
| Avg latency, ms (raw) | 104 |
| Avg latency, ms (excluding rate-limit waits) | 104 |
| P95 latency, ms (excluding rate-limit waits) | 29 |
| Avg LLM calls | 0.0 |
| Avg tool calls | 6.0 |
| Avg tokens | 0 |
| Tool calls forced by the coverage guard | 0 |
| LLM fallbacks to rules | 0 |
| LLM claims dropped by guardrails | 0 |

Cases not passing every check:
- Rules only (no LLM): DAT-08
