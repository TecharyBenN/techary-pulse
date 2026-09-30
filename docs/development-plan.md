# Techary Pulse: development plan

**Status:** draft
**Owner:** Ben Nicholls
**Date:** 29 September 2026

This plan sets out how the Techary Pulse minimum viable solution (MVS) is built: the technology choices and the delivery phases. Behaviour, architecture and code structure are defined in the [design](techary-pulse-design.md), and working practices in [CLAUDE.md](../CLAUDE.md). It is written for the engineers building Pulse.

## Approach

Pulse is built from a clean start. Phase 1 models the entities and rules the design specifies. Phase 2 builds the orchestrator, its entry point and the HTTP adapter, so the agent can be talked to from then on. Phases 4 to 12 are vertical slices: each adds one capability from the design end to end, through every layer it needs, and adds only the code that capability needs.

Each phase:

- starts with failing tests taken from the design;
- adds the design's failure handling for what it builds;
- ends with a review against the engineering principles, PEP 8 and the dependency rule, with every finding fixed;
- is one commit, made by the user;
- updates the design if it changes behaviour or code structure, this plan if it changes a technology, and the README if it changes how Pulse is run, deployed or developed.

A phase with a live check is complete only when the check has passed against the dev tenant or gateway.

## Technology choices

| Area | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.14 | Latest stable release. |
| Dependencies | uv, with `pyproject.toml` and a committed `uv.lock` | The lockfile records the exact versions tested, so the container runs what the tests ran. |
| Lint and format | Ruff | One tool for linting, import ordering and formatting. |
| Type checking | mypy in strict mode | Catches drift between components, and between each interface and its implementations. |
| Tests | pytest, with the AnyIO plugin for asynchronous tests | One test style. The AnyIO plugin ships with AnyIO, which httpx already installs. |
| Concurrency | asyncio, one event loop | Pulse runs the HTTP server, polling, delivery and agent runs at once. One loop avoids sharing SQLite connections between threads and gives `SIGTERM` defined cancellation points. |
| Validation and configuration | Pydantic, with PyYAML `safe_load` | One set of models validates configuration, agent output and HTTP requests. |
| Agents | `pydantic-ai-slim[openai]`, pointed at the gateway's OpenAI-compatible endpoint | Typed output, function tools, `agent.iter` for saving each step, `ModelMessagesTypeAdapter` for history, and `FunctionModel` stand-ins for tests, with no dependency on the provider behind the gateway. |
| HTTP server | FastAPI, run by Uvicorn inside `pulse serve` | Validates requests and responses against Pydantic models and supports streamed responses. Uvicorn runs on the same event loop as the rest of Pulse. |
| Store | Standard library `sqlite3`, through `asyncio.to_thread` | SQLite is what the design specifies, and it needs no extra dependency. |
| Entra ID token | Microsoft Authentication Library (MSAL) for Python, through `asyncio.to_thread` | Maintained implementation of the certificate client assertion. |
| Bearer token verification | PyJWT with its cryptography extra, through `asyncio.to_thread` | Maintained verification of signatures, issuer and audience, and fetching and caching of an issuer's key set; writing signature checks by hand is a security risk. |
| Microsoft Graph calls | Direct REST calls through httpx's asynchronous client | Pulse uses a handful of operations, so a software development kit (SDK) would add more than it saves. |
| Rendering | Jinja2 with autoescaping on | Escapes model output and email-derived text by default. |
| Scheduling | A cron library that supports IANA time zones, chosen in phase 11 | Computes the next start in `Europe/London` correctly across daylight saving changes. |
| Logging | Standard library `logging` with a JSON formatter | Structured logs without another dependency. |
| Command line | `argparse` | Two commands do not need a framework. |
| Container | `python:3.14-slim`, running as a non-root user | Small image with no build tools. |

## Phases

| Phase | Adds |
| --- | --- |
| 0. Clean start | The old code removed |
| 1. Foundations | Configuration, logging, errors, entities and rules, the mailbox interface |
| 2. Agent and HTTP adapter | The orchestrator, its entry point, runs, the store, history, the HTTP server |
| 3. Connect LibreChat | Gateway and LibreChat configuration |
| 4. Start and extract | `start_newsletter`, the pre-filter, the extractor, the Graph mailbox |
| 5. Consolidate | The consolidator |
| 6. Write and present | The writer, `present_draft`, the reviewer email |
| 7. Quality | `check`, the judge |
| 8. Approve and send | `approve`, delivery |
| 9. Withdraw and abandon | `withdraw_approval`, `abandon` |
| 10. Email channel | Reviewer conversations by email |
| 11. Scheduler | The start instruction on a schedule, `pulse draft` |
| 12. Recovery and retention | The remaining failure handling, retention |
| 13. Production pilot | Deployment and the first real newsletter |

