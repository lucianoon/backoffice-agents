# backoffice-agents

[![CI](https://github.com/lucianoon/backoffice-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/lucianoon/backoffice-agents/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)

**Language:** [Português](README.md) | English

A backoffice-agent pilot where **the LLM generates and reasons** and **Jev decides, routes, and verifies**.
Inbound email, CRM and ERP tools, and a human operator over Telegram.

[Jev](https://typesafe.ai) is TypeSafe AI's *System One* model: it does not generate text; it answers typed
questions (yes/no, choice, score) with calibrated probabilities, in 70 to 500 ms, at US$ 0.042 per million
input tokens. Until the API key arrives (early access), the project runs with an **emulator** that
uses the LLM itself in the same format, marked as uncalibrated.

![backoffice-agents architecture: the LLM generates and reasons, while JEV decides, routes, and verifies](docs/assets/jev-architecture.png)

> **Core idea:** the LLM produces the reply and uses the tools; JEV is an independent decision layer that controls triage, safety gates, and verification before send.

## Flow

```
email ──> triage (Jev) ──> LLM agent + tools ──> verification (Jev) ──> send
               │                    │ gate (Jev + policy)          │
               │                    ▼                                ▼
               └──> escalate    human approval (Telegram)   human review (Telegram)
```

1. **Triage** (one call, five questions; two calls when an image is attached): category (Choice), urgency (Score 1-5), needs a
   human (Noul), sensitive data (Noul), prompt injection (Noul). High-confidence spam is discarded;
   a human request (`NEEDS_HUMAN_ESCALATE`) or sensitive data (`SENSITIVE_ESCALATE`) escalates.
2. **Category confidence band** decides the path: `auto` (>= 0.85) proceeds on its own, `review`
   (>= 0.55) sends the reply for a human to approve, below that it escalates.
3. **Agent** (LLM with tool calling) looks up CRM and ERP and drafts the reply. Before **every** tool
   the gate decides: reads execute; reversible writes go through Jev (appropriate? complete arguments?);
   high-risk and irreversible actions **always** ask for human approval, regardless of probability.
4. **Draft verification** (Jev): does it resolve the request? quality? does it claim something not grounded in the data?
   Failed: regenerate once with the feedback; failed again: escalate.
5. **Send** through the email adapter, record in the CRM, notify on Telegram.

Every Jev response (probability, confidence, latency, whether it is calibrated) is stored in the `decisions`
table with room for the human label, which is what makes it possible to measure calibration throughout the pilot.

### Conversation, policies, and attachments

- **Threads**: a new email is attached to the right conversation by the `In-Reply-To` and
  `References` headers (including when it replies to our message, whose `Message-ID` is stored) or,
  without headers, by sender and normalized subject within `THREAD_WINDOW_DAYS`. History
  (customer messages, our replies, open items) is fed into triage, the agent prompt, and
  verification.
- **Knowledge base** (`KB_DIR`, `.md` files per policy): the `kb_search` tool does
  a lexical search, sends the candidates to Jev in a single call with a relevance Score per
  snippet, and only the relevant ones reach the LLM. The prompt requires consulting the knowledge base before asserting
  any deadline or rule, and verification uses the snippets as grounding for "ungrounded claims".
- **Attachments**: PDF (pypdf), text, and images (LLM vision transcription) become text in the item
  state, next to the email body, and go through the same pseudonymization. Attachments larger than 5 MB
  are recorded without content. Triage first runs without the LLM (extracted PDF and text, images
  pending); only if the email passes the prompt-injection gate are images transcribed, and
  triage is rerun with the transcription, which goes through the same gate before reaching the agent.

### Token budget, per-customer configuration, and evaluation of the three decisions

- **Token budget** (`JEV_STATE_BUDGET_TOKENS`, default 24 thousand): before any call to
  Jev, the state is shrunk in predictable steps, from least to most important (history,
  attachments, tool results, email body), until it fits. What was cut is recorded in the item
  notes. Without this, a long thread would blow the API's 32-thousand-token limit.
- **Tenant** (`TENANT_FILE`, default `tenants/default.toml`): taxonomy, urgency and
  quality levels, agent prompt, company name, and signature. The tenant version is written on every
  decision and model call, and the calibration report splits by version, so a prompt or taxonomy
  change never mixes with the previous one.
- **Shadow gate and verification**: `eval-shadow --stage gate` uses `gate_labeled.jsonl` (tool
  calls labeled for appropriateness and complete arguments); `--stage verify` uses
  `verify_labeled.jsonl` (drafts labeled for resolve, ungrounded claims, and quality).
  `--stage all` runs all three decisions. The sets are synthetic, with positives and negatives.
  A question Jev left without a valid answer shows up as "no answer", counts as an error in
  accuracy (never as 0.5), and appears in each stage's "missing answers" row.

### Claim-by-claim verification, deterministic regression, and observability

- **Facts verified one by one**: the agent ends the reply with a `---FATOS---` line and the list
  of facts it asserts (order, amounts, dates, deadlines, promises). The customer does not see the list. In
  verification, each fact becomes a yes/no question to Jev in the same call. An ungrounded fact
  fails the draft, and the regeneration feedback names exactly which one, with the probability.
  `backoffice show` lists the facts with the verdict.
- **Replay cassette**: `eval-shadow --record data/cassettes/eval.json` records the
  LLM (emulator) answers indexed by the hash of the messages; `--replay` answers only from the cassette, with no network
  and no key. CI runs the full evaluation in replay with floors (`--min-triage`, `--min-gate`,
  `--min-verify`) and `--fail-stale`. Because the versioned cassette is generated from the
  labels themselves (`scripts/rebuild_cassette.py`, label-aligned answers at p=0.9, no LLM), this
  step **does not measure model quality or catch accuracy loss**: it fails when prompt,
  questions, taxonomy, pseudonymization, or dataset change without the cassette being regenerated (missing
  or leftover entry) and when the evaluation path (answer parsing, metrics) breaks.
  Did you change any of those on purpose? Rebuild with `PYTHONPATH=src uv run python
  scripts/rebuild_cassette.py` or record again with `--record` to capture the emulator's real text
  (then the floors start measuring the recorded LLM).
- **Decision-logic regression**: `tests/test_decision_logic_golden.py` is a labeled truth table,
  independent of the cassette, of what the code does with Jev probabilities: precedence
  of triage escalations (injection, sensitive data, spam, human, confidence), risk level
  of each tool, gate outcome and approval, regeneration or escalation on verification. Changing a
  default threshold, check order, or tool risk breaks these tests.
- **Logs and metrics**: `LOG_FORMAT=json` emits one event per line (triage, gate, approval,
  send, escalation, error, alert) with `item_id` and fields. `backoffice metrics` shows queue depth,
  errors, escalations, pending approvals and their age, mean and p95 latency per model, and
  cost in the window; `--prometheus` and `metrics-server` expose Prometheus format;
  `--push-cloudwatch` publishes to CloudWatch (`aws` extra). The worker notifies on Telegram, with
  cooldown, when an approval ages, the queue exceeds the limit, or the error rate rises.

### Retention, concurrency, and a cheaper emulator

- **Retention (LGPD)**: closed items are redacted after `RETENTION_REDACT_DAYS` (body,
  sender, attachments, messages, draft, and facts disappear; status, triage, verification
  numbers, and notes remain, which calibration and cost use) and deleted after `RETENTION_DELETE_DAYS`,
  along with decisions, calls, and approvals. Open items are never touched. The worker runs the purge
  every cycle; `backoffice purge --dry-run` lists what would be done.
- **Concurrency**: `WORKER_CONCURRENCY` processes several items in parallel inside one worker,
  with the same atomic queue reservation. `JEV_MAX_RPM` limits requests to the real Jev across
  threads, below the API's 1,200-per-minute cap.
- **Cheaper emulator**: `EMULATOR_MODEL` uses another model from the same provider only for the
  Jev emulator (for example, `gpt-4.1-nano`), with its own price in `EMULATOR_PRICE_*` for the
  cost report. The agent stays on the main model.

### Labeling, calibration, and thresholds

- **Taxonomy** in `docs/TAXONOMIA.md`: the category is the operational action requested; tone and threat go
  into `needs_human` and `urgency`. A guide for labelers with tie-break rules.
- **Two annotators**: `backoffice labels export` generates the batch; `labels agreement` measures agreement and
  Cohen's kappa per field; `labels merge` consolidates where they agree and separates conflicts for
  adjudication. The emails in `emails_eval.json` are synthetic; they exercise the
  tools, not support conclusions.
- **Telegram label**: every final notification (replied, escalated, discarded) includes
  "Category ok" and "Correct category" buttons. The label goes into the `decisions` table;
  `/calibracao` on the bot or `backoffice calibration` show accuracy and ECE per question and per model,
  separating real Jev from the emulator.
- **Per-category thresholds**: `CONFIDENCE_AUTO_BY_CATEGORY` and `CONFIDENCE_REVIEW_BY_CATEGORY`
  (JSON in `.env`) override the globals. `eval-shadow --suggest-thresholds` computes, per
  category, the lowest threshold with precision above the target and prints the ready-to-paste `.env` line;
  `None` means keep the category in human review until there is more data.

### Cost, queue, and tracing

- **Real cost and latency**: every model call (agent LLM, emulator, real Jev) goes into the
  `model_calls` table with tokens and latency. `backoffice costs` shows cost per model, per item,
  mean and p95, and the "what if": what the tokens that currently go through the emulator would cost on real Jev.
  Prices in `LLM_PRICE_*_PER_M` and `JEV_PRICE_INPUT_PER_M`.
- **Queue with atomic reservation**: `DB_URL` accepts SQLite (dev) or Postgres. Each worker reserves the
  next item with `FOR UPDATE SKIP LOCKED` (Postgres) or an immediate transaction (SQLite), so several
  workers and the Telegram poller coexist without processing the same item twice. Items in error
  come back after `RETRY_DELAY_S`. `docker-compose.yml` starts Postgres and two workers.
- **Per-item tracing**: `TRACING=langsmith` (with `LANGSMITH_API_KEY`) or `TRACING=langfuse`
  (`langfuse` extra). Each graph run becomes a trace named by the item, with nodes, LLM
  calls, tools, and Jev queries as spans.

### Protections

- **Pseudonymization** (`JEV_ANONYMIZE`): before leaving for Jev, emails, CPF, CNPJ, cards,
  phones, and the customer's known names become stable per-item tokens (`<email_1>`, `<nome_1>`).
  Order, invoice, and tracking numbers stay. The email sent to the customer is not affected.
- **Jev down**: the real client has a per-request timeout (`JEV_TIMEOUT_S`) and retries on 429, 5xx,
  and network/timeout errors with exponential backoff (`JEV_MAX_RETRIES`, `JEV_BACKOFF_S`, capped at
  `JEV_BACKOFF_MAX_S`), honoring `Retry-After`; a `Retry-After` larger than the limit ends the
  attempts. Once exhausted, or with a response outside the contract (no `answers`, invalid JSON, unanswered
  question), it raises a typed error (`JevUnavailableError`, `JevHTTPError`,
  `JevMalformedResponseError`). In triage and verification, it falls back to the emulator
  (`JEV_FALLBACK_EMULATED`) and notes that on the item; without fallback, the item escalates to a human. At the gate,
  the action falls back to human approval. Other kinds of errors leave the item in `error`, retried
  up to `MAX_ATTEMPTS` and then `failed`, with a Telegram notice.
- **Prompt injection**: triage asks Jev whether the email tries to instruct the assistant (ignore
  rules, act on another customer's behalf, leak data). Above `INJECTION_ESCALATE` the item escalates
  with no call to the agent LLM and no image transcription (triage with this gate runs
  before either of those; see "Attachments"). The gate is fail-closed: a missing, malformed, or
  non-numeric answer counts as suspicious and escalates the same way, also without the LLM (the emulator does not invent
  0.5 for a Noul the LLM failed to answer). The same applies to the other triage and
  verification questions; at the tool gate, the action goes to human approval. With **real Jev** the
  email never reaches any LLM. In
  **emulated mode** (`JEV_MODE=emulated`, or `JEV_FALLBACK_EMULATED` when Jev is down) triage itself
  is done by an LLM: the content reaches it, but in a tool-less call that can only
  return probabilities in JSON. Images from an email that passed the gate go to the vision LLM
  (also without tools) and the transcribed text goes through the gate again before the agent. The agent
  prompt also treats the email as data, not instruction.
- **Identity in tools**: CRM/ERP only operate on the item's sender. An order or invoice for another
  customer comes back as not found (no data leak). Forwarding only to addresses in
  `EMAIL_FORWARD_ALLOWLIST`.
- **Sensitive data**: noul `sensitive` above `SENSITIVE_ESCALATE` escalates without going through the
  agent LLM (in emulated mode, triage itself was already done by an LLM, as described above).
  `NEEDS_HUMAN_ESCALATE` is the dedicated “needs a human” threshold, separate from `CONFIDENCE_AUTO`.
- **IMAP by UID**: ingestion and `mark_processed` use UID SEARCH/FETCH/STORE, so compacting the mailbox
  does not renumber `item_id` or mark the wrong message.
- **Telegram operators**: only the chat in `TELEGRAM_CHAT_ID` is accepted and, with `TELEGRAM_OPERATORS`
  (comma-separated user ids), only those users approve, reject, or label. Who
  decided is recorded in `approvals.decided_by` and `decisions.human_label_by`.
- **Adapter contract**: `backoffice contracts` runs the same suite against mocks or real systems
  (reads by default; `--allow-writes` includes creating and canceling an order). When you implement a
  real CRM or ERP, the graph does not change; the contract guarantees the adapter behaves like the mock.
- **CI**: `.github/workflows/ci.yml` runs ruff, mypy (`src/`, with `check_untyped_defs`), the suite
  (including the Postgres test), contracts, and evaluation replay on every push and pull request.
- **Container**: the `Dockerfile` installs exactly `uv.lock` (`uv sync --locked`, no fallback),
  runs as a non-root user, and has a `HEALTHCHECK` with `backoffice healthcheck` (configuration and
  `SELECT 1` on the database, no external calls).
- **External effects at most once**: send writes a `sending` marker to disk before calling
  SMTP and `sent_at` afterward; an in-progress approval stays `applying`. If the process dies in the middle,
  resume does not repeat the effect: the item goes to a human to confirm in the destination system.

## Running

Requires Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # fill in LLM_* (or export OPENAI_API_KEY)
uv sync --python 3.12
uv run pytest               # offline suite: mocks and scripted LLM (the Postgres test runs only with TEST_DB_URL)
uv run mypy                 # type check of src/
uv run backoffice contracts --allow-writes   # contract suite for the configured adapters

uv run backoffice demo      # end to end with the emails in data/samples/emails.json and mocks
uv run backoffice items     # list items and final status
uv run backoffice show email:em-001
uv run backoffice eval-shadow --mode emulated --suggest-thresholds   # triage vs labels: accuracy, ECE, thresholds
uv run backoffice eval-shadow --mode emulated --stage all            # triage + gate + verification
uv run backoffice calibration                   # decisions with a human label, per question and model
uv run backoffice costs                         # cost and latency per model, real-Jev "what if"
uv run backoffice metrics                       # queue, errors, pending approvals, latency, alerts
uv run backoffice eval-shadow --mode emulated --stage all --replay data/cassettes/eval.json   # no network
uv run backoffice labels export --out data/labels/lote1.jsonl        # batch for two annotators
uv run backoffice labels from-mailbox --limit 100                    # last N from the mailbox (mock or IMAP) → batch
uv run backoffice doctor                                             # checks Jev/IMAP/Telegram without leaking secrets
```

Continuous operation:

```bash
uv run backoffice worker --watch     # ingest emails (IMAP) and process
uv run backoffice telegram           # button approvals and /pendentes /status /aprovar /rejeitar commands
docker compose up                    # Postgres + 2 workers + Telegram poller
```

### Switching from mock to a real system

| Variable | Values | Notes |
|---|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` | `openai`, `anthropic`, `bedrock_converse`, `google_genai`, `ollama` | `openai` + `LLM_BASE_URL` covers Ollama, Groq, vLLM, OpenRouter |
| `JEV_MODE` | `emulated`, `real` | `real` requires `TYPESAFE_API_KEY` |
| `DB_URL` | `sqlite:///...`, `postgresql+psycopg://...` | Postgres requires `uv sync --extra postgres` |
| `TRACING` | `none`, `langsmith`, `langfuse` | Langfuse requires `uv sync --extra langfuse` |
| `EMAIL_ADAPTER` | `mock`, `imap` | generic IMAP/SMTP (Gmail with an app password, Outlook) |
| `TELEGRAM_ADAPTER` | `mock`, `bot` | `bot` requires a token and an authorized `TELEGRAM_CHAT_ID` |
| `CRM_ADAPTER` / `ERP_ADAPTER` | `mock` or `module:Class` | implement the Protocol in `adapters/crm.py` / `erp.py`; the graph does not change. Run `backoffice contracts` |
| `EMAIL_FORWARD_ALLOWLIST` | emails or `@domain.com` | empty rejects automatic forwarding |

## Layout

```
src/backoffice_agents/
  config.py        environment variables
  llm.py           LLM factory
  jev/             Noul/Choice/Score models, real HTTP client, emulator
  policy.py        confidence bands, risk levels, gate decision
  privacy.py       pseudonymization of the state sent to Jev
  labeling.py      annotator batch, Cohen's kappa, consolidation, mailbox export
  calibration.py   accuracy and ECE of decisions with a human label
  decisions.py     the questions asked of Jev at each stage
  health.py        `backoffice doctor` (Jev, IMAP, Telegram, no secret leak) and `backoffice healthcheck`
  tools.py         agent tools with a declared risk level
  agent_loop.py    tool-calling loop with gate and pause for approval
  graph/           LangGraph graph (triage, action, verification, approval, send)
  adapters/        email (mock, IMAP/SMTP by UID), CRM/ERP (mock or module:Class), Telegram (mock, Bot API)
  storage.py       SQLAlchemy (SQLite/Postgres): item queue with atomic reservation, decisions, approvals, model calls
  costs.py         cost and latency per model, real-Jev "what if"
  tracing.py       LangSmith / Langfuse per item, Jev spans
  contracts.py     adapter contract suite (mocks and real)
  budget.py        token budget for the state sent to Jev
  tenant.py        per-customer configuration (tenants/*.toml)
  claims.py        splits customer-facing text from the asserted-fact list
  replay.py        record and replay of LLM answers (CI regression)
  obs.py           structured logs
  metrics.py       queue metrics, Prometheus, CloudWatch, and alerts
  retention.py     two-phase retention purge
  ratelimit.py     per-minute request limiter for Jev
data/cassettes/    evaluation replay cassette
  threads.py       attach emails to the conversation and history for the agent
  knowledge.py     knowledge base with Jev scoring snippets
  attachments.py   PDF, text, and image text
data/kb/           example policies (returns, deadlines, payment, warranty)
  runner.py        wires everything, processes and resumes items
  channels/        Telegram poller
  eval_shadow.py   shadow evaluation (accuracy, ECE, latency)
  cli.py           commands
```

## Pilot plan

| Phase | Delivery | Exit criterion |
|---|---|---|
| 0 Access and data | Jev key (waitlist or Vercel AI Gateway), 300 to 500 labeled emails, PII policy | Portuguese dataset ready |
| 1 Shadow | `eval-shadow --mode both` on the dataset | Jev accuracy >= LLM; ECE < 0.05 |
| 2 Confidence routing | `JEV_MODE=real` with the thresholds found in phase 1 | cost and p95 drop without losing accuracy |
| 3 Gates and verification | gate and verification in production | zero irreversible action without approval |

## Known risks

- **Data in the US.** Jev only exists as a US-hosted API. Zero data retention is only on the
  enterprise tier. Pseudonymization covers direct identifiers; the email body can still contain
  sensitive data in free text, so negotiate the enterprise tier for data under LGPD.
- **Portuguese.** TypeSafe assumes English as the primary language. Question instructions are in
  English and the state is in Portuguese; phase 1 measures whether that is enough.
- **Early access.** The API was unstable at launch. See "Protections" above: fallback to the
  emulator, escalation to a human, and bounded reprocessing.

## License

This project is under the [MIT license](LICENSE).
