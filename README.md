# Agent Pipeline

Small FastAPI service that turns GitHub events into isolated Pi agent runs.

## Workflow

- Every recognized human issue, comment, or review event starts a read-only Pi decision run.
- Decision run reads issue context and current plan/implementation PR state, then chooses: plan, reply, implement, recreate a discarded PR, ask for clarification, or do nothing.
- Uncertain intent produces a contextual GitHub question; its answer arrives as another event.
- Implementation and PR recreation require explicit evidence from latest message plus GitHub `write`, `maintain`, or `admin` permission.
- Replacement PRs are allowed only when previous PR is closed and unmerged; replacement gets unique branch.
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
- `run.sh` loads `.env` and starts one Uvicorn process on `${HOST:-127.0.0.1}:${PORT:-8000}`. Set `HOST=0.0.0.0` only for direct IP access; prefer Caddy/TLS in production.
- `tests.sh` runs unit tests, then integration tests. Tests use fake GitHub/Pi boundaries and need no credentials or network.

Open <http://127.0.0.1:8000/> and authenticate with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`. For temporary VPS-IP access, set `HOST=0.0.0.0` and open `http://<vps-ip>:<PORT>/`; Basic Auth is not encrypted without HTTPS.

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
- Events: Issues, Issue comments, Pull request reviews, Pull request review comments

Set `GITHUB_BOT_LOGIN` to machine-account login. App ignores that login and its hidden publication markers to prevent comment loops.

## Configuration

Copy `.env.example` to `.env`. Required values:

- `GITHUB_REPOSITORY=owner/name`
- `GITHUB_TOKEN`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_BOT_LOGIN`
- `MODEL`
- `REASONING_LEVEL`
- `DASHBOARD_USER`
- `DASHBOARD_PASSWORD`

Set `MODEL` to the Pi model pattern or ID used for every run (for example,
`anthropic/claude-sonnet-4-5`). The same value appears in GitHub attribution
footers when Pi does not report a more specific resolved model. Set
`REASONING_LEVEL` to `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, or
`max`; it is passed to Pi as `--thinking`.

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
MODEL=anthropic/claude-sonnet-4-5
REASONING_LEVEL=medium
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

### Nginx subpath

For `https://azamat.tech/agent_runner/`, add to `/etc/agent-pipeline.env`:

```dotenv
ROOT_PATH=/agent_runner
FORWARDED_ALLOW_IPS=127.0.0.1
```

Use a trailing slash on `proxy_pass` so Nginx strips the public prefix before forwarding:

```nginx
location = /agent_runner {
    return 301 /agent_runner/;
}

location /agent_runner/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Do not add a trailing slash to `ROOT_PATH`. If Nginx runs on another host, set `FORWARDED_ALLOW_IPS` to that proxy's IP. The webhook URL becomes `https://azamat.tech/agent_runner/webhooks/github`.

Inspect failures:

```bash
journalctl -u agent-pipeline -f
systemctl status agent-pipeline
```

## Security boundary

Webhook signature is verified from raw body before JSON processing. Delivery IDs are deduplicated in SQLite. Pi runs as `pi-runner`; repository tests run as `agent-test`. Bubblewrap exposes only current run worktree, hiding concurrent worktrees. Neither account can read `/etc/agent-pipeline.env`, SQLite state, or GitHub credentials. App performs commits, pushes, comments, and PR creation after validating agent output.

Agent and test processes still have host network and process resources. Keep repository and write-authorized collaborators trusted. Use container isolation before accepting untrusted public repositories.
