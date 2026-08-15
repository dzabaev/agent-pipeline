# GitHub-driven Pi agent pipeline

## Goal

Run one trusted repository automation service on a private VPS:

1. New issue creates a plan-only pull request.
2. Human comments receive a Pi review or suggested answer.
3. Plan merge or authorized approval starts implementation.
4. Implementation creates one code pull request; app never merges it.
5. Up to configured `MAX_CONCURRENT_AGENTS` Pi runs execute in parallel.
6. Small web dashboard shows deliveries, runs, output, failures, and retry controls.

## Minimal architecture

```text
GitHub webhook
    |
    v
FastAPI -> GitHub adapter -> SQLite run queue
    |                            |
    |                            v
    +-> dashboard          N workflow workers
                                  |
                    +-------------+-------------+
                    v                           v
              Pi adapter                 GitHub adapter
              isolated worktrees         comments/branches/PRs
```

Run one Uvicorn process. `MAX_CONCURRENT_AGENTS` is a positive integer, defaults to `1`, and sets parallel agent capacity. FastAPI `lifespan` starts exactly that many queue loops. SQLite rows remain the durable queue; no `BackgroundTasks`, Redis, or Celery.

## Replaceable boundaries

Core workflow depends on two small `typing.Protocol` contracts:

- `CodeHost` — verify and normalize webhook events, fetch conversation context, check actor permission, post comments, publish branches, and open pull requests.
- `AgentRunner` — accept normalized mode/prompt/worktree request and return normalized output/events/error.

Concrete adapters stay separate:

- `github.py` implements `CodeHost`; no Pi command or workflow state logic.
- `pi.py` implements `AgentRunner`; no GitHub payload, token, comment, or pull-request logic.
- `worker.py` orchestrates normalized contracts; it imports neither GitHub payload types nor Pi JSON event shapes.
- `main.py` is composition root and directly wires `GitHubCodeHost` and `PiAgentRunner`.

Use dataclasses for normalized events and results. No provider registry, dynamic plugins, factories, or configuration-driven class loading. Replacing either adapter means implementing one protocol and changing composition root.

MVP ships only GitHub and Pi implementations. GitLab, Forgejo, other code hosts, and other agent runtimes remain deferred.

## Event rules

### New issue

Handle `issues.opened`:

1. Insert delivery using `X-GitHub-Delivery` as unique key.
2. Queue one `plan` run for issue.
3. Create clean worktree from current default branch.
4. Run Pi with read-only tools: `read,grep,find,ls`.
5. Require Markdown output and no repository changes.
6. App writes only `plans/issues/<number>.md`.
7. App commits, pushes `agent/plan-<number>`, and opens plan PR.
8. App comments on issue with PR link.

### Human comment

Handle conversation comments and inline review comments:

- `issue_comment.created`
- `pull_request_review_comment.created`
- non-empty `pull_request_review.submitted`

Ignore configured machine account and comments containing app publication marker.

Normal comment:

1. Fetch issue/PR body plus relevant conversation and linked agent PRs.
2. Run Pi with read-only tools.
3. Post one reply containing hidden marker `<!-- agent-pipeline:<delivery-id> -->`.

Approval-like comment:

1. Normalize case, surrounding whitespace, and trailing punctuation.
2. Match complete comment against fixed phrases: `/pi implement`, `implement`, `yes`, `yes implement`, `agreed`, `do that`, `ok`, `okay`, `proceed`, `go ahead`, `please implement`, `ship it`.
3. Reject mixed or substring matches such as `ok, but change the plan first`.
4. Check commenter permission through GitHub API. Accept `write`, `maintain`, or `admin` only.
5. If issue has plan PR and no implementation run, queue implementation. Otherwise run normal review and explain state.

Model-based approval classification is excluded. It must not control code execution.

### Plan PR merge

Handle `pull_request.closed` where `merged == true` and PR maps to plan:

