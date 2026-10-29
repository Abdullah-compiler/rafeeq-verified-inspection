# Rafeeq HTTP service

`labs/capstone/api.py` is an original FastAPI adapter around the existing guarded booking application, not a copy of a peer service. It always runs configuration **B** with response caching **disabled**. The course context and core booking code are unchanged.

**Scope:** a loopback-only synthetic demonstration. Each session gets a separate copy of the course database. This is **not** a shared municipal database, enterprise tenant isolation, or a production identity system. The simulation booking date is **1 November 2026**; approval expiration uses the real server clock instead.

## Start

From the project root:

```bash
# New service-only credentials; neither value is printed by these commands.
export RAFEEQ_USER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export RAFEEQ_OFFICER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
cd labs
uv run --script --python 3.12 capstone/api.py --host 127.0.0.1 --port 8793
```

`--host` defaults to `127.0.0.1`, `--port` to `8793`; there is no `serve` subcommand. `::1` is also supported; other bind addresses are rejected by this CLI. Stop with Ctrl-C. Avoid exposing the service through a tunnel or reverse proxy without adding appropriate transport security and revisiting the authentication model.

Configuration:

| Environment variable | Meaning |
|---|---|
| `RAFEEQ_USER_TOKEN` | Required user-service bearer secret, at least 32 ASCII non-whitespace characters |
| `RAFEEQ_OFFICER_TOKEN` | Required separate officer-service bearer secret, same length rule |
| `RAFEEQ_API_DATA` | Optional private storage root; default `labs/artifacts/rafeeq-api` |
| Existing model/provider variables | Read by the existing project configuration; never accepted as service client credentials |

Existing `labs/.env` loading continues through the core. A root dispatcher can load root `.env` before invoking this file with `runpy` and forwarding CLI arguments. Standalone `api.py` does not independently read root `.env`. Keep any environment files untracked and private. Restart to change service tokens, but keep the same storage directory to retain session capabilities and approvals.

The service rejects identical user/officer tokens, OpenRouter-prefixed keys, and a token equal to the configured OpenRouter key. An OpenRouter key is never a bearer credential for this service. Service tokens are available only to authentication code, not included in model prompts or persisted in the service registry.

Dependency metadata is embedded using PEP 723, Python 3.12:

- Service-specific additions: `fastapi>=0.135,<1`, `uvicorn>=0.42,<1`.
- Test-specific addition: `httpx>=0.28,<1`.
- Existing core dependencies: `openai>=3.30`, `pydantic>=2.12`, `python-dotenv>=1.1`, `mcp>=2.3`.
- Tests use the standard-library `unittest`; pytest is not required.

## Authentication and endpoints

Send secrets in headers, never query strings. Responses have `Cache-Control: no-store`.

| Method / path | Authentication | Purpose |
|---|---|---|
| `GET /health` | Public | Process liveness, synthetic-data marker, config B and cache status; not a provider readiness probe |
| `GET /docs` | Public | Interactive OpenAPI documentation |
| `GET /openapi.json` | Public | Machine-readable schema and bearer-security declarations |
| `POST /sessions` | User bearer | Create an isolated synthetic session; HTTP 201 |
| `GET /sessions/{session_id}` | User bearer + `X-Session-Token` | Durable usage counters and this session's approvals, including terminal outcomes |
| `POST /sessions/{session_id}/chat` | User bearer + `X-Session-Token` | Run one bounded model turn; may return `pending_approval` |
| `GET /officer/approvals` | Officer bearer | All currently pending proposals, with their session IDs and exact count |
| `GET /officer/sessions/{session_id}/approvals/{approval_id}` | Officer bearer | Inspect one approval, reconcile interrupted commits and expire stale proposals |
| `POST /officer/sessions/{session_id}/approvals/{approval_id}/decision` | Officer bearer | Approve/reject the immutable exact proposal |

`POST /sessions` returns a random `session_id` and a separately random `session_token`. The service stores only the session token's SHA-256 hash. Save the returned capability privately; there is no recovery endpoint. The shared user-service token alone cannot read or chat in an existing session. A token for another session produces the same HTTP 404 as an unknown session. The officer token intentionally authorizes the officer queue across demo sessions; an approval ID still cannot be used under a different session path.

A chat request contains only:

