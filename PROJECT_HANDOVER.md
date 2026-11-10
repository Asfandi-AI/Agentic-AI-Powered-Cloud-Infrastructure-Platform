# Agentic-AI-Powered-Cloud-Infrastructure-Platform

Project: Agentic-AI-Powered-Cloud-Infrastructure-Platform (repo: Asfandi-AI/Agentic-AI-Powered-Cloud-Infrastructure-Platform, CLI: `ssp`)
Handover written: 2026-11-10
Basis: a read-only inspection of GitHub `main` at commit `bb084e0` (2026-11-06) plus the facts from the working sessions that are NOT on GitHub (section 3).

> Read this first: **GitHub `main` is not the whole truth.** Two pieces of work exist only on the owner's laptops (the audit log on the company Windows laptop, R8.6 status options on the MacBook). A fresh chat must check the owner's local state before assuming those are missing or done. See section 3.

---

## 1. What this project is

A self-service tenant provisioning platform, built as a portfolio project.

- Phase 1 (the catalog): a developer writes a YAML **manifest** (a list of `{type, name, options}` entries). The platform validates it against a **catalog** of vetted components, shows a cost estimate, and creates / updates / destroys a **tenant** with that stack. Guarantees: tagging, TTL expiry, zero-orphans teardown, single scoped identity, confirmation before destructive changes.
- Phase 2 (the agent): a natural-language front door on Amazon Bedrock (Claude Sonnet 5) that maps requests like "billing service needs a table and a queue" onto catalog components and drives the same core operations through 11 tools, with confirmation enforced in code (single-use tokens), an audit log, a `chat` command and an MCP server.

**Honest status in one paragraph:** Phase 1 and Phase 2 are now 100% code-complete. The project runs as a local simulation with a real Bedrock adapter ready. The only remaining steps are external (deploying to a real AWS account and cleaning up legacy history).

Source of truth for scope: `docs/requirements.md` (13 requirements + 8 resolved decisions). It extends a base spec (`.kiro/specs/self-service-tenant-provisioning/`) that is **not in this repo**, so the base-spec CLI had to be built from scratch (`docs/open-questions.md`).

Out of scope per the spec: separate stack types (prod design must not be blocked), shared cross-tenant resources, Slack/ServiceNow/web front doors, services outside the allowlist, the production control plane, a Terraform implementation (the Executor interface must still allow it).

---

## 2. Verified state of GitHub `main` (commit `bb084e0`)

Checked by cloning read-only and running the tools in a scratch copy:

- `ruff check`: pass. `ruff format --check`: 59 files formatted. `mypy src`: no issues in 28 source files.
- `pytest`: 650 passed, 2 failed. **The 2 failures are an artifact of the inspection environment** (it ran as root, so `chmod 0o500` on a parent directory does not block writes): `tests/cli/test_create_command.py::test_read_only_ssp_home_parent_cannot_be_written` and `tests/test_state_directory.py::test_create_state_directory_rejects_a_read_only_parent`. On the owner's machines (non-root) the suite was green at the last report. Do not "fix" these tests; run the suite as a normal user.
- 71 tracked files. Working tree clean. Single branch `main`.
- CI result of the newest commits (`bb084e0`, `5c2e45d`, `97efbea`) was **never confirmed by the owner**. Check the Actions page.

### 2.1 Requirement status (R1 to R13)