1. Atomically claim implementation slot for issue.
2. Queue implementation if none exists.
3. Do nothing if comment approval already claimed slot.

Approval before plan merge branches from plan PR head and uses current plan file. Later plan merge does not create duplicate implementation.

### Implementation

1. Recheck approving user's permission immediately before work. Merge-triggered runs use plan merger identity.
2. Verify plan PR changes only expected plan Markdown file.
3. Create `agent/issue-<number>` from merged default branch or unmerged plan head.
4. Run Pi with edit tools in isolated worktree.
5. Pi may edit and test but may not commit, push, or call GitHub.
6. Require changed files, unchanged git history, and configured test command success.
7. App commits, pushes, and opens one draft implementation PR.
8. Comment on issue with implementation PR link.

No automatic merge.

## Pi process contract

Launch without shell:

```text
pi --mode json --no-session
   --no-extensions --no-skills --no-prompt-templates --no-context-files
   --tools <profile>
   <prompt>
```

- Parse JSONL events and store bounded output on run row.
- Use final `message_end` as authoritative assistant output.
- Enforce timeout and kill process group on timeout/shutdown.
- Pass sanitized environment without GitHub PAT, webhook secret, or dashboard password.
- Delimit GitHub text as untrusted data in prompt.
- Run Pi under separate unprivileged `pi-runner` OS user. Grant write access only to current worktree.
- Create one exclusive worktree per run at `<worktree-root>/<run-id>` before launching Pi; pass that path as subprocess `cwd`.
- Never reuse or share a worktree between running agents. Read-only runs use detached worktrees; implementation runs use their own branch.
- Remove worktree after terminal run state. On startup, clean interrupted-run worktrees and run `git worktree prune`.
- Serialize only short shared-clone operations such as fetch, worktree creation, and cleanup.

Read-only runs cannot mutate repository. App validates git state anyway. Worktrees are disposable and Pi execution remains parallel.

## SQLite schema

### `deliveries`

- `id` — `X-GitHub-Delivery`, primary key
- `event`, `action`
- `payload_json`
- `disposition`
- `created_at`

### `issues`

- `number`, primary key
- `plan_run_id`, `plan_pr_number`, `plan_head_sha`, `plan_text`
- `implementation_run_id`, unique when present
- `implementation_pr_number`
- timestamps

### `runs`

- `id`, primary key
- `delivery_id`, `issue_number`
- `kind`: `plan`, `review`, or `implementation`
- `status`: `queued`, `running`, `publishing`, `succeeded`, `failed`, `interrupted`
- `attempt`
- `actor`, `actor_permission`
- `prompt_context`, `output`, `error`
- `branch`, `worktree_path`, `github_url`
- timestamps

Each of the `MAX_CONCURRENT_AGENTS` worker loops atomically claims one row from `runs WHERE status = 'queued' ORDER BY created_at`. Use `BEGIN IMMEDIATE` for run claims and implementation slots; SQLite serializes these short writes while Pi processes run concurrently.

On restart, mark active rows `interrupted`. Retry reuses same run row, reconciles existing comment/branch/PR first, then increments attempt. Never create duplicate publication.

