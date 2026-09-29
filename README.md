# Techary Pulse

Techary Pulse turns staff updates emailed to a Microsoft 365 submissions mailbox into a newsletter. An orchestrator agent drafts each newsletter, discusses it with reviewers over email or LibreChat, revises it from their feedback and, once a reviewer approves it, sends it to an all-staff distribution list. This README covers what Pulse needs to run and how to work on it. Behaviour is defined in the [design document](docs/techary-pulse-design.md), and delivery is planned in the [development plan](docs/development-plan.md).

Pulse is being rebuilt to the current design. So far, `pulse serve` runs the chat endpoint, where reviewers talk to the orchestrator; the orchestrator has no tools yet. The development plan sets out the order in which the rest arrives.

## How it works

Staff email updates to the submissions mailbox at any time. On a schedule, when a reviewer asks, or when an operator runs `pulse draft`, the orchestrator takes every pending message, has code reject anything that is not from an allowed sender, and uses specialist agents to extract, consolidate and write the updates into a draft. It emails each draft to the reviewers with a review section listing check results, exclusions and sources.

Reviewers reply by email, or talk to the orchestrator in LibreChat, with questions, feedback or approval. The orchestrator answers, revises the draft and emails each new version. An approved newsletter is sent to the all-staff list at the configured send time, or straight away if that time has passed, and its submissions then move to the `Processed` or `Rejected` folder. A reviewer can withdraw an approval until the send starts, or ask for a newsletter to be abandoned.

## Deployment requirements

Pulse connects to mailboxes, a distribution list, a gateway and a chat client that are set up outside this codebase. It does not create or check any of the following.

### Microsoft 365

| Requirement | Detail |
| --- | --- |
| Submissions mailbox | A shared mailbox that receives staff updates, for example `pulse@techary.ai`. Pulse creates the `Processed` and `Rejected` folders if they are absent. |
| Conversation mailbox | A shared mailbox that sends drafts, replies and the newsletter, and receives reviewer messages, for example `pulseagent@techary.ai`. Pulse creates the `Processed` and `Rejected` folders if they are absent. |
| Internal senders only | In production, both mailboxes have `RequireSenderAuthenticationEnabled` set, so Exchange rejects mail from unauthenticated or external senders. |
| All-staff list | The distribution list that receives the approved newsletter. It accepts mail only from the conversation mailbox and named administrators. |
| App registration | An Entra ID app registration authenticated by a certificate. The app needs no Mail permissions in Entra ID. |
| Mailbox permissions | Exchange Online RBAC (role-based access control) for Applications, scoped to the two Pulse mailboxes only: `Application Mail.ReadWrite` (read, move, create folders, create replies) and `Application Mail.Send` (send drafts, replies, alerts and the newsletter). |
| Sensitivity labels | If messages carry Microsoft Purview sensitivity labels, the IDs of the labels Pulse may process go in `allowed_sensitivity_labels`. Pulse reads the label from each message's `msip_labels` header and rejects any other label; unlabelled messages are allowed. |
| Certificate | A PEM file holding the certificate and its private key, mounted into the container. Pulse calculates the thumbprint from it and logs it at start-up, so it can be matched against the app registration. |

### AI gateway

An AI gateway, currently agentgateway, exposing an OpenAI-compatible chat completions endpoint for Pulse's model calls. Pulse holds only the gateway credential, never provider credentials. Prompt filtering, if the gateway offers it, is configured on the gateway.

The gateway also routes LibreChat to Pulse's chat endpoint, registered as an OpenAI-compatible backend. The route authenticates LibreChat, sends a second gateway credential to Pulse as a bearer token through agentgateway's `backendAuth` policy, and forwards the `X-User-Email` header unchanged.

### LibreChat

LibreChat reaches Pulse through the gateway as a custom endpoint and passes the signed-in user's email address in `X-User-Email`. In production, LibreChat users sign in through Entra ID.

### Host

A container runtime on a server with outbound access to `graph.microsoft.com`, `login.microsoftonline.com` and the gateway, accepting connections on `chat.port` from the gateway only. The container needs:

- `/etc/pulse/config.yaml`, copied from [config/config.example.yaml](config/config.example.yaml);
- `/etc/pulse/pulse.env`, containing `PULSE_LLM_API_KEY=<gateway credential>` and `PULSE_CHAT_GATEWAY_KEY=<chat endpoint credential>`;
- `/etc/pulse/pulse.pem`, the certificate and its private key;
- `/var/lib/pulse/state`, a writable directory for the store.

The files under `/etc/pulse` must be readable only by the account that runs the container. The container runs `pulse serve --config /config/config.yaml`, so `config.yaml` is mounted at `/config/config.yaml`, and `state.db_path` points into the mounted state directory.

## Configuration

Pulse reads `config.yaml`, described in the [design](docs/techary-pulse-design.md#configuration). It stops at start-up if any recipient is outside `allowed_recipient_domains`.

## Development

Development needs [uv](https://docs.astral.sh/uv/), which installs the Python version and dependencies Pulse uses. Development runs against the dev tenant and dev gateway, never the production mailboxes.

`config.yaml`, `.env`, `*.pem` and `state/` are git-ignored.

| Command | Purpose |
| --- | --- |
| `uv sync` | Install dependencies |
| `uv run ruff format` | Format code |
| `uv run ruff check` | Lint |
| `uv run mypy src tests` | Type check |
| `uv run pytest` | Run tests; `live` tests are excluded by default |
| `uv run --env-file .env pytest -m live -s` | Run the tests that call the dev tenant or gateway |
| `uv lock --upgrade` | Upgrade every dependency to its latest version, then run the checks |
| `docker build -t techary-pulse .` | Build the container image |

Run format, lint, type check and tests before every commit, and after every upgrade.

### Running Pulse locally

Copy [config/config.dev.example.yaml](config/config.dev.example.yaml) to `config.yaml` and fill in the dev tenant and gateway values. Put `PULSE_LLM_API_KEY` and `PULSE_CHAT_GATEWAY_KEY` in `.env`, then start the chat endpoint:

```sh
uv run --env-file .env pulse serve
```

Pulse reads `./config.yaml` unless `--config` names another file. It listens on `chat.port`, which must be free on the machine. To check the endpoint, send a non-streamed request, then the same request with `"stream": true` and `curl -N`, which returns progress notes and the reply as server-sent events ending in `data: [DONE]`:

```sh
set -a; . ./.env; set +a
curl -s localhost:<chat.port>/v1/chat/completions \
  -H "Authorization: Bearer $PULSE_CHAT_GATEWAY_KEY" \
  -H "X-User-Email: <reviewer address from config.yaml>" \
  -H "Content-Type: application/json" \
  -d '{"model": "pulse", "messages": [{"role": "user", "content": "Hello"}]}'
```
