Model: `openai/gpt-oss-120b`, 7 cases x 1 run(s) per architecture

| Metric | B: staged, 2 agents | A: single agent |
|---|---:|---:|
| Cases passing every check | 7/7 (100.0%) | 7/7 (100.0%) |
| Public minimum checks (starter runner) | 6/6 | 6/6 |
| Correct next action | 100.0% | 100.0% |
| Approvals exactly right | 100.0% | 100.0% |
| Risk flags exactly right | 100.0% | 100.0% |
| Missing information right | 100.0% | 100.0% |
| Evidence grounded in tool results | 100.0% | 100.0% |
| Safety (human review, no granted approval) | 100.0% | 100.0% |
| Policy failures (cases) | 0 | 0 |
| Avg latency, ms (raw) | 4167 | 56261 |
| Avg latency, ms (excluding rate-limit waits) | 2179 | 7110 |
| P95 latency, ms (excluding rate-limit waits) | 2369 | 7318 |
| Avg LLM calls | 2.0 | 8.0 |
| Avg tool calls | 6.0 | 6.0 |
| Avg tokens | 1600 | 8298 |
| Tool calls forced by the coverage guard | 0 | 0 |
| Runs completed on the model path (no fallback) | 100.0% | 100.0% |
| LLM fallbacks to rules | 0 | 0 |
| LLM claims dropped by guardrails | 0 | 0 |

Cases not passing every check:
- B: staged, 2 agents: none
- A: single agent: none
