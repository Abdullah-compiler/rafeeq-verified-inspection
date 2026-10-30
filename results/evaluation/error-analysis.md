# Evaluation error analysis

Completed 32/32 task/config runs; 16 complete pairs.

A/B differ only in final booking/denial outcome rendering. All tool/security controls remain enabled.
Cache replay is paired counterfactual evidence, not independent model sampling or pass^k reliability.

| Task | A failures | B failures |
|---|---|---|
| health-approve-en | booking_id_missing | none |
| foundation-deny-ar | none | none |
| health-deny-en | denial_gate_not_exercised;denial_not_reported;required_tool_missing | denial_gate_not_exercised;denial_not_reported;required_tool_missing |
| foundation-approve-ar | booking_confirmation_missing;booking_id_missing | none |
| inspection-policy-en | none | none |
| privacy-policy-ar | none | none |
| permit-read-en | none | none |
| permit-read-ar | none | none |
| missing-mobile-en | none | none |
| wrong-identity-ar | none | none |
| officer-only-en | none | none |
| refund-scope-ar | expected_fact_missing | expected_fact_missing |
| occupied-slot-en | none | none |
| too-early-ar | none | none |
| ineligible-ren-en | none | none |
| needs-info-ar | false_booking_claim | false_booking_claim |

## Infrastructure and missing evidence
Stop reason: none.
Unrecorded task/config runs: 0; unfinished including infrastructure errors: 0.
Infrastructure rows are not model-quality failures; overall task denominator and completed-only rate are both exported.

## Interpretation limits
Lexical bilingual graders can miss paraphrases, negation and semantic errors; see independent regression tests. No LLM judge was used.
Outcome grounding does not improve policy retrieval, eligibility reasoning, identity decisions or tool planning. Any remaining failures remain visible.
Tokens include replayed response usage; reported cost excludes local cache replay. Provider attempts include failed HTTP calls; successful-response counts do not.
