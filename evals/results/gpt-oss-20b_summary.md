Model: `openai/gpt-oss-20b`, 16 cases x 1 run(s) per architecture

| Metric | B: staged, 2 agents | A: single agent |
|---|---:|---:|
| Cases passing every check | 16/16 (100.0%) | 16/16 (100.0%) |
| Public minimum checks (starter runner) | 6/6 | 6/6 |
| Correct next action | 100.0% | 100.0% |
| Approvals exactly right | 100.0% | 100.0% |
| Risk flags exactly right | 100.0% | 100.0% |
| Missing information right | 100.0% | 100.0% |
| Evidence grounded in tool results | 100.0% | 100.0% |
| Safety (human review, no granted approval) | 100.0% | 100.0% |
| Policy failures (cases) | 0 | 0 |
| Avg latency, ms (raw) | 7996 | 32472 |
| Avg latency, ms (excluding rate-limit waits) | 1807 | 4820 |
| P95 latency, ms (excluding rate-limit waits) | 2339 | 5952 |
| Avg LLM calls | 2.0 | 4.88 |
| Avg tool calls | 6.0 | 6.06 |
| Avg tokens | 1558 | 3975 |
| Tool calls forced by the coverage guard | 0 | 54 |
| Runs completed on the model path (no fallback) | 100.0% | 93.8% |
| LLM fallbacks to rules | 0 | 1 |
| LLM claims dropped by guardrails | 0 | 1 |

Cases not passing every check:
- B: staged, 2 agents: none
- A: single agent: none
