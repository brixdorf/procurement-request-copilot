# Architecture Decision Memo

## Decision

Ship **Architecture B**: code collects evidence in a fixed order, then two narrow model stages (Evidence Analyst, Recommendation Reviewer) with a typed handoff. A human approves everything.

## Evidence

Same 26 cases for both, model `openai/gpt-oss-20b`, temperature 0, one run each.

| Metric | Single agent (A) | Staged, 2 agents (B) |
|---|---:|---:|
| Cases passing every check | 23/26 | 26/26 |
| Correct next action | 92.3% | 100% |
| Risk flags exactly right | 88.5% | 100% |
| Approvals exactly right | 100% | 100% |
| Avg LLM calls | 4.9 | 2.0 |
| Avg tool calls | 6.0 | 6.0 |
| Avg tokens | 3,799 | 1,547 |
| Avg latency excluding rate-limit waits | 4.7 s | 1.8 s |
| Tool calls forced by the coverage guard | 92 | 0 |
| Model-path failures | 1 | 0 |

A replicate on `gpt-oss-120b` (7 cases) passed both architectures but cost A 8.0 LLM calls against B's 2.0.

## Trade-offs

Approvals, thresholds, date checks and review triggers live in deterministic code, so both architectures get them right. The model only judges overlap with existing tools, picks among actions the policy allows, and writes the wording.

B gained cost, speed and consistency: a fixed workflow cannot skip evidence, and a focused analyst prompt judged "add-on to an existing product" correctly where A, juggling evidence, policy and wording in one context, called three add-ons duplicates.

B gave up runtime flexibility. It always pays two calls, even for a clear-cut request, and a new evidence path means changing code. In 26 cases that flexibility was never needed.

## Risks and limitations

- The ground truth is mine, written from the policy. An independent reviewer and a larger set are needed.
- Single runs; run-to-run variance is unmeasured. The free tier's daily token cap limited repeats and the second-model replicate.
- I did not ablate B against "collect, then one structured call". The second agent's marginal value is unproven, though it isolates untrusted text from the stage that writes the recommendation.
- Injection detection is pattern based plus a quoted-text check. A paraphrased attack could go unflagged, but it cannot change approvals, flags or allowed actions.
- Not built: queue, idempotency, durable state, circuit breaker. Needed before multi-user use.
- Open policy questions: Legal review for cross-region data on an approved vendor, and whether `internal_documents` counts as confidential.

## Why this is the right MVP

The workflow is the same for every request: load, check budget, search catalog, check registry, check vendor risk, apply rules. When the path never varies, letting a model choose it adds calls, tokens and failure modes without improving any outcome. B keeps the model where it helps (interpretation and explanation) and keeps humans where it matters (approvals and exceptions). Guardrails ensure a failing, slow or manipulated model degrades to a safe, deterministic recommendation that still routes to human review.
