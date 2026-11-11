# self-service-platform

Agentic-AI-Powered-Cloud-Infrastructure-Platform

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3120/)
[![CI](https://github.com/Asfandi-AI/Agentic-AI-Powered-Cloud-Infrastructure-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/Asfandi-AI/Agentic-AI-Powered-Cloud-Infrastructure-Platform/actions/workflows/ci.yml)
[![uv](https://img.shields.io/badge/uv-package%20manager-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)

## What this is

A developer describes a tenant as a YAML manifest of catalog components. The platform validates that manifest, estimates cost, and creates, updates, and destroys the tenant in a local simulation. The same operations are exposed as 12 agent tools, through a Bedrock adapter for `ssp chat`, and through the `ssp-mcp` MCP server. Confirmation stays outside the model.

This project is strictly tested with **816 tests**, `mypy` (including strict mode), and zero external cloud dependencies during local runs.

[Architecture defense](docs/ARCHITECTURE_DEFENSE.md) · [Architecture decisions](docs/adr/)

## CI

Push and pull requests to `main` run ruff, mypy, and pytest on Ubuntu and Windows.

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/).

The commands below are the same on Windows PowerShell and bash.

```text
git clone https://github.com/Asfandi-AI/Agentic-AI-Powered-Cloud-Infrastructure-Platform.git
cd Agentic-AI-Powered-Cloud-Infrastructure-Platform
uv sync
```

`SSP_HOME` selects the directory that holds `tenants.sqlite` and `local-executor.sqlite`. It must be an absolute path. When it is unset, that directory is `~/.ssp`.

## Example

`create`, `status`, and `destroy` are one path. `--yes` confirms. Without it, create asks for confirmation and destroy asks you to type the tenant id.

```text
uv run ssp create demo examples/valid-manifest.yaml --type dev --owner alice --yes
```

`create` prints the plan, then provisions the tenant with `LocalExecutor` and reports:

```text
created: demo status active
status: active
tenant_type: dev
owner: alice
uploads s3-bucket
jobs sqs-queue
```

```text
uv run ssp status demo
```

```text
status: active
tenant_type: dev
owner: alice
uploads s3-bucket
jobs sqs-queue
```

`status` also prints the initiator, catalog version, expiry, and last error.

```text
uv run ssp destroy demo --yes
```

`destroy` prints the plan first, naming each component and stating that all resources and data of the tenant will be deleted, then reports:

```text
destroyed: demo status destroyed
status: destroyed
```

A rejected manifest is refused before any of that work. `validate` on `examples/invalid-manifest.yaml` prints the issues on stderr and exits 1. Nothing is provisioned and nothing is written to the registry.

```text
uv run ssp validate examples/invalid-manifest.yaml
```

```text
position 1 uploads: unknown component type no-such-type
position 2 jobs: unknown option foo
```

## Commands

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
- chat, which uses Bedrock when `SSP_BEDROCK_MODEL_ID` is set

`tenants` and `status` read `tenants.sqlite`. `create`, `update`, and `destroy` write that file and `local-executor.sqlite`. `sweep --yes` writes them when it destroys a tenant. A dry run does not.

`LocalExecutor` is a persistent local simulation stored in `local-executor.sqlite` under the state directory. The in-memory fake forgets everything between processes. `create`, `update`, `destroy`, and `sweep --yes` use that simulation for provisioning. They call AWS only for the optional adapters below.

## Sweeper

`ssp sweep` tears down expired dev and ephemeral tenants through the same path as `ssp destroy`. Scheduling is left to you: cron, or Task Scheduler.

Without `--yes` the command is a dry run and changes nothing. `--yes` destroys the expired tenants. `--max N` limits how many successful destroys one run performs. The default is 10.

## Optional AWS adapters

Provisioning stays on `LocalExecutor`. Bedrock and CloudWatch Logs are wired, and both are opt-in. Unset or blank values leave the local path in place. Credentials come from the normal AWS SDK chain. This repository does not store them.

`ssp chat` uses Amazon Bedrock when `SSP_BEDROCK_MODEL_ID` is set. `SSP_BEDROCK_REGION` selects the region. When that variable is unset, the region is `us-east-1`. An invalid region is refused before the client is created, and `ssp chat` exits 1 with `invalid AWS region`. When `SSP_BEDROCK_MODEL_ID` is unset, `ssp chat` exits 2 and prints `no model client is configured`. The model id still has to be verified in the target account before a live call.

Successful `create`, `update`, `destroy`, and `sweep --yes` also write the flushed audit batch to CloudWatch Logs when both `SSP_AWS_REGION` and `SSP_LOG_GROUP` are set. The region must be an AWS region code such as `eu-west-1`. The log group name must be 1 to 512 characters and may contain only `A-Z`, `a-z`, `0-9`, and `.`, `-`, `_`, `/`, `#`. An invalid region or log group name is refused before the client is created. The CLI then exits 2 and prints `cannot write the audit log`. That same text is what the operator sees if CloudWatch later rejects the write. The AWS error stays on the chained exception and is not printed. The JSON-lines file is written in that same flush. A CloudWatch failure after the file write still exits 2 with `cannot write the audit log`. Plan, decline, refusal, and failed-attempt lines stay in the process until a successful execute or destroy.

The Execution_Policy and the Permissions_Boundary exist as policy documents built by `execution_policy` and `permissions_boundary` in `core/policy.py`. Structural tests check those documents. They are not verified against IAM, and nothing applies them.

## Limitations

Provisioning uses `LocalExecutor`, a local simulation. A cloud executor is future work. Starter component prices are unset, so an estimate reports each of them as not priced. Real prices are future work.

## Development

```text
uv run ruff check .
uv run mypy src
uv run pytest
uv audit --locked
uv build
uv run ssp --version
```

CI runs ruff, mypy and pytest on Ubuntu and Windows, audits the locked dependencies, and builds and smoke-tests the package. `uv build` writes a wheel and a source distribution in `dist/` and does not publish them. `uv run ssp --version` prints the installed package version and exits.

## Architecture decisions

Accepted decisions are in [docs/adr/](docs/adr/).

## Open questions

See [docs/open-questions.md](docs/open-questions.md).
