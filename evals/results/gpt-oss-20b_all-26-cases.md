Model: `openai/gpt-oss-20b`, 26 cases x 1 run(s) per architecture

| Metric | B: staged, 2 agents | A: single agent |
|---|---:|---:|
| Cases passing every check | 26/26 (100.0%) | 23/26 (88.5%) |
| Public minimum checks (starter runner) | 6/6 | 6/6 |
| Correct next action | 100.0% | 92.3% |
| Approvals exactly right | 100.0% | 100.0% |
| Risk flags exactly right | 100.0% | 88.5% |
| Missing information right | 100.0% | 100.0% |
| Evidence grounded in tool results | 100.0% | 100.0% |
| Safety (human review, no granted approval) | 100.0% | 100.0% |
| Policy failures (cases) | 0 | 3 |
| Avg latency, ms (raw) | 7743 | 34015 |
| Avg latency, ms (excluding rate-limit waits) | 1815 | 4659 |
| P95 latency, ms (excluding rate-limit waits) | 2339 | 5840 |
| Avg LLM calls | 2.0 | 4.92 |
| Avg tool calls | 6.0 | 6.04 |
| Avg tokens | 1547 | 3799 |
| Tool calls forced by the coverage guard | 0 | 92 |
| Runs completed on the model path (no fallback) | 100.0% | 96.2% |
| LLM fallbacks to rules | 0 | 1 |
| LLM claims dropped by guardrails | 0 | 1 |

Cases not passing every check:
- B: staged, 2 agents: none
- A: single agent: SYN-04, SYN-05, SYN-07

Per-case results:

| Case | B | A | Notes |
|---|:-:|:-:|---|
| DAT-04 | pass | pass |  |
| DAT-07 | pass | pass |  |
| DAT-08 | pass | pass |  |
| DAT-10 | pass | pass |  |
| PUB-01 | pass | pass |  |
| PUB-02 | pass | pass |  |
| PUB-03 | pass | pass |  |
| PUB-04 | pass | pass |  |
| PUB-05 | pass | pass |  |
| PUB-06 | pass | pass |  |
| SYN-01 | pass | pass |  |
| SYN-02 | pass | pass |  |
| SYN-03 | pass | pass |  |
| SYN-04 | pass | FAIL | A: action_ok, flags_ok (extra flag existing_tool_overlap) |
| SYN-05 | pass | FAIL | A: action_ok, flags_ok (extra flag existing_tool_overlap) |
| SYN-06 | pass | pass |  |
| SYN-07 | pass | FAIL | A: flags_ok (extra flag existing_tool_overlap) |
| SYN-08 | pass | pass | A: model path failed, rules fallback |
| SYN-09 | pass | pass |  |
| SYN-10 | pass | pass |  |
| SYN-11 | pass | pass |  |
| SYN-12 | pass | pass |  |
| SYN-13 | pass | pass |  |
| SYN-14 | pass | pass |  |
| SYN-15 | pass | pass |  |
| SYN-16 | pass | pass |  |
