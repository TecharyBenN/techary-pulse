# Techary Pulse: design and architecture (minimum viable solution)

**Status:** draft
**Author:** Ben Nicholls
**Date:** 25 September 2026

Techary Pulse is a scheduled service that turns staff updates emailed to `pulse@techary.ai` into a weekly draft newsletter and emails the draft to human-in-the-loop (HITL) reviewers. This document describes the design of its minimum viable solution (MVS) for the engineers who build and operate it.

## Scope

Each run reads the emails in the Pulse mailbox inbox, filters out anything not from an allowed company sender, extracts and consolidates the genuine updates, drafts a newsletter in the Techary tone of voice, and emails the draft to the configured reviewers. Processed emails are then moved out of the inbox.

The MVS excludes:

- sending the newsletter to all staff, and any approval workflow;
- figures from company systems, such as Salesforce;
- databases, embeddings and vector storage;
- attachment content.

## Environment

Pulse runs as a container on a closed server. The server has outbound access to Microsoft Graph (`graph.microsoft.com`), the Microsoft Entra ID token endpoint (`login.microsoftonline.com`) and an AI gateway, currently agentgateway. It has no access to Azure Storage or other Azure services.

All connections, paths and the schedule are set in configuration. Pulse has no dependency on the host beyond these.

## Architecture

Pulse is a fixed pipeline. Code performs all deterministic work: fetching, filtering, rule enforcement, rendering, sending and moving mail. Model calls perform only classification, extraction, consolidation, drafting and verification, each returning JSON (JavaScript Object Notation) validated against a schema. The model has no tools and cannot affect recipients or which messages are processed.

The pipeline reaches the mailbox through `Mailbox`, a short interface that lists, moves and sends mail. Production uses `GraphMailbox`, which calls Microsoft Graph; automated tests use `FakeMailbox`, which holds test messages in memory.

Each model step is an agent: a self-contained class, derived from a shared `Agent` base class, declaring its instructions, model, input and output types, output checks and permitted tools, which are always none. A shared runner executes any agent through Pydantic AI. Automated tests run the same agents against Pydantic AI's test models instead of the gateway.

```mermaid
flowchart LR
    subgraph HOST["Pulse container"]
        S["pulse schedule"] --> P["Pulse pipeline"]
        CFG["config.yaml"] --> P
        P --> FS[("Run artefacts")]
    end
    subgraph M365["Microsoft 365"]
        ENTRA["Entra ID<br/>token endpoint"]
        MB["pulse@techary.ai<br/>shared mailbox"]
    end
    P -- "token (certificate)" --> ENTRA
    P -- "Graph: read, move, send" --> MB
    P -- "prompts" --> GW["AI gateway"]
    GW --> LLM["LLM provider"]
    MB --> R["HITL reviewers"]
```

*Figure 1: Pulse components and connections.*

| Component | Responsibility |
| --- | --- |
| `pulse run` | Runs the pipeline once and exits. |
| `pulse schedule` | Long-running process that calls `pulse run` on the cron schedule in config. The container entrypoint. |
| `config.yaml` | Mailbox, reviewers, schedule, sections, rules, limits, gateway connection and model names. |
| Agents | One class per model step (extractor, consolidator, drafter, judge), packaged with Pulse. |
| Run artefacts | One directory per run containing fetched messages, intermediate JSON, draft attempts, check results and the run manifest. |
| `pulse@techary.ai` | Shared mailbox. Receives submissions and sends the draft. Holds `Processed` and `Rejected` folders. |
| AI gateway | Receives all model calls from Pulse, holds provider credentials and forwards requests to the provider. |

## Process flow

```mermaid
flowchart TD
    A["1. Lock and resume"] --> B["2. Snapshot inbox<br/>message IDs"]
    B --> C["3. Pre-filter<br/>(code)"]
    C -->|rejected| X1["Rejected folder"]
    C --> D["4. Extract<br/>(LLM, per email)"]
    D --> E["5. Consolidate<br/>(code and LLM, one call)"]
    E --> F["6. Draft<br/>(LLM, one call)"]
    F --> G["7. Check<br/>(code and LLM judge)"]
    G -->|first failure| F
    G --> H["8. Render and send"]
    H --> I["9. Move messages,<br/>close manifest"]
```

*Figure 2: Pipeline for one run. Steps 4 to 7 call the model.*

