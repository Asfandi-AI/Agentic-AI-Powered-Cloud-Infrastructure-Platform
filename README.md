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
dynamodb-table dynamodb A DynamoDB table
eventbridge-rule events An EventBridge rule
http-api apigateway An HTTP API
lambda-function lambda A Lambda function
log-group logs A log group
s3-bucket s3 An S3 bucket
sns-topic sns An SNS topic
sqs-queue sqs An SQS queue
catalog_version: 6323c6d8dc8233b6ead14a88cd40667a3d0b90d6af74d6e1f56c4c547a0c175f
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

`create`, `update`, and `destroy` store the tenant under `SSP_HOME` and record resources with `LocalExecutor`. That executor is a local simulation. It is not AWS. `SSP_HOME` must be an absolute directory. The transcript below was captured with `SSP_HOME` set to a temporary directory outside this project. That directory was deleted afterwards.

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
expires_at: 2026-10-24T09:09:57.758725+00:00
created: demo status active
status: active
tenant_type: dev
owner: alice
initiator: cli demo
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-24T09:09:57.758725+00:00
last_error: -
uploads s3-bucket
jobs sqs-queue
```

```text
uv run ssp create demo --type dev --owner alice --yes
```

```text
tenant_id: demo
tenant_type: dev
owner: alice
bucket s3-bucket
  S3 storage not priced
  total: not priced
function lambda-function
  Lambda requests not priced
  total: not priced
logs log-group
  Log storage not priced
  total: not priced
manifest total: not priced
POC heuristic
expires_at: 2026-11-05T10:42:50.787545+00:00
created: demo status active
status: active
tenant_type: dev
owner: alice
initiator: cli demo
catalog_version: f09c038f8d5f67596e1ce70903ec402743ff79ec8d1d23a1905e2ad64ba1f8f5
expires_at: 2026-11-05T10:42:50.787545+00:00
last_error: -
bucket s3-bucket
function lambda-function
logs log-group
```

```text
uv run ssp update demo examples/added-manifest.yaml --yes
```

```text
tenant_id: demo
status: active
added: reports sqs-queue
summary: 1 added, 0 removed, 0 changed
uploads s3-bucket
  S3 storage not priced
  total: not priced
jobs sqs-queue
  SQS requests not priced
  total: not priced
reports sqs-queue
  SQS requests not priced
  total: not priced
manifest total: not priced
POC heuristic
removals: -
replacements: -
updated: demo status active
status: active
tenant_type: dev
owner: alice
initiator: cli demo
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-24T09:09:57.758725+00:00
last_error: -
uploads s3-bucket
jobs sqs-queue
reports sqs-queue
```

```text
uv run ssp update demo examples/updated-manifest.yaml --yes --confirm-removals
```

```text
tenant_id: demo
status: active
removed: jobs sqs-queue (data in this component will be deleted)
summary: 0 added, 1 removed, 0 changed
uploads s3-bucket
  S3 storage not priced
  total: not priced
reports sqs-queue
  SQS requests not priced
  total: not priced