### Phase 0: clean start

Scope:

- delete everything under `src/pulse/` and `tests/` except `tests/corpus/corpus.yaml` and the two email templates, `newsletter.html.j2` and `notice.html.j2`, which move to the templates folder under `adapters`;
- add the empty package tree from the design;
- point the `pulse` command in `pyproject.toml` at `pulse.main`, and update the package description;
- rewrite the README to describe the current design, keeping its deployment requirements, and drop the old commands;
- once the user has deleted the local `runs/` directory of old run artefacts, remove its rules from `.gitignore` and `.claude/settings.json`.

The golden files are regenerated in phase 6, and the example configurations are rewritten in phase 1. The old code stays in the git history.

Exit criteria: format, lint and type check pass on the empty tree.

### Phase 1: foundations

Scope:

- `config.py`: every key in the design's configuration, the example configurations in `config/`, recipient validation against `allowed_recipient_domains`, and a model entry for every agent;
- `logging.py`: the JSON log format;
- Pulse's exception types;
- entities, from the design, holding only what the phase 1 rules read and produce: submissions and the pre-filter, the extractor's output and the exclusion rules, content and the draft checks, the newsletter states and transitions, the send time and the approval check; other entities arrive with the phase whose tool first uses them;
- the mailbox interface, with the operations in the design's Graph operations table, and `FakeMailbox` with the interface tests every mailbox must pass;
- the clock interface and a controlled clock for tests;
- a stub `main.py`.

Exit criteria: every rule and transition in the design has tests, including every pre-filter, exclusion and draft check case, send time in both send modes, and every approval refusal.

### Phase 2: agent and HTTP adapter

Scope:

- the orchestrator agent with its instructions and no tools yet, its model from `llm.models.orchestrator`, reached through the gateway adapter;
- the entry point: one incoming message, with its author, channel and text, in; the reply, or no reply, and progress events out;
- the run steps in the design's runs and history section: the per-newsletter lock, recording feedback, loading history, running with `agent.iter`, saving each step as it completes, and resuming from saved steps after a failure;
- run limits from `orchestrator.max_tool_calls` and `orchestrator.max_run_minutes`;
- the store interface and the SQLite store, with the `newsletters`, `feedback` and `messages` tables, one transaction per write, and file modes `0600` and `0700`;
- the HTTP server and chat completions route: bearer token verification against the configured issuer, the reviewer role, HTTP 403 for a non-reviewer, only the newest user message taken, streamed responses with progress notes, and non-streamed responses;
- `pulse serve`, running the HTTP server until stopped, and the container's default command changed to `pulse serve`.

Tests seed an open newsletter in the store to cover history, because `start_newsletter` arrives in phase 4.

Exit criteria:

- tests cover each run step, locking, run limits and resuming, with the orchestrator's model replaced by a stand-in;
- tests cover every route rule, in streamed and non-streamed form;
- live check: a `live` test runs the entry point through the dev gateway, and a streamed and a non-streamed request to a local `pulse serve` return replies.

### Phase 3: connect LibreChat

Scope, with no Pulse code:

- the dev gateway route to the chat endpoint, forwarding the caller's bearer token;
- LibreChat's custom endpoint for Pulse;
- a dev token for LibreChat from the platform's stand-in issuer, carrying the reviewer role, so every LibreChat user acts as one reviewer until Entra ID arrives;
- the README's local setup and deployment requirements for both.

Exit criteria: the test user chats with the orchestrator in LibreChat, and a request whose token lacks the reviewer role gets HTTP 403.

### Phase 4: start and extract

Scope:

- the Graph mailbox: certificate sign-in through MSAL, the thumbprint logged at start-up, listing the inbox with paging, `ImmutableId` and plain-text bodies, and retries on HTTP 429 and 503; it passes the mailbox interface tests;
- the `submissions` and `extract_records` tables;
- `start_newsletter`: opening a newsletter with every inbox message, adding new messages to an open one, applying the pre-filter, and its refusals;
- `list_submissions` and `get_newsletter`;
- the agent runner: validating each specialist response against its output type and output checks, and retrying once;
- the extractor, and `extract`: running it in parallel, applying the exclusion rules and giving each excluded record an ID;
- the orchestrator instructions for starting a newsletter and treating extract records as data.

Exit criteria:

