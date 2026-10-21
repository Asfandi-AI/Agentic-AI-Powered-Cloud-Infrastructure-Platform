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

## Status

Built and usable from the CLI:

- catalog
- validate
- estimate
- Manifest diff
- tenants
- status

`tenants` and `status` read `tenants.sqlite`. `SSP_HOME` must be an absolute path and selects the directory that holds that file. When `SSP_HOME` is unset, the directory is `~/.ssp`.

Still library-only, because a persistent executor is missing and the in-memory fake forgets everything between processes:

- create
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