```json
{"question":"Please book FTR-30311 at 2026-11-04 09:00 in Al-Rawdah. My ID last4 is 6654 and registered mobile is 0532236654. Retrieve POL-05 and request separate officer approval."}
```

The identity values above are published synthetic course fixtures, not real personal data. Model-backed `/chat` requests consume provider quota. No live inference is performed by the offline tests below.

The chat response contains `session_id`, `state`, `reply`, `approvals`, `usage`, and `model_error`. `state` is `complete`, `stopped`, or `pending_approval`. An approval record contains:

- `approval_id`, immutable `proposal`, and `proposal_hash`;
- `created_at` and `expires_at`, Unix seconds on the real server clock;
- `status`: `pending`, `approved`, `rejected`, `expired`, or `failed`;
- `decision` and a database-verified `booking` when applicable.

Chat is deliberately **stateless across HTTP requests**: histories and identity credentials are not retained between turns or on disk. Supply the necessary synthetic identity details again for a new permit lookup. Session databases, pending proposals, results, and usage counters are durable. This is not a durable multi-turn transcript service.

## Human approval protocol

1. A valid chat tool call reaches the core's approval callback. The API stores the exact proposal and its hash in that session's SQLite database, then returns `False` to the core. The core cannot commit a booking.
2. Although the synchronous CLI core interprets `False` as denial, the HTTP adapter returns a trusted **pending approval / no booking** response. It does not repeat the core's temporary denial message or model-generated booking promises. A queued proposal remains discoverable even if a later model step fails.
3. An authorized officer inspects the exact proposal in the officer queue. Do not infer human review merely because the API received an officer bearer token: the operator or UI is responsible for presenting the proposal and obtaining an actual decision.
4. The officer copies the exact proposal into the decision request. Approving additionally requires **fresh** synthetic identity values; previously verified credentials are not stored in the proposal.
5. The service retrieves POL-05 through the existing policy-only MCP bridge, re-verifies the current permit/identity, lists the exact date again, and invokes the existing guarded booking tool. Its approval gate checks the recomputed proposal for equality and checks real-clock expiry. Current identity/authorization, permit details and slot availability are rechecked by the core inside the write transaction.
6. A successful response is returned only after read-back of the inspection and the approval-bound transaction audit. The session status endpoint shows the terminal outcome; the caller need not ask a model to explain it.

Approve payload shape (the `proposal` must come from the queue, not be constructed from this example):

```json
{
  "decision": "approve",
  "proposal": {
    "permit_id": "FTR-30311",
    "slot": "2026-11-04 09:00",
    "district": "Al-Rawdah",
    "kind": "health"
  },
  "national_id_last4": "6654",
  "mobile": "0532236654"
}
```

To reject, send `{"decision":"reject","proposal": ...}` with the same exact proposal; no identity values are necessary. Rejecting does not write an inspection or reserve a slot.

Properties:

- Default expiry: **600 seconds**. An expired proposal cannot authorize a new write.
- Proposal fields, ID, hash, creation and expiry timestamps are immutable through SQLite triggers. Terminal status/decision/result cannot be changed through an ordinary update.
- One approval authorizes at most one booking write. Repeating the same successful decision returns the same outcome with `idempotent_replay: true`; attempting the opposite decision yields HTTP 409. Replays still require officer authentication and an identical proposal. A replay is a read of the authorized result, not a new approval or identity verification.
- Missing fresh identity yields HTTP 422; incorrect/revoked identity yields HTTP 403 and leaves the proposal pending until its expiry. Policy-service outage yields HTTP 503 and leaves it pending. Changed business state or an unavailable slot invalidates the proposal with HTTP 409 and no booking by that attempt.
- Threads and cooperating processes sharing one storage root serialize decisions with a process lock and file lock. The core also serializes its booking transaction with SQLite `BEGIN IMMEDIATE`.
- A core transaction records actor `api-approval:<approval_id>`. If a process stops after the booking commit but before saving the queue outcome, the next status read/decision recovers the exact booking from that transaction audit before evaluating expiry. It does not make another booking. An unreconcilable audit/inspection mismatch requires manual review rather than a guessed success.
- Filesystem/database administrators are trusted. These triggers and hashes are not tamper-proof signatures against an administrator who can edit the database or remove triggers.

