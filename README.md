# Action Guardrail Proxy

A pre-execution guardrail for autonomous agents. Every tool call an agent
wants to make (delete database rows, send an email, read a file, ...) is
routed through this proxy first. A declarative policy file decides, per
call, whether to **block** it outright, **hold it for human review**, or
**log and allow** it to proceed — before the real system ever sees it.

```
LLM / Agent → POST /v1/execute-tool → Policy Engine (policy.yaml)
                                            │
                     ┌──────────────────────┼──────────────────────┐
                     ▼                      ▼                      ▼
                  BLOCK                REQUIRE_HITL           LOG_AND_ALLOW
                 403 + audit      202, PENDING in DB,      executes mock tool
                                  awaits /v1/hitl/review     + writes audit log
```

## Contents

```
config/
  policy.yaml       Declarative rules the Policy Engine evaluates
  settings.py        Environment-driven app configuration
src/
  models.py           Pydantic v2 request/response/audit schemas
  database.py         Async SQLAlchemy models + audit/HITL persistence
  policy_engine.py     Rule evaluator (no eval/exec — operator dispatch table)
  tools.py             Mock DatabaseTool / EmailTool / FileSystemTool
  main.py              FastAPI gateway: /v1/execute-tool, /v1/hitl/review, /healthz
tests/
  test_harness.py      Integration tests + optional live-LLM tool-call generation
Dockerfile, docker-compose.yml, .dockerignore, .env.example
```

## Quickstart (local)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

uvicorn src.main:app --reload --port 8000
```

The service creates its SQLite database and tables automatically on
startup — no migration step needed for local dev.

Try it:

```bash
# Blocked: over the 100-record threshold
curl -X POST localhost:8000/v1/execute-tool \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"agent-1","tool_name":"database_delete","parameters":{"record_count":500,"table":"users"}}'
# -> 403, action:"block"

# Held for human review: external recipient
curl -X POST localhost:8000/v1/execute-tool \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"agent-1","tool_name":"send_email","parameters":{"email_to":"partner@othercorp.com","subject":"hi","body":"hello"}}'
# -> 202, action:"require_hitl", returns a request_id

# Approve it
curl -X POST localhost:8000/v1/hitl/review \
  -H "Content-Type: application/json" \
  -d '{"request_id":"<paste the request_id here>","decision":"approve","reviewer":"alice"}'
```

## API reference

| Method | Path                | Purpose                                                         |
|--------|---------------------|------------------------------------------------------------------|
| POST   | `/v1/execute-tool`  | Submit a proposed tool call for policy evaluation (+ execution). |
| POST   | `/v1/hitl/review`   | Approve or reject a pending HITL request.                        |
| GET    | `/v1/hitl/pending`  | List requests currently awaiting human review.                   |
| GET    | `/v1/audit-logs`    | Recent audit trail (`?limit=`).                                  |
| GET    | `/healthz`          | Liveness/readiness probe.                                        |

**`POST /v1/execute-tool`** request body:

```json
{
  "agent_id": "agent-finance-01",
  "tool_name": "database_delete",
  "parameters": { "record_count": 5, "table": "users" }
}
```

Responses:
- `403` — action was **block**ed. Body includes `rule_matched` and `reason`.
- `202` — action needs **human review**. Body includes `request_id`; poll
  `/v1/hitl/pending` or wait for your reviewer to call `/v1/hitl/review`.
- `200` — action was **log_and_allow**ed and executed (or simulated, if
  `dry_run` is on). Body includes the mock tool's `result`.

## Policy configuration (`config/policy.yaml`)

Rules are a flat, ordered list — data, not code:

```yaml
rules:
  - tool: "database_delete"
    condition: "parameters.record_count > 100"
    action: "block"
    reason: "Delete exceeds the 100-record safety threshold."
