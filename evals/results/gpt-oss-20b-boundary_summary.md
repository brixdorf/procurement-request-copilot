Model: `openai/gpt-oss-20b`, 10 cases x 1 run(s) per architecture

| Metric | B: staged, 2 agents | A: single agent |
|---|---:|---:|
| Cases passing every check | 10/10 (100.0%) | 7/10 (70.0%) |
| Public minimum checks (starter runner) | 0/0 | 0/0 |
| Correct next action | 100.0% | 80.0% |
| Approvals exactly right | 100.0% | 100.0% |
| Risk flags exactly right | 100.0% | 70.0% |
| Missing information right | 100.0% | 100.0% |
| Evidence grounded in tool results | 100.0% | 100.0% |
| Safety (human review, no granted approval) | 100.0% | 100.0% |
| Policy failures (cases) | 0 | 3 |
| Avg latency, ms (raw) | 7338 | 36483 |
| Avg latency, ms (excluding rate-limit waits) | 1827 | 4400 |
| P95 latency, ms (excluding rate-limit waits) | 2113 | 5125 |
| Avg LLM calls | 2.0 | 5.0 |
| Avg tool calls | 6.0 | 6.0 |
| Avg tokens | 1528 | 3518 |
| Tool calls forced by the coverage guard | 0 | 38 |
| Runs completed on the model path (no fallback) | 100.0% | 100.0% |
| LLM fallbacks to rules | 0 | 0 |
| LLM claims dropped by guardrails | 0 | 0 |

Cases not passing every check:
- B: staged, 2 agents: none
- A: single agent: SYN-04, SYN-05, SYN-07
