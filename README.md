# Techary Pulse

Techary Pulse is a service that turns staff updates emailed to a Microsoft 365 submissions mailbox into a newsletter. A chat agent drafts each edition, discusses it with human reviewers over email or LibreChat, revises it from their feedback and, once a reviewer approves it, sends it to an all-staff distribution list. This README covers what Pulse needs to run and how to work on it. Behaviour is defined in the [design document](docs/techary-pulse-design.md), and delivery is planned in the [development plan](docs/development-plan.md).

Pulse is under development. The build workflow works today as `pulse build`, which creates a draft edition, if none is open, and emails version 1 to reviewers from the conversation mailbox; `pulse serve` is not implemented, and `pulse schedule` remains its stub until then. The development plan adds the conversation, approval and all-staff send in phases 6 to 8. The running and development instructions below describe what works now.

## How it works

A build reads the messages in the submissions mailbox inbox, rejects anything that is not a genuine update from an allowed sender, and uses a fixed sequence of model calls through an AI gateway to extract, consolidate and draft the updates. Code checks the draft, renders it with a review section listing sensitivity flags, exclusions and sources, and emails it to the configured reviewers. Processed messages then move to the `Processed` or `Rejected` folder. A build runs on a schedule, when a reviewer asks, or from the command line, and at most one edition is open at a time.

Reviewers reply to the draft by email, or talk to Pulse in LibreChat, with questions, feedback or approval. The chat agent answers, revises the draft and sends each new version to the reviewers. When a reviewer approves the current version, Pulse sends it to the all-staff list straight away or at the next configured send slot. A reviewer can withdraw an approval until the send starts, an unapproved edition expires after a configured period, and a reviewer can discard an edition.

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

A container runtime on a server with outbound access to `graph.microsoft.com`, `login.microsoftonline.com` and the gateway. The container needs:

- `/etc/pulse/config.yaml`, copied from [config/config.example.yaml](config/config.example.yaml);
- `/etc/pulse/pulse.env`, containing `PULSE_LLM_API_KEY=<gateway credential>`;
- `/etc/pulse/pulse.pem`, the certificate and its private key;
- `/var/lib/pulse/runs`, a writable directory for run artefacts;
- `/var/lib/pulse/state`, a writable directory for the edition store.

The files under `/etc/pulse` must be readable only by the account that runs the container.

## Configuration

Pulse stops at start-up if any reviewer or alert address is outside `allowed_recipient_domains`.

## Running

```bash
docker build -t techary-pulse .
docker run --rm \
  -v /etc/pulse/config.yaml:/config/config.yaml:ro \
  -v /etc/pulse/pulse.pem:/run/secrets/pulse.pem:ro \
  -v /var/lib/pulse/runs:/var/lib/pulse/runs \
  -v /var/lib/pulse/state:/var/lib/pulse/state \
  --env-file /etc/pulse/pulse.env \
  techary-pulse
```

The container runs `pulse schedule` by default, which is not yet implemented. `pulse build` creates a draft edition, if none is open, and exits; `pulse build --dry-run` runs every step without sending or moving mail.

## Development

Development needs [uv](https://docs.astral.sh/uv/), which installs the Python version and dependencies Pulse uses.

To run Pulse locally against the dev tenant:

1. Copy `config/config.dev.example.yaml` to `config.yaml` at the repository root, fill in the dev tenant and gateway values, and set `mailboxes.submissions` and `mailboxes.conversation` to the two dev tenant mailbox addresses.
2. Put `PULSE_LLM_API_KEY=<dev gateway credential>` in `.env` at the repository root.
3. Put the dev certificate outside the repository, and set `graph.certificate_path` to its full path.
4. Run `uv run --env-file .env pulse build --dry-run`.

To run the synthetic test emails through the agents on the dev gateway, after steps 1 and 2, run `uv run --env-file .env pytest -m live -s`. It is a dry run against a fake mailbox, so nothing is sent or moved, and it prints where the reviewer email is saved.

`config.yaml`, `.env`, `*.pem`, `runs/` and `state/` are git-ignored.

| Command | Purpose |
| --- | --- |
| `uv sync` | Install dependencies |
| `uv run ruff format` | Format code |
| `uv run ruff check` | Lint |
| `uv run mypy src tests` | Type check |
| `uv run pytest` | Run tests; `live` tests are excluded by default |
| `uv run --env-file .env pytest -m live -s` | Run the synthetic emails through the agents on the dev gateway, as a dry run, and print where the reviewer email is saved |
| `uv run --env-file .env pulse build --dry-run` | Run one build locally with `./config.yaml` and the credentials in `./.env` |
| `uv lock --upgrade` | Upgrade every dependency to its latest version, then run the checks |

Run format, lint, type check and tests before every commit, and after every upgrade.