manifest total: not priced
POC heuristic
removals: jobs
replacements: -
updated: demo status active
status: active
tenant_type: dev
owner: alice
initiator: cli demo
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-24T09:09:57.758725+00:00
last_error: -
uploads s3-bucket
reports sqs-queue
```

```text
uv run ssp status demo
```

```text
status: active
tenant_type: dev
owner: alice
initiator: cli demo
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-24T09:09:57.758725+00:00
last_error: -
uploads s3-bucket
reports sqs-queue
```

```text
uv run ssp destroy demo --yes
```

```text
tenant_id: demo
tenant_type: dev
status: active
uploads
reports
all resources and data of the tenant will be deleted
destroyed: demo status destroyed
status: destroyed
tenant_type: dev
owner: alice
initiator: cli demo
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-24T09:09:57.758725+00:00
last_error: -
uploads s3-bucket
reports sqs-queue
```

```text
uv run ssp tenants
```

```text
demo destroyed dev alice 2026-10-24T09:09:57.758725+00:00
```

```text
uv run ssp status demo
```

```text
status: destroyed
tenant_type: dev
owner: alice
initiator: cli demo
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2026-10-24T09:09:57.758725+00:00
last_error: -
uploads s3-bucket
reports sqs-queue
```

## Sweeper

`ssp sweep` tears down expired dev and ephemeral tenants through the same path as `ssp destroy`. It runs against the local simulation. It is not AWS. Scheduling is left to you: cron, or Task Scheduler.

Without `--yes` the command is a dry run and changes nothing. `--yes` destroys the expired tenants. `--max N` limits how many successful destroys one run performs. The default is 10.

The transcript below was captured with `$env:USERNAME = "demo"`, and with `USER` and `LOGNAME` set to `demo` when those variables were set. `SSP_HOME` was a temporary directory outside this project, and that directory was deleted afterwards. The tenant was created as ephemeral, then its `expires_at` was set to `2000-01-01T00:00:00+00:00` so the capture did not wait for the TTL.

```text
uv run ssp sweep
```

```text
skipped: demo dry run
summary: 0 destroyed, 0 failed, 1 skipped
```

```text
uv run ssp sweep --yes
```

```text
destroyed: demo
summary: 1 destroyed, 0 failed, 0 skipped
```

```text
uv run ssp status demo
```

```text
status: destroyed
tenant_type: ephemeral
owner: alice
initiator: sweeper sweeper
catalog_version: 38925d059c7b9c8a677bd163d1d05f61bf45784ab02d21f46304f1fa0f6219b6
expires_at: 2000-01-01T00:00:00+00:00
last_error: -
uploads s3-bucket
jobs sqs-queue
```

```text
uv run ssp sweep
```

```text
nothing to sweep
```

## Status

Built and usable from the CLI:

- catalog, which lists `s3-bucket`, `sqs-queue`, `lambda-function`, `log-group`, `dynamodb-table`, `sns-topic`, `eventbridge-rule`, and `http-api`
- validate
- estimate
- Manifest diff
- tenants, with `list` as an alias
- status
- create, which calls `default_manifest()` when no manifest path is given (`bucket` of type `s3-bucket`, `function` of type `lambda-function`, `logs` of type `log-group`)
- update
- destroy
- sweep

`tenants` and `status` read `tenants.sqlite`. `create`, `update`, and `destroy` write that file and `local-executor.sqlite`. `sweep --yes` writes them when it destroys a tenant. A dry run does not. `SSP_HOME` must be an absolute path and selects the directory that holds those files. When `SSP_HOME` is unset, the directory is `~/.ssp`.

`LocalExecutor` is a persistent local simulation stored in `local-executor.sqlite` under the state directory. It is not AWS. The in-memory fake forgets everything between processes. `create`, `update`, `destroy`, and `sweep --yes` use that simulation. They do not call AWS.

The Execution_Policy and the Permissions_Boundary exist as policy documents built by `execution_policy` and `permissions_boundary` in `core/policy.py`. Structural tests check those documents. They are not verified against IAM, and nothing applies them.

Not built yet:

- A Terraform executor or an AWS executor
- An agent: the Model_Client interface and the fake exist in `core/`, and there is no Bedrock client
- Confirmation tokens: `create`, `update`, `destroy`, and `sweep` use the issuer and the verifier in `core/`. An agent does not
- A `chat` command
- An MCP server
- An audit log: the JSON-lines sink is used by those commands, and there is no real CloudWatch sink

## Development

```text
uv run ruff check .
uv run mypy src
uv run pytest
uv audit --locked
uv build
ssp --version
```

CI runs ruff, mypy and pytest on Ubuntu and Windows, audits the locked dependencies, and builds and smoke-tests the package. `uv build` writes a wheel and a source distribution in `dist/` and does not publish them. `ssp --version` prints the installed package version and exits.

## Open questions

See [docs/open-questions.md](docs/open-questions.md).
