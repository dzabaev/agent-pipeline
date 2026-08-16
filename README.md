# Agent Pipeline

Small FastAPI service that turns GitHub events into isolated Pi agent runs.

## Workflow

- New issue → read-only Pi run → plan-only PR at `plans/issues/<number>.md`.
- Human comment → read-only Pi review → one GitHub reply.
- Plan PR merge or approved command → Pi implementation → draft code PR.
- Approved commands use complete phrases such as `/pi implement`, `yes`, `agreed`, `do that`, or `proceed`. Mixed comments do not trigger code.
- Only users with GitHub `write`, `maintain`, or `admin` permission can approve implementation.
- `MAX_CONCURRENT_AGENTS` controls parallel runs. Every run gets its own git worktree.

The app never merges pull requests.

## Requirements

- Python 3.11+
- Git
- Node.js and npm
- Pi authentication for selected model provider

## Development

```bash
./setup.sh
$EDITOR .env
./tests.sh
./run.sh
```

Scripts:

- `setup.sh` creates `.venv`, installs app dependencies, installs project-local Pi when needed, creates `.env`, and prepares runtime directories.
- `run.sh` loads `.env` and starts one Uvicorn process on `127.0.0.1:${PORT:-8000}`.
- `tests.sh` runs unit tests, then integration tests. Tests use fake GitHub/Pi boundaries and need no credentials or network.

Open <http://127.0.0.1:8000/> and authenticate with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.

## GitHub setup

Use dedicated machine account. Create fine-grained PAT restricted to target repository:

- Metadata: read
- Contents: read/write
- Issues: read/write
- Pull requests: read/write

Do not grant Workflows permission. Implementation runs reject `.github/workflows/` changes.

Create repository webhook:

- URL: `https://<domain>/webhooks/github`
- Content type: `application/json`
- Secret: same value as `GITHUB_WEBHOOK_SECRET`
- Events: Issues, Issue comments, Pull requests, Pull request reviews, Pull request review comments

Set `GITHUB_BOT_LOGIN` to machine-account login. App ignores that login and its hidden publication markers to prevent comment loops.

## Configuration

Copy `.env.example` to `.env`. Required values:

- `GITHUB_REPOSITORY=owner/name`
- `GITHUB_TOKEN`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_BOT_LOGIN`
- `DASHBOARD_USER`
- `DASHBOARD_PASSWORD`

Important optional values:

- `MAX_CONCURRENT_AGENTS` — positive integer, default `1`
- `AGENT_TIMEOUT_SECONDS` — per-run timeout
- `PI_EXECUTABLE` — Pi path, default `pi`
- `PI_RUNNER_USER` — optional user for development; leave empty in hardened service
- `TEST_COMMAND` — command run before implementation publication
- `DATABASE_PATH`, `REPOSITORY_PATH`, `WORKTREE_ROOT` — durable state locations

## Replaceable boundaries

- `agent_pipeline/github.py` implements `CodeHost`.
- `agent_pipeline/pi.py` implements `AgentRunner`.
- `agent_pipeline/workflow.py` depends only on contracts in `agent_pipeline/contracts.py`.
- `agent_pipeline/main.py` wires concrete adapters.

To replace GitHub or Pi, implement matching protocol and change composition root. No plugin registry or factory required.

## VPS deployment

Example assumes systemd, Caddy, Bubblewrap (`bwrap`), app at `/opt/agent-pipeline`, and state at `/var/lib/agent-pipeline`.

```bash
command -v bwrap sudo git npm python3
sudo groupadd --system agent-runs
sudo useradd --system --home /var/lib/agent-pipeline --create-home agent-pipeline
sudo useradd --system --home /var/lib/pi-runner --create-home pi-runner
sudo useradd --system --home /var/lib/agent-pipeline-test --create-home agent-test
sudo chmod 0700 /var/lib/pi-runner /var/lib/agent-pipeline-test
sudo usermod -aG agent-runs agent-pipeline
sudo usermod -aG agent-runs pi-runner
sudo usermod -aG agent-runs agent-test
sudo install -d -m 2750 -o agent-pipeline -g agent-runs /opt/agent-pipeline
sudo install -d -m 0711 -o agent-pipeline -g agent-pipeline /var/lib/agent-pipeline
sudo install -d -m 2770 -o agent-pipeline -g agent-runs /var/lib/agent-pipeline/worktrees
git archive --format=tar HEAD | sudo tar -xf - -C /opt/agent-pipeline/
sudo chown -R agent-pipeline:agent-runs /opt/agent-pipeline
sudo -u agent-pipeline -H /opt/agent-pipeline/setup.sh
sudo chmod -R g+rX /opt/agent-pipeline
sudo install -m 640 -o root -g agent-pipeline .env.example /etc/agent-pipeline.env
sudo install -m 755 deploy/agent-pipeline-run-tests /usr/local/libexec/agent-pipeline-run-tests
sudo install -m 440 deploy/agent-pipeline.sudoers /etc/sudoers.d/agent-pipeline
sudo visudo -cf /etc/sudoers.d/agent-pipeline
sudo install -m 644 deploy/agent-pipeline.service /etc/systemd/system/
sudo install -m 644 deploy/Caddyfile /etc/caddy/Caddyfile
```

Edit `/etc/agent-pipeline.env`:

```dotenv
APP_ENV=production
DATABASE_PATH=/var/lib/agent-pipeline/agent-pipeline.db
REPOSITORY_PATH=/var/lib/agent-pipeline/repository.git
WORKTREE_ROOT=/var/lib/agent-pipeline/worktrees
PI_EXECUTABLE=/opt/agent-pipeline/.tools/node_modules/.bin/pi
PI_RUNNER_USER=pi-runner
TEST_RUNNER_USER=agent-test
```

Add required GitHub/dashboard secrets. Configure Caddy environment and authenticate Pi as isolated runner user:

```bash
sudo systemctl edit caddy
# Add: [Service]
# Add: Environment=DOMAIN=agents.example.com
# Add: Environment=PORT=8000
sudo -u pi-runner -H /opt/agent-pipeline/.tools/node_modules/.bin/pi
# run /login, then exit
sudo systemctl daemon-reload
sudo systemctl enable --now agent-pipeline caddy
curl -fsS https://<domain>/healthz
```

Inspect failures:

```bash
journalctl -u agent-pipeline -f
systemctl status agent-pipeline
```

## Security boundary

Webhook signature is verified from raw body before JSON processing. Delivery IDs are deduplicated in SQLite. Pi runs as `pi-runner`; repository tests run as `agent-test`. Bubblewrap exposes only current run worktree, hiding concurrent worktrees. Neither account can read `/etc/agent-pipeline.env`, SQLite state, or GitHub credentials. App performs commits, pushes, comments, and PR creation after validating agent output.

Agent and test processes still have host network and process resources. Keep repository and write-authorized collaborators trusted. Use container isolation before accepting untrusted public repositories.
