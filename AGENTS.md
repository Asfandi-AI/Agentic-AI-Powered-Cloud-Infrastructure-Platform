# Self-Service Platform - rules for AI assistants

Project spec: docs/requirements.md. Read it before any task.

## Architecture rules (never break)
- `core/` and `cli/` must NOT import boto3, CDK, Terraform or any cloud SDK.
- Components reach the Executor only as data (the validated Manifest).
- Executors live in `executors/` behind the Executor_Interface.
- Manifest validation always runs before any Executor call or Registry write.
- A rejected Manifest causes no Executor call and no Registry write.
- Cost items with no price are "not priced", never zero.

## Working style
- Python 3.12, type hints everywhere, Pydantic for schemas.
- Write the pytest tests first, then the implementation.
- One requirement or one small task at a time. Do not build ahead.
- Keep functions small. No unused code, no speculative features.
- Run `ruff`, `mypy` and `pytest` before saying a task is done.
- Never commit secrets, keys or .env files.

## Layout
src/self_service_platform/{core,catalog,executors,cli}/ and tests/