## HTTP routes

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/webhooks/github` | Signed GitHub webhook receiver |
| `GET` | `/` | Recent deliveries and runs |
| `GET` | `/runs/{id}` | Status, output, errors, GitHub links |
| `POST` | `/runs/{id}/retry` | Retry failed/interrupted run |
| `GET` | `/healthz` | Process and SQLite health |

Dashboard uses Jinja templates and small CSS. Detail page refreshes every five seconds while run is active. Protect dashboard with HTTP Basic and constant-time credential checks. Retry form carries HMAC CSRF token derived from dashboard secret and run ID.

Webhook route is not Basic-authenticated; signature verification is mandatory before JSON parsing or database writes.

## GitHub configuration

Use dedicated machine account with fine-grained PAT restricted to configured repository:

- Metadata: read
- Contents: read/write
- Issues: read/write
- Pull requests: read/write

Do not grant Actions/Workflows permission in MVP. Refuse implementation changes under `.github/workflows/` until explicitly enabled later.

Configure webhook secret and events listed above. Store bot login separately because PAT comments can appear as `User`, not `Bot`.

## Minimum files

```text
pyproject.toml
setup.sh         # idempotent environment bootstrap
run.sh           # start one app process
tests.sh         # run unit tests, then integration tests
agent_pipeline/
  __init__.py
  main.py          # config loading, lifespan, routes, templates
  contracts.py     # normalized dataclasses and two Protocol boundaries
  db.py            # schema, transactions, queue operations
  github.py        # GitHub CodeHost adapter
  pi.py            # Pi AgentRunner adapter
  worker.py        # provider-neutral workflow orchestration
  worktrees.py     # local git worktree lifecycle and validation
  templates/
    dashboard.html
    run.html
  static/app.css
tests/
  unit/
    test_github.py
    test_pi.py
    test_worker.py
  integration/
    test_workflows.py
deploy/
  agent-pipeline.service
  Caddyfile
