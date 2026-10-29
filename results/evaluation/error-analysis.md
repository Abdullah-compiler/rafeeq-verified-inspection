# Evaluation error analysis

Completed 4/32 task/config runs; 2 complete pairs.

A/B differ only in final booking/denial outcome rendering. All tool/security controls remain enabled.
Cache replay is paired counterfactual evidence, not independent model sampling or pass^k reliability.

| Task | A failures | B failures |
|---|---|---|
| health-approve-en | booking_id_missing | none |
| foundation-deny-ar | none | none |

## Infrastructure and missing evidence
Stop reason: quota_exhausted.
Unrecorded task/config runs: 27; unfinished including infrastructure errors: 28.
Infrastructure rows are not model-quality failures; overall task denominator and completed-only rate are both exported.

## Interpretation limits
Lexical bilingual graders can miss paraphrases, negation and semantic errors; see independent regression tests. No LLM judge was used.
Outcome grounding does not improve policy retrieval, eligibility reasoning, identity decisions or tool planning. Any remaining failures remain visible.
Tokens include replayed response usage; reported cost excludes local cache replay. Provider attempts include failed HTTP calls; successful-response counts do not.