## Operational limits and privacy

Defaults are explicit in `Settings` and may be adjusted by constructing a service with that settings object:

| Limit | Default |
|---|---:|
| Question | 12,000 characters, nonblank |
| Incoming JSON/body | 65,536 bytes, including streaming bodies |
| Request model-token accounting allowance | 60,000 |
| Cumulative allowance per session | 300,000 |
| Cumulative allowance across this service storage root | 1,000,000 |
| Chat requests per session, including failed attempts | 50 |
| Sessions per storage root | 100 |

Before any provider request, a `BudgetShape` reserves a conservative charge consisting of serialized prompt/tool-schema UTF-8 bytes, framing allowance, and requested maximum output. If it cannot fit, the provider call is not made. Provider-reported usage is recorded separately as `observed_tokens`; if it exceeds a reservation, the extra is charged and tools from that generation are not executed. Conservative charges, **not** provider token measurements, consume the durable quota. This is an application guard, not a tokenizer-specific proof or provider billing limit.

The full request allowance is reserved durably before starting. On normal completion or handled failure, unused allowance is released; a process interruption conservatively retains the full reservation. Restarting or creating a new session does not evade the service-wide allowance. The core retains its additional maximum-step, maximum-cost and output-length controls. All model calls are serialized because the inherited ledger/configuration is process-global. This intentionally favors a small, predictable demonstration over throughput.

Storage under `RAFEEQ_API_DATA`:

```text
service.sqlite                      # session capability hashes and durable quota counters
operations.jsonl                    # metadata-only operational events
.service.lock                       # cooperating-process serialization
<session_id>/madar.sqlite            # synthetic business data and api_approvals table
```

The service creates private directories (`0700`) and database/log files (`0600`). Pending records store no submitted ID4/mobile and no bearer/session token. **The inherited synthetic business database itself contains fictional applicant identities**; that is separate from storing HTTP-supplied credentials.

Operational JSONL uses server-generated event names, timestamps, random identifiers, HTTP statuses, usage charges and durations. It never records question/reply/exception text, identity values, headers, raw URLs or query strings. The existing redactor is additionally applied. Validation errors do not echo rejected inputs; model errors return generic messages. Uvicorn access logging and proxy-header trust are disabled by the CLI. No CORS policy grants browser origins access. Heuristic reply masking is not a complete PII detector; do not use real personal data.

Not included: TLS, per-human accounts, an officer identity directory, token rotation/revocation endpoints, a distributed queue, log rotation, backups, a deletion/retention scheduler, provider-side data guarantees, or an internet-facing rate limiter. `/health` does not test database/provider/MCP readiness. Long model calls serialize chat and officer operations, which is acceptable only for this small demo. Stop and restart does not reset quotas; deleting the data directory discards all demo records and should be a deliberate local action.

## Verification

Run from `labs/`:

```bash
uv run --script --python 3.12 capstone/api.py --help
uv run --script --python 3.12 capstone/tests/test_api.py
```

The API suite uses a conspicuously labelled deterministic **fake model runner** and mostly a fake public-policy fixture to test HTTP protocol, not model quality. All booking, identity, slot, audit and queue assertions use real synthetic SQLite databases and the real guarded core tools. It covers authentication/roles, cross-session denial, exact-proposal matching, immutable/terminal records, fresh/revoked identity, policy outage, expiry (including expiration while checking policy), rejection, changed permit state, taken slots, idempotence, duplicate concurrent decisions, serialized model calls, crash recovery, privacy, input limits and durable quotas.

Two actual loopback exercises run inside that suite:

1. A real TCP/Uvicorn server is exercised with HTTPX for health, docs, denied unauthenticated creation, authenticated creation, fixture-backed chat, officer approval, and outcome read-back. This is not a TestClient-only check.
2. The production CLI is started as two separate processes against the same storage directory. A fixture-generated pending record survives the first shutdown; the second process approves it over HTTP using the **real policy MCP subprocess** and guarded SQLite booking, without making a model call.

All spawned HTTP servers/processes are stopped and joined before the tests finish. Tests keep data under a temporary directory and clean it up. These checks do **not** establish live-provider `/chat` reliability, actual human sign-off, broad security assurance, or capstone evaluation performance. A separate, explicitly authorized live-model smoke test remains necessary.