```

For a given `tool_name`, rules are checked top to bottom and the **first**
whose `condition` evaluates true wins. `condition` is a small boolean
expression — comparisons (`>`, `>=`, `<`, `<=`, `==`, `!=`), membership
(`in` / `not in`), boolean combinators (`and`/`or`/`not`), and a handful of
string methods (`.endswith()`, `.startswith()`, `.lower()`, `.upper()`,
`.strip()`) — evaluated against the tool call's own `parameters` dict.

**This is deliberately not Python's `eval()`.** `src/condition_evaluator.py`
parses each condition into an AST and walks it through an explicit
whitelist (see `tests/test_condition_evaluator.py` for a battery of
injection attempts — `__import__(...)`, `open(...)`, `exec(...)`, dunder
attribute chains — that are all rejected before evaluation). `policy.yaml`
sits directly in the path of a security control and is the kind of file a
future teammate — or, in a worst case, an attacker who gets write access to
config — might be able to edit; handing that string to raw `eval()` would
be an arbitrary-code-execution hole in the gateway itself. The evaluator
keeps the config purely declarative while staying exactly as expressive as
the three example rules above need.

Add a new guarded tool by adding new rule blocks; no Python changes
required. If a tool has rules but none of their conditions match the given
parameters, or the tool has no rules at all, the top-level `default_action`
applies (defaults to `log_and_allow`, so unknown shapes are audited rather
than silently dropped or hard-blocked).

The global `dry_run: false` flag can be flipped in the YAML or overridden
per-deployment with the `DRY_RUN_OVERRIDE` env var. When on, the Policy
Engine still fully evaluates and audits every call, but `src/tools.py`
returns `{"status": "simulated", "dry_run": true, ...}` instead of
performing the (mock) action — useful for rehearsing a new policy against
real traffic before trusting it to actually run.

## Running the tests

```bash
pip install -r requirements.txt   # includes pytest/pytest-asyncio
pytest tests/test_harness.py -v
```

This runs eight tests in-process against the FastAPI app (via httpx's
`ASGITransport` — no server process needed): the five mandatory scenarios
from the spec (500-record block, 5-record pass, external-email HITL hold,
internal-email pass, confidential-file read-and-audit) plus two bonus tests
covering the full HITL approve/reject webhook lifecycle, and a health check.

**Live LLM integration.** `tests/test_harness.py` also contains
`generate_tool_call_via_llm()`, which uses the official `anthropic` SDK with
real tool-calling to have a live Claude model decide the `tool_name` and
`parameters` for each scenario from a natural-language prompt — exercising
the true LLM → tool_use → proxy path end-to-end. Set `ANTHROPIC_API_KEY` in
your environment to enable it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pytest tests/test_harness.py -v
```

Without a key, each test transparently falls back to an equivalent
hand-built payload, so the suite is fully deterministic and safe to run in
CI without live model calls or API costs gating every build. This is a
deliberate choice: a live model call is not reproducible run-to-run, so it
shouldn't be what decides whether your guardrail logic is correct — the
in-process assertions are what actually prove that, on every run.

## Docker

```bash
docker compose up --build
```

This builds the image, mounts a named volume at `/app/data` for the SQLite
file (so audit history survives container restarts), and exposes the API on
`localhost:8000`. Override any setting via environment variables in
`docker-compose.yml` (see `.env.example` for the full list — they map
directly to fields in `config/settings.py`).

For a registry/CI build without compose:

```bash
docker build -t guardrail-proxy .
docker run -p 8000:8000 -e DRY_RUN_OVERRIDE=true guardrail-proxy
```

## Deploying to AWS

The container is stateless aside from the SQLite file, so the simplest path
is:

1. **Swap SQLite for a managed Postgres** in production — set
   `DATABASE_URL=postgresql+asyncpg://...` (RDS/Aurora) so multiple task
   instances share one audit/HITL store instead of each holding a private
   SQLite file. Add `asyncpg` to `requirements.txt` when you do.
2. Push the image to **ECR**, then run it on **ECS Fargate** or **App
   Runner**, either of which can point its health check directly at
   `GET /healthz` on port 8000.
3. Put an **ALB** in front for TLS termination and to fan traffic out
   across task replicas — the app has no in-memory session state, so it
   scales horizontally without sticky sessions.
4. If HITL reviewers should be notified rather than having to poll
   `/v1/hitl/pending`, wire an **SNS/EventBridge** publish call into the
   `require_hitl` branch of `POST /v1/execute-tool` in `src/main.py`.

## Notes on scope / design choices

- **No `eval()`/`exec()`** anywhere in the policy path — conditions are
  matched through an explicit operator table in `policy_engine.py`, so
  `policy.yaml` can be edited without ever running arbitrary code.
- **Structured JSON logs** (see `JsonFormatter` in `src/main.py`) so the
  service is CloudWatch/ELK-friendly out of the box.
- **Sensitive parameter redaction** (`sanitize_parameters` in
  `src/database.py`) strips fields like `password`/`api_key`/`token` before
  they're written to the audit log, even though today's tools are mocks —
  this is the habit you want in place before real tools are plugged in.
- The mock tools in `src/tools.py` are intentionally simple stand-ins;
  swap their bodies for real DB/SMTP/filesystem calls when you're ready to
  go from guardrail-around-mocks to guardrail-around-production-systems —
  the FastAPI/policy/audit layers above them don't need to change.
