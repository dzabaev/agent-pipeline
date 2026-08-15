# Agent Pipeline

Self-hosted server that turns GitHub events into Pi coding-agent jobs. Advisory jobs post comments. Change jobs edit code, run repository verification, and open draft pull requests.

## Stack

- FastAPI, Jinja, HTMX
- SQLite-backed durable queue
- GitHub App webhooks and installation API
- Pi RPC subprocess per job
- Host Git worktrees

GitHub and Pi are hardcoded first-release integrations. Their code is isolated in `app/github.py` and `app/pi.py`; orchestration stays in `app/jobs.py`.

## GitHub App setup

Create a GitHub App owned by your account.

Repository permissions:

- Contents: read and write
- Issues: read and write
- Pull requests: read and write
- Metadata: read

Subscribe to:

- Issues
- Issue comments
- Pull requests
- Pull request reviews

Set webhook URL to `https://your-host/webhooks/github`. Set callback URL to `https://your-host/auth/callback`. Install App only on repositories server may automate. Copy App ID, client ID, client secret, webhook secret, private key, installation ID, and your numeric GitHub user ID.

Pi never receives GitHub credentials. Server fetches and publishes with short-lived installation tokens.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

Set variables in `.env`. Generate session secret with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Production requires separate `agent-pipeline-worker` OS user for Pi. Authenticate Pi as that user and never configure `gh` or GitHub App credentials there:

```bash
sudo useradd --system --create-home agent-pipeline-worker
sudo usermod -aG agent-pipeline agent-pipeline-worker
sudo -H -u agent-pipeline-worker pi   # use /login, then exit
sudo install -m 0440 deploy/agent-pipeline.sudoers /etc/sudoers.d/agent-pipeline
sudo visudo -cf /etc/sudoers.d/agent-pipeline
```

Keep private key and SQLite file mode `0600`, owned by server user. Make only `data/worktrees` and `data/sessions` group-writable by shared `agent-pipeline` group. `PI_RUN_AS_USER=agent-pipeline-worker` runs both Pi and repository verification under worker identity with group-writable umask. Sudoers grants shell execution only as less-privileged worker because verification commands are user-configured shell commands.

Production also requires cgroup v2 isolation. Keep `REQUIRE_CGROUP_ISOLATION=true`; systemd unit uses `Delegate=yes`. Each Pi/verification run gets child cgroup, and server kills whole cgroup before inspecting or publishing files. This prevents detached/background descendants from changing code after verification. For local-only development, set `PI_RUN_AS_USER=current` and `REQUIRE_CGROUP_ISOLATION=false`; this bypasses production isolation.

Run development server:

```bash
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Use one Uvicorn process. Host-worktree mode enforces worker concurrency `1`; multiple Uvicorn workers would create competing schedulers. Parallel jobs require per-run filesystem identities or containers.

## First use

1. Sign in through GitHub.
2. Add repository using `owner/name`, GitHub App installation ID, and deterministic verification command such as `pytest` or `npm test`.
3. Add structured rules or launch manual job.
4. Watch job page; send steering or follow-up instructions, cancel, retry manually, or continue prior Pi session.

Rules support issue, issue-comment, pull-request, and pull-request-review events. Optional filters cover action, subject kind, labels, sender, author association, body text, base branch, changed-path globs, review state, and draft status.

## Job safety

- Run API under `agent-pipeline`; run Pi under separate `agent-pipeline-worker` identity.
- Keep GitHub secrets and SQLite mode `0600`, readable only by API account.
- Pi receives sanitized environment, different UID, and per-run cgroup; it cannot read same-UID `/proc` secrets or leave background descendants.
- Agent cannot publish directly; server owns comments, commits, pushes, and draft PR creation.
- Change job publishes only when repository verification exits zero without changing files.
- Server never merges pull requests.
- Change jobs on pull requests from forks are rejected.
- Successful worktrees are removed immediately. Failed, cancelled, and interrupted worktrees remain for 72 hours.

Host worktrees are not strong sandboxing. Runs remain serial because they share worker identity. Use per-run containers or dedicated VMs before enabling parallel jobs or accepting events from untrusted public contributors.

## Test

```bash
.venv/bin/python -m compileall -q app tests
.venv/bin/pytest -q
```

## Production

`deploy/agent-pipeline.service` and `deploy/agent-pipeline.sudoers` provide production baseline. Adjust paths, create server/worker users and shared group, place environment at `/etc/agent-pipeline.env`, validate sudoers, and serve Uvicorn through TLS reverse proxy.
