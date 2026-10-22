# Self-Service Platform

A manifest is a YAML list of catalog components. The platform validates that list against the catalog and prints a per-component cost estimate labeled as a POC heuristic. Starter component prices are unset, so a real estimate reports each of them as not priced.

## CI

[![CI](https://github.com/Asfandi-AI/Self-Service-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/Asfandi-AI/Self-Service-Platform/actions/workflows/ci.yml)

Push and pull requests to `main` run ruff, mypy, and pytest on Ubuntu and Windows.

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/).

The commands below are the same on Windows PowerShell and bash.

```text
git clone https://github.com/Asfandi-AI/Self-Service-Platform.git
cd Self-Service-Platform
uv sync
```

```text
uv run ssp catalog
```

```text
s3-bucket s3 An S3 bucket
sqs-queue sqs An SQS queue
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
```

The catalog_version value is a hash that changes whenever a component definition changes.

```text
uv run ssp validate examples/valid-manifest.yaml
```

```text
OK: 2 entries
```

```text
uv run ssp estimate examples/valid-manifest.yaml
```

```text
uploads s3-bucket
  S3 storage not priced
  total: not priced
jobs sqs-queue
  SQS requests not priced
  total: not priced
manifest total: not priced
POC heuristic
```

```text
uv run ssp diff examples/valid-manifest.yaml examples/updated-manifest.yaml
```

```text
added: reports sqs-queue
removed: jobs sqs-queue (data in this component will be deleted)
summary: 1 added, 1 removed, 0 changed
```

```text
uv run ssp validate examples/invalid-manifest.yaml
```

```text
position 1 uploads: unknown component type no-such-type
position 2 jobs: unknown option foo
```

```text
uv run ssp estimate examples/invalid-manifest.yaml
```

```text
position 1 uploads: unknown component type no-such-type
position 2 jobs: unknown option foo
```

The two commands on examples/invalid-manifest.yaml print those issues on stderr and exit with status 1.

## Try it

`create` stores the tenant under `SSP_HOME` and records resources with `LocalExecutor`. That executor is a local simulation. It is not AWS. `SSP_HOME` must be an absolute directory. The transcript below was captured with `SSP_HOME` set to a temporary directory outside this project. That directory was deleted afterwards. `update` and `destroy` are not available from the CLI yet.

```text
uv run ssp create demo examples/valid-manifest.yaml --type dev --owner alice --plan-only
```

```text
tenant_id: demo
tenant_type: dev
owner: alice
uploads s3-bucket
  S3 storage not priced
  total: not priced
jobs sqs-queue
  SQS requests not priced
  total: not priced
manifest total: not priced
POC heuristic
expires_at: 2026-10-22T09:59:37.957317+00:00
plan only: nothing was written
```

```text
uv run ssp create demo examples/valid-manifest.yaml --type dev --owner alice --yes
```

```text
tenant_id: demo
tenant_type: dev
owner: alice
uploads s3-bucket
  S3 storage not priced
  total: not priced
jobs sqs-queue
  SQS requests not priced
  total: not priced
manifest total: not priced
POC heuristic
expires_at: 2026-10-22T09:59:38.880765+00:00
created: demo status active
status: active
tenant_type: dev
owner: alice
initiator: cli REDACTED
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-22T09:59:38.880765+00:00
last_error: -
uploads s3-bucket
jobs sqs-queue
```

```text
uv run ssp tenants
```

```text
demo active dev alice 2026-10-22T09:59:38.880765+00:00
```

```text
uv run ssp status demo
```

```text
status: active
tenant_type: dev
owner: alice
initiator: cli REDACTED
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-22T09:59:38.880765+00:00
last_error: -
uploads s3-bucket
jobs sqs-queue
```

## Status

Built and usable from the CLI:

- catalog
- validate
- estimate
- Manifest diff
- tenants
- status
- create

`tenants` and `status` read `tenants.sqlite`. `create` writes that file and `local-executor.sqlite`. `SSP_HOME` must be an absolute path and selects the directory that holds those files. When `SSP_HOME` is unset, the directory is `~/.ssp`.

`LocalExecutor` is a persistent local simulation stored in `local-executor.sqlite` under the state directory. It is not AWS. The in-memory fake forgets everything between processes.

Still not reachable from the CLI:

- update
- destroy

Not built yet:

- A Terraform executor or an AWS executor
- A sweeper
- An agent
- An MCP server

## Development

```text
uv run ruff check .
uv run mypy src
uv run pytest
```

CI runs the same three.

## Open questions

See [docs/open-questions.md](docs/open-questions.md).