.env.example
README.md
```

No ORM, migration framework, service layer, repository classes, frontend build, runtime plugin system, or provider registry.

## Required scripts

All three scripts resolve repository root from their own location, use `set -euo pipefail`, and are committed executable.

### `setup.sh`

Idempotent bootstrap for development and app execution:

1. Check required host commands: `python3`, `git`, and `npm`.
2. Create `.venv` when absent.
3. Install project plus test dependencies from `pyproject.toml`.
4. Install project-local Pi under `.tools/` when `pi` is unavailable.
5. Copy `.env.example` to `.env` only when `.env` does not exist.
6. Create ignored runtime directories for SQLite, clone, and worktrees.
7. Print missing secret values and exact next command; never overwrite configuration or secrets.

System users, Caddy, and systemd remain explicit VPS deployment steps because silently changing host services from a development bootstrap is unsafe.

### `run.sh`

1. Fail with instruction to run `setup.sh` when `.venv` or `.env` is missing.
2. Add project-local Pi binary to `PATH` when present.
3. Load trusted `.env` and execute one Uvicorn process on `127.0.0.1:${PORT:-8000}`.
4. Use `exec` so signals reach Uvicorn and lifespan shuts down agent processes cleanly.

### `tests.sh`

1. Fail with instruction to run `setup.sh` when test environment is missing.
2. Run `tests/unit/` first.
3. Run `tests/integration/` second only when unit tests pass.
4. Return nonzero on either failure.

Worker tests inject fake `CodeHost` and `AgentRunner` implementations. Adapter integration tests use temporary SQLite databases, real git repositories/worktrees, FastAPI test client, fake GitHub HTTP endpoints, and fake Pi executable. They require no GitHub token, model credentials, or network access.

## Build sequence

### 1. Reproducible scripts and environment

- Add `pyproject.toml`, `.env.example`, `.gitignore`, and three required scripts.
- Verify clean-machine setup, idempotent second setup, app startup, and both test phases.

**Done:** `./setup.sh && ./tests.sh` passes, and `./run.sh` starts healthy app.

### 2. Secure webhook and durable queue

- Add normalized contracts, settings validation, including positive integer `MAX_CONCURRENT_AGENTS`.
- Add SQLite schema, FastAPI lifespan, and `/healthz`.
- Implement webhook verification and normalization inside GitHub adapter using `hmac.compare_digest`.
- Deduplicate delivery IDs and queue normalized runs.
- Test valid/invalid signatures, redelivery, ignored machine comments, and invalid concurrency values.

**Done:** GitHub receives `202` quickly; duplicate delivery cannot duplicate run.

### 3. Pi adapter

- Implement `AgentRunner` with sanitized Pi subprocess and read-only/edit profiles.
- Parse JSONL, capture final response, timeout cleanly.
- Persist run status/output/errors.
- Start configured number of worker loops.
- Create exclusive `<worktree-root>/<run-id>` before each Pi launch and remove it afterward.
- Test protocol behavior, parser, nonzero exit, malformed line, timeout, restart cleanup, peak concurrency, and unique subprocess working directories.

**Done:** with `MAX_CONCURRENT_AGENTS=N`, up to N fake prompts run concurrently in N distinct worktrees and run N+1 stays queued.

### 4. Plan-only PR flow

- Add GitHub client and paginated context fetch.
- Add bare clone/worktree management.
- Generate plan, validate exact one-file change, publish PR/comment.
- Reconcile existing branch/PR on retry.

**Done:** opening test issue produces only `plans/issues/<number>.md` PR.

### 5. Comment review flow

- Route supported comments to read-only Pi run.
- Post one marked reply and prevent self-trigger loops.
- Map comments on plan/implementation PRs back to issue.

**Done:** each human comment receives one reply; app reply receives none.

### 6. Authorized implementation flow

- Add deterministic approval matcher and collaborator permission check.
- Claim implementation slot transactionally for merge/comment race.
- Run Pi in edit worktree, execute configured tests, publish draft PR.
- Block unchanged output, agent-created commits, workflow edits, and duplicate PRs.

**Done:** authorized command or plan merge creates exactly one implementation PR.

### 7. Dashboard and VPS deployment

- Add dashboard/detail/retry routes and templates.
- Add Basic auth, CSRF, Caddy TLS, systemd units/users, file permissions.
- Bind one Uvicorn process to `127.0.0.1`; configure agent concurrency independently.
- Smoke test webhook, dashboard auth, restart recovery, parallel runs, and GitHub links.

**Done:** service survives restart without losing queued work or exposing secrets.

## Acceptance scenarios

1. New issue creates one plan PR containing one Markdown file and no source changes.
2. Duplicate webhook returns success but creates no second run or PR.
3. Human comment gets one Pi reply; machine account reply does not loop.
4. `yes` from read-only user cannot start implementation.
5. `yes` from write user starts implementation when plan exists.
6. Plan merge and approval arriving together still create one implementation run.
7. Pi timeout/failure is visible and retry does not duplicate GitHub output.
8. Implementation agent cannot read GitHub token and app rejects agent-created commits.
9. Dashboard requires credentials; webhook rejects bad signature.
10. With `MAX_CONCURRENT_AGENTS=3`, three runs may be active in three distinct worktrees and fourth remains queued.
11. One agent cannot see or modify another active agent's uncommitted changes.
12. `setup.sh` is idempotent, `run.sh` propagates shutdown signals, and `tests.sh` runs unit then integration suites.
13. Integration tests run without live GitHub or model credentials.
14. Workflow tests pass with fake `CodeHost` and `AgentRunner`; worker has no GitHub payload or Pi JSON parsing code.
15. App never merges pull requests.

## Deferred upgrades

- Multiple repositories: replace singleton settings and add GitHub App installation tokens.
- Multiple VPS instances or Uvicorn processes: move queue to PostgreSQL/Redis only when one process with N agent workers is insufficient.
- Iterative code updates after first implementation PR: add revision runs only when needed.
- Live logs: add server-sent events only when five-second refresh is inadequate.
- Stronger sandbox: containerize Pi when untrusted public contributors or repositories are allowed.

## References

- Pi JSON event mode: local `docs/json.md` in `@earendil-works/pi-coding-agent`
- GitHub webhook validation: <https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries>
- GitHub webhook payloads: <https://docs.github.com/webhooks/webhook-events-and-payloads>
- GitHub collaborator permissions: <https://docs.github.com/en/rest/collaborators/collaborators>
- FastAPI lifespan: <https://fastapi.tiangolo.com/advanced/events/>
- FastAPI HTTP Basic: <https://fastapi.tiangolo.com/advanced/security/http-basic-auth/>