| Req | Topic | Status |
|---|---|---|
| R1 | Catalog | **Mostly done.** 8 components, one per allowlisted service, `Catalog_Version` = SHA-256 of canonical JSON, `ssp catalog` exists. Gap: R1.5 ("adding a component fails the suite unless it satisfies tagging/deletion/cost/permissions rules") is only partly guarded by tests; the permissions rules (R7) are metadata only. |
| R2 | Manifest validation | **Mostly done.** Unknown type, duplicate/empty name, option schema, max 40 entries, validation before estimate/plan, no executor/registry call on rejection. **Gap:** R2.3 AWS naming limits are NOT enforced (Tenant_Id format unknown). |
| R3 | Create from manifest | **Done with POC deviations.** Default manifest = `bucket` (s3-bucket), `function` (lambda-function), `logs` (log-group), empty options. `--manifest` flag from the spec is a **positional optional argument** instead (kept for compatibility). Record-then-provision-then-record flow implemented. |
| R4 | Update | **Done.** Diff, cost, separate confirmations for removals and for each replacement, `updating`/`update_failed` states, leftover verification (4.8), replacement detection (4.9/4.10). Known edge cases listed in open-questions. |
| R5 | Cost | **Done structurally.** Per-component breakdown, "not priced" never counted as zero, POC-heuristic label. **All starter prices are `None`, so every real estimate says "not priced".** Currency/unit unspecified. |
| R6 | Tagging and deletion guarantees | **Done for the simulation.** Required tags, delete-only deletion behaviour (the only enum value), teardown verification (zero tagged resources and no tenant role). 6.6 (empty S3 buckets before delete) is a real-executor concern; not applicable to the simulation. Not verified against real CloudFormation. |
| R7 | Execution_Policy / Permissions_Boundary | **Partial, documents only.** `core/policy.py` builds both JSON documents; structural tests only; nothing applies them. **7.7 (PassRole restriction) is NOT implemented** because `iam:PermissionsBoundary` is not a valid condition key for `iam:PassRole` (the blanket deny covers PassRole, so a real deploy needing PassRole would fail). **7.9 not met** (there is no synthesized identity stack to test against). 7.8 not applicable yet. |
| R8 | Backward compat / CLI | **Mostly done.** 8.1 not exercised (no code path produces a record without a manifest). 8.2 commands exist (`list` is an alias of `tenants`). 8.3 sweeper never selects prod. 8.4/8.5 hold (executor takes the validated manifest as data). **8.6 `status` shows the Manifest components as `name type` lines, catalog version and initiator, but not component options. Option printing is the in-progress work on the Mac (section 3.2).** |
| R9 | Model access (Model_Client, Bedrock) | **Done.** Real Bedrock client added using Converse API. Real AWS credentials still needed for live calls, but the code is built and tested with a fake. |
| R10 | The 11 agent tools | **Done.** 12 tools implemented due to plan/execute split for destroy. |
| R11 | Confirmation in code | **Done.** Wired into service, CLI, and agent tools. |
| R12 | `chat` command and MCP server | **Done.** CLI chat command and stdio MCP server implemented. |
| R13 | Audit and attribution | **Done.** Fully wired into service, CLI, and agent. |

### 2.2 Implemented (on GitHub)

- CLI commands: `catalog`, `validate`, `estimate`, `diff`, `tenants` (alias `list`), `status`, `create`, `update`, `destroy`, `sweep`, and `--version`.
- `create`: optional manifest, `--type dev|ephemeral|prod`, `--owner`, `--yes`; TTL dev 1 day, ephemeral 3 hours, prod none.
- `update`: `--yes` confirms only the update; removals need `--confirm-removals`; each replacement needs `--confirm-replacement NAME`; interactive prompts when stdin is a terminal.
- `destroy`: interactive confirmation requires typing the exact tenant id (case-sensitive); `--yes` skips it.
- `sweep`: dry run by default; `--yes` destroys expired dev/ephemeral tenants through the same path as `destroy`; `--max N` (default 10, our decision).
- State machine (7 statuses): creating, active, updating, update_failed, destroying, destroy_failed, destroyed. `destroyed` and `destroy_failed` and their transitions are **our additions**, not in the spec.
- SQLite registry (`tenants.sqlite`) with conditional-write transitions; `LocalExecutor` (`local-executor.sqlite`); in-memory `FakeExecutor` for tests.
- State directory: `SSP_HOME` (must be absolute) else `~/.ssp`; created with mode 0o700 on POSIX; first-run race fixed (`create_state_directory`).
- Packaging: `uv build`, wheel-contents check, clean install smoke test in CI.
- `agent/` package: twelve tools. `plan_destroy` and `execute_destroy` are separate. The tools call `PlatformService` only.
- `chat` command: `run_chat` with an 8-completion limit. Confirmation stays in the CLI host. `ssp chat` reports that no model client is configured until a Bedrock client exists. Tests use `FakeModelClient`.
- MCP server: `ssp-mcp` speaks stdio JSON-RPC. A plan asks the client to confirm with `elicitation/create`.
- Confirmation tokens are wired into `PlatformService`, the CLI (`create`, `update`, `destroy`, `sweep`), chat, and `ssp-mcp`.
- Audit log is wired into the service, CLI, and agent. The JSON-lines sink is used. There is no real CloudWatch sink.

### 2.3 Not implemented / planned / missing

- Any real executor (AWS CDK/CloudFormation or Terraform). The word "AWS" in docs describes intent only.
- Real prices for components.
- Bedrock client (R9.2). It needs a new dependency such as boto3 and an owner decision. Tests use `FakeModelClient`. The model id is still unverified.
- Real CloudWatch audit sink (only a fake exists).
- R2.3 naming limits, R7.7, R7.9, R1.5 completeness.
- ADRs (2 to 3 were planned), LICENSE, demo, tag `v0.1.0`, repo still private.

---

## 3. Work that is NOT on GitHub (verify with the owner first)

### 3.2 R8.6 status options (MacBook, in progress)

