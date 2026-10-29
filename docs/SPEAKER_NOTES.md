# Eight-minute presentation notes

**Before presenting:** confirm the evaluation status in `results/evaluation/summary.json`. If `complete` is false, the full assessment requirement is unfinished. Do not present the partial sample as a final pass rate. The slide generator adds a draft footer while incomplete.

## 0:00–1:00 — Problem and user (slides 1–2)

“I chose one short workflow rather than expose every municipal action. A verified permit holder needs a real inspection slot. The model can interpret Arabic or English and select tools, but it cannot grant itself authority to write. Success means an actual database booking, not a confident sentence.”

Identify Madar as fictional and the identity details as published synthetic fixtures. Explain the simulated date, 1 November 2026, so nobody mistakes the appointment dates for real current availability.

## 1:00–3:00 — Live demonstration

Start a fresh session in `uv run rafeeq.py chat --session presentation-demo` and use the synthetic request in README.md. Keep the exact approval proposal visible. Explain each read: verified permit, retrieved policy, actual slots. Then demonstrate `y` or rejection as planned. Read back the resulting inspection, not merely the model's text.

For the API variation, create an applicant session and show that chat returns a pending proposal without a booking. The officer has a separate credential and approves the exact immutable proposal with fresh synthetic verification. A wrong-role token cannot approve it.

If inference stalls, open `docs/demo-replay.html`. Clearly say: “This is a replay of a previous real model-backed test. It makes no new requests.” It is not represented as a live screen recording or an independent human decision.

## 3:00–5:00 — Engineering decisions (slides 3–4)

- The policy-only MCP process has no applicant-record tools.
- All transactional reads and writes share one persistent local database for that demo session; a new process does not reset business state.
- Identity and authorization are checked again inside the write transaction after approval. A stale approval cannot override a changed owner, revoked agent or consumed slot.
- Rejection is latched for that model turn; retrying the same tool cannot wear down the human gate.
- Context is bounded. The small process does not need an outer coding loop or graph framework; the API approval queue provides the required durable pause.
- The service uses separate user/officer roles, hashed session capabilities, durable request/token allowances and metadata-only logs. A bearer token is not itself proof that a real human inspected the proposal; operator review is part of the demonstration.

## 5:00–7:00 — Evaluation and error analysis (slides 5–6)

Read exact current counts from the generated slides and CSVs. A uses model wording; B uses verified booking/denial rendering. Safety is unchanged. The paired experiment may replay genuine model responses and rerun real tools on fresh databases, so it is not independent sampling and cannot claim pass^3 reliability.

Describe at least one failure honestly: generated promises, language mismatch or overbroad redaction masking a reference. Distinguish an application failure, a grader limitation and a quota error. Do not imply that unrun tasks passed or that the model's planning improved merely because the output renderer improved a confirmation.

## 7:00–8:00 — Security and close (slides 7–8)

“The malicious audit ticket asked for full identity details to an attacker-chosen destination. In an isolated intentionally unsafe control it caused one sensitive local-outbox row. The actual defended application produced none. No real email was sent.”

Map private records, untrusted ticket text and egress. The defence is capability restriction, verified access and approval in code—not a list of suspicious words. State residual risks: weak synthetic identity, heuristic masking, model prose errors, provider availability and lack of production infrastructure.

Close with reproducibility: one CLI entry point, offline checks, original tasks, actual A/B CSVs, security traces, and credited course primitives. This is a bounded demonstration, not a production municipal system.

## Four-minute Q&A preparation

Be ready to show the transaction/revalidation code, an approval audit, a failed eval row, cache-hit/provider-request accounting, exact remaining quota status, and the distinction between model token usage and conservative service token charges. If a risk is not handled, say so rather than guessing.