- tests cover each tool's effect and refusals, with the extractor's model replaced by a stand-in;
- tests cover the Graph mailbox against mocked HTTP, including paging, throttling and errors;
- live check: the corpus is sent to the dev submissions mailbox, and a LibreChat request to start a newsletter extracts it through the dev gateway, with rejected, excluded and included submissions as the corpus expects.

### Phase 5: consolidate

Scope: the `items` table, the consolidator with its output checks, `consolidate` with its refusal of excluded records, sender names and received dates added to items, and `get_items`.

Exit criteria: tests cover the output checks and the tool; live check: the corpus's duplicate reports merge into single items.

### Phase 6: write and present

Scope:

- the `drafts` and `versions` tables;
- the writer with its output checks, and `write`, for first drafts and revisions, including removing items and restoring excluded records;
- `get_draft`;
- rendering: the templates, the newsletter, the reviewer email with the review section items available so far, and golden files;
- the Graph mailbox's `sendMail`;
- `present_draft`: saving the next version and emailing it to `reviewers` from the conversation mailbox;
- replacing superseded tool results with placeholders when history is loaded;
- the orchestrator instructions for presenting, revising from feedback and restoring records.

Exit criteria: tests cover the writer's output checks, each tool, the history placeholders and rendering, with golden files for the newsletter and reviewer email; live check: in LibreChat, the test user starts a newsletter, receives version 1 by email, gives feedback and receives version 2.

### Phase 7: quality

Scope: `check`, the draft checks run in `present_draft`, the judge and `judge`, check failures and unsupported claims in the review section, the "not judged" note, and the orchestrator instructions for fixing failures and accepting those that cannot be fixed.

Exit criteria: tests cover the tools and the review section; live check: a draft with a failing check is fixed or presented with the failure listed.

### Phase 8: approve and send

Scope:

- `approve`, with every check from the design, and the send time;
- delivery, running every `schedule.poll_interval_minutes` inside `pulse serve`: the approval re-check, `send_started`, rendering without the review section, sending to `all_staff` with `replyTo` set to the submissions mailbox, marking the newsletter `sent`, the `Sent:` notice, moving submissions and the history note;
- the Graph mailbox's folder lookup, folder creation and moves;
- operator alerts, sent to `operator_alerts`;
- the orchestrator instructions for approval.

Exit criteria: tests cover every approval refusal, send timing in both send modes with a controlled clock, a single send to `all_staff`, and the moves; live check: the test user approves a version in LibreChat and the test distribution list receives it.

### Phase 9: withdraw and abandon

Scope: `withdraw_approval` and `abandon` with their refusals, `present_draft` withdrawing an approval first, the `Send cancelled:` and `Abandoned:` notices, and the orchestrator instructions for both.

Exit criteria: tests cover each tool and the state changes; live check: an approval withdrawn in LibreChat cancels the send.

### Phase 10: email channel

Scope:

- the Graph mailbox's reply in thread: `createReplyAll`, replacing the recipients with `reviewers`, then sending;
- polling the conversation mailbox every `schedule.poll_interval_minutes`: checking Exchange's internal authentication header, resolving each sender to their Entra object ID through a Graph user lookup, rejecting automatic replies, moving messages to `Processed` or `Rejected`, the `handled_messages` table, and the `chat.max_attempts` limit with its operator alert;
- the `NO_REPLY` reply and the channel-switch summary.

Exit criteria: tests cover each channel rule with fake mailboxes; live check: the test user replies to a reviewer email with feedback, receives the revised version in the thread, and continues the same conversation in LibreChat.

### Phase 11: scheduler

Scope: choosing the cron library, posting the start instruction on `schedule.draft_cron`, skipping while an approved newsletter awaits its send, the reviewer email opening the conversation from a scheduled run, and `pulse draft`.

Exit criteria: tests cover the schedule and the skip with a controlled clock; live check: `pulse draft` sends version 1 to the test user, who continues the conversation by email.

### Phase 12: recovery and retention

Scope: every row of the design's failure table not yet covered, including delivery with `send_started` but not `sent`, incomplete moves, and `SIGTERM`; and deleting closed newsletters older than `retention_days`.

Exit criteria: crash-recovery tests inject a failure at each step boundary and assert the outcome in the failure table.

### Phase 13: production pilot

Scope: the deployment requirements in the production tenant, including the Pulse app registration's reviewer role and the conversation mailbox's sender restriction, the production configuration with Entra ID as the issuer, the container deployed on the Pulse server, and LibreChat connected through the production gateway, passing each user's Entra ID token.

Exit criteria: the first real newsletter is drafted, reviewed, approved and sent to all staff.