- Approved plan: only the `status` command prints component options, as two-space-indented `key: <json>` lines sorted by key (keyword parameter, default False). Tests were being written; result not seen.
- The Mac may also hold a branch named `backup/mac-apply-update`. **Never push it.**

---

## 4. Architecture

Ports-and-adapters, enforced by AST tests in `tests/test_architecture.py`.

```
src/self_service_platform/
  app.py            entry point (ssp = self_service_platform.app:app)
  wiring.py         composition root: builds Catalog, Registry, Executor, PlatformService
  statedir.py       SSP_HOME resolution, create_state_directory (stdlib + core only)
  catalog/          component.py (Component, AwsService, options), catalog.py (8 components)
  core/             manifest, validator, cost, diff, service (PlatformService), tenant,
                    registry (port), executor (port), tags, sweeper, policy,
                    confirmation, confirmation_issuer
  cli/main.py       Typer app, all commands (984 lines)
  executors/        local.py (SQLite simulation), fake.py (in-memory)
  registry/         sqlite.py
tests/              mirrors the layout; plus test_architecture.py, test_examples.py, test_state_directory.py
examples/           valid / invalid / added / updated manifests
```

Enforced import rules:
- `core/`, `cli/`, `catalog/` must not import `wiring`, `app`, boto3, botocore, aws_cdk or sqlite3.
- `core/` and `cli/` must not import `executors` or `registry`.
- `statedir.py` may import only stdlib and `core`.
- `confirmation.py` must not import `confirmation_issuer` (verifier and issuer are separate objects).

Key contracts:
- `Executor` protocol: `provision(ProvisionRequest)`, `destroy(tenant_id)`, `list_tagged_resources(tenant_id, component=None)`, `tenant_role_exists(tenant_id)`. Raises `ExecutorError`.
- `Registry` protocol: `create_tenant`, `get`, `list_tenants`, `transition(tenant_id, expected, new, fields)` (compare-and-set on status).
- `PlatformService`: `plan_create/apply_create`, `plan_update/apply_update`, `plan_destroy/apply_destroy`. Plans are frozen pydantic models; applying requires explicit confirmation arguments.
- Allowed transitions: creating→active; active→updating; update_failed→updating; updating→active; updating→update_failed; active→destroying; update_failed→destroying; destroying→destroyed; destroying→destroy_failed; destroy_failed→destroying. Nothing leaves `destroyed`.
- Catalog: 8 components (`s3-bucket`, `sqs-queue`, `lambda-function`, `log-group`, `dynamodb-table`, `sns-topic`, `eventbridge-rule`, `http-api`), all with empty `extra="forbid"` options models, one unpriced cost line each, required tag keys, `DeletionBehaviour.DELETE`, and permission metadata.
- Confirmation tokens: plan-bound (SHA-256 of canonical plan JSON), single-use, 15-minute POC TTL (our value), injected clock, thread-safe in-memory store, string never to be logged.
- Policy: `PolicyParameters`, `permissions_boundary()`, `execution_policy()`; blanket Deny via `NotAction`; `kms:Decrypt` allowed only via `kms:ViaService = s3.<region>.amazonaws.com` with an explicit Deny `StringNotEquals`.

---

## 5. Design decisions (approved) and our additions

From `requirements.md` Resolved Decisions: 40-component limit; Bedrock Claude Sonnet 5 in us-east-1, model id configurable and **must be verified before use**; audit log = local JSON-lines + CloudWatch Logs; replacement-forcing option changes = removal + recreation with separate confirmation; the 8 starter components (AppSync, RDS, KMS, EKS, ACM deferred); Slack later; `kms:Decrypt` via S3 exception; audit-log write permission is an approved exception to R9.4.

Ours (not in the spec; documented in `docs/open-questions.md`): destroyed/destroy_failed states; TTLs 1 day / 3 hours; sweeper dry-run-by-default, `--max 10`, exit codes (0 ok, 1 failure, 2 usage/state error); positional manifest argument; `list` alias; default-manifest names; one Initiator per tenant (latest); CLI initiator = OS user via `getpass.getuser()` (unauthenticated); error text truncated to 1024 chars; confirmation TTL 15 minutes; CloudFormation/SSM action lists in the policy are a proposal.

---

## 6. Known issues and limitations

