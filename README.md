# Techary Pulse

Techary Pulse turns staff updates emailed to a Microsoft 365 submissions mailbox into a newsletter. An orchestrator agent drafts each newsletter, discusses it with reviewers over email or LibreChat, revises it from their feedback and, once a reviewer approves it, sends it to an all-staff distribution list. This README covers what Pulse needs to run and how to work on it. Behaviour is defined in the [design document](docs/techary-pulse-design.md), and delivery is planned in the [development plan](docs/development-plan.md).

Pulse is being rebuilt to the current design. So far, `pulse serve` runs the chat endpoint and the email channel, where reviewers talk to the orchestrator, and delivery. The orchestrator can start a newsletter from the submissions mailbox, draft, check and present it, record approval, withdraw an approval and abandon a newsletter; delivery sends an approved newsletter to all staff at its send time. Reviewers can reply to any reviewer email, and the reply comes back in the same thread. The schedule is still to come, as the development plan sets out.

## How it works

Staff email updates to the submissions mailbox at any time. On a schedule, when a reviewer asks, or when an operator runs `pulse draft`, the orchestrator takes every pending message, has code reject anything that is not from an allowed sender, and uses specialist agents to extract, consolidate and write the updates into a draft. It emails each draft to the reviewers with a review section listing check results, exclusions and sources.

Reviewers reply by email, or talk to the orchestrator in LibreChat, with questions, feedback or approval. The orchestrator answers, revises the draft and emails each new version. An approved newsletter is sent to the all-staff list at the configured send time, or straight away if that time has passed, and its emails then move to the `Processed` or `Rejected` folder. A reviewer can withdraw an approval until the send starts, or ask for a newsletter to be abandoned.

## Deployment requirements

Pulse connects to mailboxes, a distribution list, a gateway and a chat client that are set up outside this codebase. It does not create or check any of the following.

### Microsoft 365

| Requirement | Detail |
| --- | --- |
| Submissions mailbox | A shared mailbox that receives staff updates, named by `mailboxes.submissions`. Pulse creates the `Processed` and `Rejected` folders if they are absent. |
| Conversation mailbox | A shared mailbox that sends drafts, replies and the newsletter, and receives reviewer messages, named by `mailboxes.conversation`. Pulse creates the `Processed` and `Rejected` folders if they are absent. It accepts mail only from members of the reviewers list. |
| Reviewers list | A distribution list of the people who review newsletters. Drafts and notices are sent to it. |
| Internal senders only | In production, both mailboxes have `RequireSenderAuthenticationEnabled` set, so Exchange rejects mail from unauthenticated or external senders. |
| All-staff list | The distribution list that receives the approved newsletter. It accepts mail only from the conversation mailbox and named administrators. |
| App registration | An Entra ID app registration authenticated by a certificate. It defines the `agent.pulse` app role, assigned to the reviewers, and its client ID is the audience of tokens for Pulse's chat endpoint. It needs no Entra ID permissions, and no Mail permissions in Entra ID. |
| Mailbox permissions | Exchange Online RBAC (role-based access control) for Applications, scoped to the two Pulse mailboxes only: `Application Mail.ReadWrite` (read, move, create folders, create replies) and `Application Mail.Send` (send drafts, replies, alerts and the newsletter). |
| Sensitivity labels | If messages carry Microsoft Purview sensitivity labels, the IDs of the labels Pulse may process go in `allowed_sensitivity_labels`. Pulse reads the label from each message's `msip_labels` header and rejects any other label; unlabelled messages are allowed. |
| Certificate | A PEM file holding the certificate and its private key, mounted into the container. Pulse calculates the thumbprint from it and logs it at start-up, so it can be matched against the app registration. |

### AI gateway

An AI gateway, currently agentgateway, exposing an OpenAI-compatible chat completions endpoint for Pulse's model calls. Pulse holds only the gateway credential, never provider credentials. Prompt filtering, if the gateway offers it, is configured on the gateway.

The gateway also routes LibreChat to Pulse's chat endpoint, through a route in its `config.yaml`:

| Part | Setting |
| --- | --- |
| Match | Path prefix `/agents/pulse/v1/`, rewritten to `/v1/` before forwarding |
| Authentication | `jwtAuth`, with the same issuer, audience and key set as the gateway's model traffic |
| Authorisation | The rule `jwt.roles.exists(r, r == "agent.pulse")`, so only reviewers reach Pulse |
| Backend | Pulse's host and `chat.port`, with `backendAuth: passthrough`, so the caller's token reaches Pulse unchanged |

Pulse verifies the token again against `auth`, because it must also refuse callers that do not come through the gateway.

### LibreChat

LibreChat reaches Pulse through the gateway route, registered in `librechat.yaml` as a custom endpoint:

```yaml
- name: "Pulse"
  apiKey: "${GATEWAY_TOKEN}"
  baseURL: "http://<gateway host>:<gateway port>/agents/pulse/v1"
  models:
    default: ["pulse"]
    fetch: false        # Pulse has no model list
  titleConvo: false     # otherwise LibreChat's title requests reach Pulse as reviewer messages
  modelDisplayLabel: "Pulse"
```

In production, LibreChat users sign in through Entra ID and LibreChat passes each user's own token, which carries `agent.pulse` when the user is assigned that app role. Until then, LibreChat sends one shared token, so in dev that token carries `agent.pulse` and every LibreChat user acts as one reviewer.

### Host

A container runtime on a server with outbound access to `graph.microsoft.com`, `login.microsoftonline.com` and the gateway, accepting connections on `chat.port` from the gateway only. The container needs:

- `/etc/pulse/config.yaml`, copied from [config/config.example.yaml](config/config.example.yaml);
- `/etc/pulse/pulse.env`, containing `PULSE_LLM_API_KEY=<gateway credential>`;
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

Copy [config/config.dev.example.yaml](config/config.dev.example.yaml) to `config.yaml` and fill in the dev tenant and gateway values, with `auth.jwks` pointing at the stand-in issuer's key set file. Put `PULSE_LLM_API_KEY` in `.env`, then start the chat endpoint and delivery:

```sh
uv run --env-file .env pulse serve
```

Every `schedule.poll_interval_seconds`, delivery sends an approved newsletter whose send time has come; with the dev configuration's `send.mode: on_approval`, that is the first poll after approval. A sent newsletter's emails move out of the dev submissions inbox; before drafting again, empty the inbox and reseed the corpus with `uv run --env-file .env pytest -m live -k test_seed_the_corpus -s`. To start again from a clean dev inbox, stop Pulse and delete `state/pulse.db`, so the store and the mailbox match. The store has no migrations, so also delete `state/pulse.db` after a change to its tables.

Pulse reads `./config.yaml` unless `--config` names another file. It listens on `chat.port`, which must be free on the machine. To check the endpoint, send a non-streamed request, then the same request with `"stream": true` and `curl -N`, which returns progress notes and the reply as server-sent events ending in `data: [DONE]`. `TOKEN` is a token from the issuer in `auth`, for the audience in `auth.audience`, carrying the reviewer role; without the role, Pulse returns HTTP 403:

```sh
TOKEN=<token from the issuer in auth>
curl -s localhost:<chat.port>/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "pulse", "messages": [{"role": "user", "content": "Hello"}]}'
```
