# Techary Pulse

Techary Pulse is a scheduled service that turns staff updates emailed to a shared Microsoft 365 mailbox into a weekly draft newsletter, and emails the draft to human reviewers. This README covers what Pulse needs to run and how to work on it. Behaviour is defined in the [design document](docs/techary-pulse-design.md), and delivery is planned in the [development plan](docs/development-plan.md).

Pulse is under development. The `pulse` command validates its configuration, but the pipeline itself is not yet implemented.

## How it works

Each run reads the messages in the Pulse mailbox inbox, rejects anything that is not a genuine update from an allowed sender, and uses a fixed sequence of model calls through an AI gateway to extract, consolidate and draft the updates. Code checks the draft, renders it with a review section listing sensitivity flags, exclusions and sources, and sends it to the configured reviewers. Processed messages then move to the `Processed` or `Rejected` folder.

## Deployment requirements

Pulse connects to a mailbox and a gateway that are set up outside this codebase. It does not create or check any of the following.

### Microsoft 365

| Requirement | Detail |
| --- | --- |
| Shared mailbox | The Pulse mailbox, for example `pulse@techary.ai`. Pulse creates the `Processed` and `Rejected` folders if they are absent. |
| Internal senders only | In production, the mailbox has `RequireSenderAuthenticationEnabled` set, so Exchange rejects mail from unauthenticated or external senders. |
| App registration | An Entra ID app registration authenticated by a certificate. The app needs no Mail permissions in Entra ID. |
| Mailbox permissions | Exchange Online RBAC (role-based access control) for Applications, scoped to the Pulse mailbox only: `Application Mail.ReadWrite` (read, move, create folders) and `Application Mail.Send` (send the draft). |
| Sensitivity labels | If messages carry Microsoft Purview sensitivity labels, the IDs of the labels Pulse may process go in `allowed_sensitivity_labels`. Pulse reads the label from each message's `msip_labels` header and rejects any other label; unlabelled messages are allowed. |
| Certificate | A PEM file holding the certificate and its private key, mounted into the container. Pulse calculates the thumbprint from it and logs it at start-up, so it can be matched against the app registration. |

### AI gateway

An AI gateway, currently agentgateway, exposing an OpenAI-compatible chat completions endpoint. Pulse holds only the gateway credential, never provider credentials. Prompt filtering, if the gateway offers it, is configured on the gateway.

### Host

A container runtime on a server with outbound access to `graph.microsoft.com`, `login.microsoftonline.com` and the gateway. The container needs:

- `/etc/pulse/config.yaml`, copied from [config/config.example.yaml](config/config.example.yaml);
- `/etc/pulse/pulse.env`, containing `PULSE_LLM_API_KEY=<gateway credential>`;
- `/etc/pulse/pulse.pem`, the certificate and its private key;
- `/var/lib/pulse/runs`, a writable directory for run artefacts.

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
  --env-file /etc/pulse/pulse.env \
  techary-pulse
```

The container runs `pulse schedule` by default. `pulse run` runs the pipeline once, and `pulse run --dry-run` runs every step without sending or moving mail.

## Development

Development needs [uv](https://docs.astral.sh/uv/), which installs the Python version and dependencies Pulse uses.

To run Pulse locally against the dev tenant:

1. Copy `config/config.dev.example.yaml` to `config.yaml` at the repository root and fill in the dev tenant and gateway values.
2. Put `PULSE_LLM_API_KEY=<dev gateway credential>` in `.env` at the repository root.
3. Put the dev certificate outside the repository, and set `graph.certificate_path` to its full path.
4. Run `uv run --env-file .env pulse run --dry-run`.

To run the synthetic test emails through the agents on the dev gateway, after steps 1 and 2, run `uv run --env-file .env pytest -m live -s`. It is a dry run against a fake mailbox, so nothing is sent or moved, and it prints where the reviewer email is saved.

`config.yaml`, `.env`, `*.pem` and `runs/` are git-ignored.

| Command | Purpose |
| --- | --- |
| `uv sync` | Install dependencies |
| `uv run ruff format` | Format code |
| `uv run ruff check` | Lint |
| `uv run mypy src tests` | Type check |
| `uv run pytest` | Run tests; `live` tests are excluded by default |
| `uv run --env-file .env pytest -m live -s` | Run the synthetic emails through the agents on the dev gateway, as a dry run, and print where the reviewer email is saved |
| `uv lock --upgrade` | Upgrade every dependency to its latest version |

Run format, lint, type check and tests before every commit, and after every upgrade.