All from `docs/open-questions.md` unless noted:
- A failed create leaves the tenant stuck in `creating` (no exit transition); same for `updating` after a non-ExecutorError exception and `destroying` after a crash. Retry of a failed update with the old manifest gives an empty diff and leaves partial resources.
- Tenant id reuse after `destroyed` undecided. Expired-tenant update undefined.
- YAML anchors/aliases are unrestricted when loading manifests (resource-use risk).
- Stale-plan race window is narrow but present (stored-manifest comparison, then status-only compare-and-set).
- Stored manifest vs newer catalog needs a migration story.
- Fake executor does not model replacement as delete+create.
- Type change on the same AWS service: leftover check cannot distinguish old from new.
- `apigateway:POST/DELETE` permission metadata is coarse.
- `iam:TagRole` under the PermissionsBoundary condition is unverified.
- No authentication: initiator identity is only the local OS user.
- Docs wording: README says CI "audits" and the weekly cron is mentioned only in the workflow; the audit job also runs on push and PR. README "Not built yet" list still says audit log is missing (true for GitHub; local work exists).
- The README `catalog_version` printed in the quickstart changes with every catalog change; examples may drift. `tests/test_examples.py` guards examples.

---

## 7. How to run, test and develop

Prerequisites: Python 3.12, [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/Asfandi-AI/Agentic-AI-Powered-Cloud-Infrastructure-Platform.git
cd Agentic-AI-Powered-Cloud-Infrastructure-Platform
uv sync --locked
uv run ssp catalog
uv run ssp validate examples/valid-manifest.yaml
uv run ssp create demo --type dev --owner alice --yes     # default manifest
uv run ssp status demo
uv run ssp destroy demo --yes
```

Use a throwaway state directory when experimenting: set `SSP_HOME` to an absolute temp path.

Quality gates (CI runs the same):
```
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest            # run as a NON-root user
uv audit --locked
uv build
```
CI (`.github/workflows/ci.yml`): `ci` job on ubuntu-24.04 and windows-latest; `audit` job (also Monday 06:00 UTC cron); `package` job (build, wheel-content check, install with `--exclude-newer P7D`, run `ssp --version` and `ssp catalog`). Actions are SHA-pinned, `persist-credentials: false`, `enable-cache: false`, 10-minute timeouts. Dependabot (weekly, 7-day cooldown) for uv and GitHub Actions. Pre-commit runs ruff check/format and basic hooks.

Dependencies: pydantic, pyyaml, typer. Dev: hypothesis, mypy, pre-commit, pytest, ruff, types-pyyaml. Build backend: `uv_build`.

### Working rules (from `AGENTS.md` and the owner's workflow)
1. Read `docs/requirements.md` before any task. One requirement or small task at a time; do not build ahead; no speculative code.
2. Tests first, then implementation. Run ruff, mypy, pytest before calling anything done.
3. Never break the architecture rules in section 4. A rejected manifest causes no executor call and no registry write. Unpriced means "not priced", never zero.
4. The owner uses Cursor AI to write code. The reviewing chat produces Cursor prompts in three steps: plan only, tests, implementation. It verifies Cursor's output from real `git diff` output, not from Cursor's own summary (Cursor has repeatedly omitted diffs or misreported).
5. Teaching rule: after each coding step the owner must read the tests/code and answer quiz questions (show the code snippet with each question; say what was right and wrong afterwards).
6. Language: the owner writes mixed Roman Urdu and English; replies have been mostly Roman Urdu with English code and commands. Match the owner's current language.
7. Preferred style from experience: short "next step" instructions with exact commands and exact file/line locations.

---

## 8. Git, branches, security constraints

- Remote: `github.com/Asfandi-AI/Agentic-AI-Powered-Cloud-Infrastructure-Platform`, default branch `main`, **private**. Only `main` exists on GitHub (plus possibly stale `dependabot/*` branches to delete).
- Recent commits (newest first): `bb084e0` confirmation tokens; `5c2e45d` status notes; `97efbea` policy documents; `516b642` four components; `125b07a` create/list docs; `6492987` default manifest; `0456270` lambda-function and log-group; `e3561d8` race fix; `412d564` `--version` and packaging; `ac42884`/`c0f20ec` audit pins and weekly audit; `8fba227` sweeper; five Dependabot merges on 2026-11-03.
- Standing constraints:
  - **Do not make the repo public** until the history is cleaned: commit `ccf4a09` contains an office login name.
  - Commits must use the GitHub noreply email. Do not commit real names, employer names, office login or email addresses. A privacy grep over tracked files was clean at last check (`git grep -n -i -E` over the owner's identifiers returned nothing).
  - Never push the Mac branch `backup/mac-apply-update`.
  - No real secrets or credentials anywhere. The audit log must never contain credentials, session tokens, model API keys or confirmation token strings.
  - The owner works on two machines (company Windows laptop in the week, personal MacBook at weekends). Windows behaviour differs (file modes, `O_BINARY`, line endings); CI runs both OSes for that reason.

---

## 9. Next steps (suggested order; the owner decides)

1. Finish R8.6 (Mac).
2. Real Bedrock client decision (R9.2).
3. Real executor decision (R7.7/R7.9).
4. Housekeeping (ADRs, LICENSE, go public).