1. **Lock and resume.** Pulse takes an exclusive lock on a lock file; a second concurrent run exits immediately. If the latest run manifest shows a sent draft with incomplete moves, Pulse completes those moves. It then deletes run artefacts older than `retention_days`.
2. **Snapshot.** Pulse lists every message in the inbox and records their IDs in a new run manifest. Read status is ignored: the inbox holds pending messages, and a message is done once it is moved to `Processed` or `Rejected`. Only the snapshot messages are processed and moved in this run.
3. **Pre-filter.** Pulse rejects messages whose sender domain is not in `allowed_sender_domains`, messages carrying an `Auto-Submitted` header other than `no` or an `X-Auto-Response-Suppress` header, and messages with a body shorter than `min_body_chars`. Bodies longer than `max_body_chars` are truncated.
4. **Extract.** One model call per message, run in parallel, returns an extract record.
5. **Consolidate.** Code first drops extract records with relevance below `min_relevance`. One model call over the remaining records merges duplicates and selects the headline; each item lists its source message IDs. Code then assigns each item, other than the headline, to the section for its category; the headline item appears only in the headline section. Items whose category has no section are left out of the newsletter and listed in the review section. If more than `max_items` items remain, code keeps the highest-relevance items and lists the rest in the review section.
6. **Draft.** One model call writes the newsletter from the consolidated items. It receives no raw email content and returns structured sections, not HTML.
7. **Check.** Code checks the draft against the rules in [check](#check), and a judge call verifies each sentence against its source items. On failure, Pulse regenerates the draft once with the failure reasons. If the second draft also fails, it is sent with the failures listed first in the review section.
8. **Render and send.** Pulse builds the subject from `subject_template` and the run date, renders the draft into the HTML template, appends the review section and sends it from `pulse@techary.ai` to `reviewers`, with `replyTo` set to `reviewers`. The manifest records the send.
9. **Move messages.** Pulse moves included and excluded messages to `Processed` and rejected messages to `Rejected`, creating either folder if absent, and marks the manifest complete. No message is moved before the send succeeds.

If no items remain after step 5, no newsletter is sent. Rejected messages are still moved to `Rejected`, all other messages stay in the inbox, and the run is logged.

## Model steps

Each step is an agent packaged with Pulse. Its instructions are held in a Markdown file beside the agent's class, and the drafter also uses a shared tone-of-voice file. The model for each step comes from `llm.models` in config, keyed by agent name. Configuration fails to load if an agent has no model entry or an entry names no agent. Pulse validates every response against the step's output type, and an invalid response is retried once. A second invalid response for a message that passed the pre-filter means the prompt, schema or model is faulty: the run fails, nothing is sent or moved, and the operator alert names the message, the agent and the validation error.

| Step | Agent | Calls per run | Input | Model tier |
| --- | --- | --- | --- | --- |
| Extract | `extractor` | One per message | One cleaned email | Small |
| Consolidate | `consolidator` | One | Extract records at or above `min_relevance` | Mid |
| Draft | `drafter` | One, plus at most one regeneration | Consolidated items, tone rules | Mid |
| Judge | `judge` | One per draft | Draft and consolidated items | Mid |

### Gateway connection

The runner sends every model call to `llm.base_url` in the OpenAI-compatible chat completions format that the gateway exposes, whichever provider sits behind it. The gateway credential is read from the environment variable named in `llm.api_key_env`. Model names in config are the names the gateway exposes. Pulse holds no provider credentials.

### Extract

```json
{
  "message_id": "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0A...",
  "is_update": true,
  "exclusion_reason": null,
  "category": "customer_win",
  "summary": "Sales signed a managed service contract with a new retail customer.",
  "facts": [
    "Contract signed on 22 September 2026",
    "Onboarding starts in October"
  ],
  "people": ["Priya Shah", "Tom Evans"],
  "relevance": 4,
  "sensitivity": [
    {"type": "commercial", "evidence": "mentions annual contract value"}
  ]
}
```

| Field | Values |
| --- | --- |
| `category` | `customer_win`, `delivery_highlight`, `team_news`, `shout_out`, `other` |
| `relevance` | Integer, 1 to 5 |
| `sensitivity.type` | `commercial` (deal values, margins, pricing, revenue), `personal` (health, family, performance, HR matters), `unannounced` (confidential, draft or not yet announced) |

Records with `is_update` set to `false` are excluded, with `exclusion_reason` shown in the review section.

### Consolidate

```json
{
  "headline_item_id": "item-2",
  "items": [
    {
      "item_id": "item-2",
      "category": "customer_win",
      "facts": ["Contract signed on 22 September 2026", "Onboarding starts in October"],
      "people": ["Priya Shah", "Tom Evans"],
      "source_message_ids": ["AAkALg...01", "AAkALg...07"],
      "sensitivity": [{"type": "commercial", "evidence": "mentions annual contract value"}]
    }
  ]
}
```

Where merged records have different categories, the consolidator chooses one, and code assigns the section from it.

### Draft

```json
{
  "intro": "A strong week for new customers and some well-earned thanks.",
  "headline": {"item_id": "item-2", "text": "Priya Shah and Tom Evans signed our newest retail customer, with onboarding starting in October."},
  "sections": [
    {
      "category": "shout_out",
      "entries": [
        {"item_id": "item-5", "text": "Thanks to the service desk for covering the bank holiday weekend."}
      ]
    }
  ]
}
```

Code renders the headline under `headline_title`, then each section under its configured title, in config order. The draft has no subject: code builds it from `subject_template`.

### Check

Code verifies that:

- the rendered newsletter, excluding the review section, is at most `max_words` words;
- no em dashes or en dashes remain (code replaces them before the other checks, so this is a normalisation rather than a failure);
- every number in the draft appears in a source message;
- every person named in the draft appears in a source message;
- every entry references an existing `item_id`, and every included item appears exactly once;
- only configured categories appear, and the headline entry references `headline_item_id`.

The judge call returns, for each entry, whether its text is supported by the facts of its item.

### Reviewer email

The reviewer email contains the rendered newsletter followed by a review section listing, in order:

1. check failures;
2. sensitivity flags, with item and evidence;
3. items left out because their category has no section, or because `max_items` was reached;
4. excluded messages, with sender, subject and reason;
5. rejected messages, by subject line only;
6. the source map, linking each item to its source messages by sender and subject.

## Microsoft Graph integration

Pulse authenticates as an Entra ID application using the OAuth 2.0 client credentials flow with a certificate. The file at `graph.certificate_path` holds the certificate and its private key; Pulse calculates the certificate thumbprint from it at start-up, passes it to MSAL (Microsoft Authentication Library) and logs it, so an operator can match it against the app registration. The app has no Mail permissions in Entra ID. Its mail access is granted in Exchange Online through RBAC (role-based access control) for Applications, scoped to the Pulse mailbox:

| Exchange application role | Scope | Used for |
| --- | --- | --- |
| `Application Mail.ReadWrite` | Pulse mailbox | Reading, moving and creating folders |
| `Application Mail.Send` | Pulse mailbox | Sending the draft |

The scoping consists of an Exchange service principal for the app, a management scope matching only the Pulse mailbox, and one role assignment per role within that scope.

| Operation | Request |
| --- | --- |
| Get token | `POST https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/token`, scope `https://graph.microsoft.com/.default`, signed client assertion |
| List inbox | `GET /users/pulse@techary.ai/mailFolders/inbox/messages?$select=id,from,sender,subject,receivedDateTime,uniqueBody,internetMessageHeaders&$top=50` |
| Create folder | `POST /users/pulse@techary.ai/mailFolders` |
| Move | `POST /users/pulse@techary.ai/messages/{id}/move`, body `{"destinationId": "<folder-id>"}` |
| Send | `POST /users/pulse@techary.ai/sendMail`, with `replyTo` |

Every request sends `Prefer: IdType="ImmutableId"`, so message IDs stay the same when messages move folders. List requests also send `Prefer: outlook.body-content-type="text"`, so bodies are returned as plain text. `uniqueBody` contains only the new content of a message, without quoted replies, and `internetMessageHeaders` supplies the headers used by the pre-filter. Pulse follows `@odata.nextLink` for paging and sorts results in code.

## Security

Pulse's security comes from two places: how the pipeline is built, and how it is deployed.

Within Pulse:

- recipients come only from `config.yaml`, and are validated against `allowed_recipient_domains` when configuration loads, so a mistyped or external address stops Pulse at start-up;
- agents have no tools, so model output cannot send mail, move messages or choose recipients;
- the drafter receives only extracted facts, and the check step verifies names and numbers against the source messages;
- rendering escapes all model output and email-derived text.

Deployment requirements, documented in the README and outside the codebase:

- the production mailbox is configured with `RequireSenderAuthenticationEnabled`, so Exchange rejects mail from unauthenticated or external senders;
- Exchange scoping, described in [Microsoft Graph integration](#microsoft-graph-integration), limits the app to the Pulse mailbox;
- prompt filtering is applied at the AI gateway if the gateway provides it.

## Failure handling and observability

The run manifest records the snapshot of message IDs, the outcome for each message, the send status and completed moves.

| Failure point | Behaviour |
| --- | --- |
| Before the send | Nothing is sent or moved. The next run processes the same messages. |
| Invalid model response after one retry | The run fails before the send. The operator alert names the message, the agent and the validation error. |
| During the send | No messages are moved. A retry may send a second draft to reviewers. |
| After the send, during moves | The next run completes the moves before processing anything else. |
| SIGTERM | Pulse stops at the next step boundary and exits. The manifest records progress. |
| Graph HTTP 429 | Pulse waits for the `Retry-After` interval and retries, up to `graph.max_retries`. |

Each run writes its artefacts to a dated directory under `run_artefacts_dir`, readable only by the account Pulse runs as. Logs are structured JSON on standard output. A failed run sends an alert from the Pulse mailbox to `operator_alerts`.

`pulse run` accepts `--dry-run`, which runs every step without sending or moving.

## Packaging

The repository produces a Python package providing the `pulse` command, and a container image built from that package with `pulse schedule` as its entrypoint. The container reads `config.yaml` from a mounted path, takes the gateway credential from an environment variable, reads the certificate from a mounted path, writes artefacts to a mounted volume and logs to standard output.

## Configuration

```yaml
graph:
  tenant_id: <tenant-id>
  client_id: <client-id>
  certificate_path: /run/secrets/pulse.pem
  max_retries: 5

mailbox:
  address: pulse@techary.ai
  processed_folder: Processed
  rejected_folder: Rejected

reviewers:
  - <reviewer>@techary.ai

operator_alerts:
  - <operator>@techary.ai

allowed_sender_domains:
  - techary.ai
allowed_recipient_domains:
  - techary.ai

schedule:
  cron: "30 17 * * FRI"
  timezone: Europe/London

limits:
  max_messages_per_run: 200
  max_items: 12
  max_body_chars: 4000
  min_body_chars: 20
  min_relevance: 3
  max_words: 400

subject_template: "Pulse: week ending {week_ending}"

headline_title: "Headline of the week"

sections:
  - {category: customer_win, title: "Customer wins"}
  - {category: delivery_highlight, title: "Delivery highlights"}
  - {category: team_news, title: "Team news and new joiners"}
  - {category: shout_out, title: "Shout-outs"}

llm:
  base_url: http://localhost:3000
  api_key_env: PULSE_LLM_API_KEY
  models:
    extractor: <gateway-model-name>
    consolidator: <gateway-model-name>
    drafter: <gateway-model-name>
    judge: <gateway-model-name>

run_artefacts_dir: ./runs
retention_days: 90
```

## Development and testing

Development runs against a Microsoft 365 dev tenant with Exchange Online, separate from Techary's production tenant. Pulse uses the same `GraphMailbox` as production, with dev tenant values in config. The dev tenant contains:

| Object | Purpose |
| --- | --- |
| Shared mailbox | Receives submissions and sends the draft. Unlicensed. |
| Licensed test user | Internal sender and sole reviewer. |
| App registration | Pulse's identity, authenticated by a certificate whose private key stays in the development workspace. |
| Exchange scoping | Service principal, management scope and role assignments limiting the app to the shared mailbox. |

The dev mailbox accepts external senders, and the dev config adds the Techary work domain to `allowed_sender_domains`, so submissions can be sent from Techary work accounts. `reviewers` and `allowed_recipient_domains` are limited to the test user and the dev tenant domain.

Automated tests run without a tenant or gateway; evaluations run against the dev gateway when instructions or models change:

| Layer | Scope | Method |
| --- | --- | --- |
| Automated tests, every commit | Pre-filter, extract to check, recipient validation, send and move ordering, crash recovery, agent validation and retry | `FakeMailbox`, with each agent's model replaced by a Pydantic AI stand-in returning JSON set by the test |
| Graph client, every commit | Requests, paging, throttling, errors | Mocked HTTP responses, including injected errors |
| Evaluations, on demand | Classification, sensitivity, merging, drafting, injection resistance | The synthetic corpus with expected labels, run against the dev gateway and scored against `tests/evals/thresholds.yaml` |

The synthetic corpus contains genuine updates, duplicate reports of the same news, out-of-office replies, external senders, sensitive content, long reply chains, empty messages and a prompt-injection attempt.

## Glossary

| Term | Meaning |
| --- | --- |
| Agent | Self-contained class for one model step, declaring its instructions, model, input and output types, output checks and permitted tools. |
| AI gateway | Proxy between Pulse and model providers that holds provider credentials and forwards requests. |
| Fake mailbox | Test stand-in for the mailbox that holds messages in memory and sends nothing. |
| HITL | Human in the loop: a person who reviews output before it takes effect. |
| Immutable ID | Graph message ID that stays the same when a message moves folders. |
| LLM | Large language model. |
| Microsoft Graph | Microsoft's API (application programming interface) for Microsoft 365 data, including mail. |
| Model as judge | A model call that assesses another model call's output against stated criteria. |
| Prompt injection | Text in an input that attempts to override a model's instructions. |
| RBAC for Applications | Exchange Online feature that limits an application's mail permissions to specific mailboxes. |
| Run manifest | JSON file per run recording processed messages and run progress. |
