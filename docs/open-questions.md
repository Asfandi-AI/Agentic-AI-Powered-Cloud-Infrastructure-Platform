# Open questions

These gaps are unresolved in the spec. Nothing here adds a status, a transition, or other behaviour.

## Failed create

Requirement 3.4 records a tenant and then provisions it. The only defined exit from `creating` is `creating` → `active`, when the create succeeds (Requirement 3.5). The spec defines no status and no transition for a failed create. A tenant whose create provision fails stays in `creating`, and the service does not call destroy. A tenant stuck in `creating` can be neither retried, because that tenant id already exists, nor destroyed, because there is no transition out of `creating`. If provision succeeds but the `creating` → `active` write fails, the resources exist while the record stays `creating`.

## Failed destroy

The defined destroy transitions are `active` → `destroying` and `update_failed` → `destroying`. The spec defines no `destroyed` status, no `destroy_failed` status, and no transition out of `destroying`.

## Decision (ours, not in the spec)

`destroyed` and `destroy_failed` are not in the spec. We added `destroying` → `destroyed`, `destroying` → `destroy_failed`, and `destroy_failed` → `destroying`. Nothing leaves `destroyed`.

Whether a tenant may move from `creating` to `destroying` is still open.

## Reusing a destroyed tenant id

Whether a `destroyed` tenant id may be created again is undecided.

## Destroy plan component names

The plan's `component_names` are those at plan time. A destroy removes everything regardless.

## Sweeper and destroyed tenants

The Sweeper skips `destroyed` tenants. Their `expires_at` stays stored for history.

## Decision (ours, not in the spec): Sweeper

The Sweeper passes `confirmed=True` because the TTL was shown and accepted at create time. `ssp sweep` defaults to a dry run. `--yes` performs the destroys. `--max` limits successful destroys per run. A failed or skipped tenant does not consume one of those slots. The default is 10. This limit is our decision, not in the spec.

Exit 0 when no entry failed. That includes a dry run that lists eligible tenants and a real sweep whose entries are all skipped. Exit 1 when at least one entry failed, and when the clock is not timezone-aware UTC. Exit 2 when `--max` is below 1, when the state cannot be read, or when the state cannot be written during a destroy.

An expired tenant stuck in `creating`, `updating`, or `destroying` is only reported. The Sweeper does not call the service for it.

One unexpected exception stops the run. The next run continues, because selection is by state.

A tenant that stays `destroy_failed` is retried on every sweep and makes every run exit 1. With the limit, the oldest failing tenants are retried first. There is no backoff and no retry limit.

Dev tenants still expire after 1 day and ephemeral tenants still expire after 3 hours. Those durations remain the POC defaults in the create TTL decision. Prod tenants do not expire, and the Sweeper never selects them.

## Initiator history

Requirement 13.4 says the Registry records the Initiator for each create, update, and destroy. A tenant record stores one Initiator, so only the latest is kept.

## Record without a stored Manifest

Requirement 8.1 says an existing Registry record with no stored Manifest is treated as the Default Manifest. `TenantRecord.manifest` is required. The tenants table defines `manifest_json` as `TEXT NOT NULL`. `apply_create` stores the plan's entries on the creating record and again when the tenant becomes active. This code does not write a tenant without a manifest. No code treats a missing manifest as the Default_Manifest, because this code does not produce such a record.

## YAML anchors and aliases

YAML anchors and aliases are not restricted when loading a manifest. Their effect on memory and CPU is unknown, so this is a known risk to revisit.

## Starter component prices

The real prices of the starter components are `None`. Every real estimate says "not priced" until someone supplies POC heuristic numbers.

## Base-spec CLI

Requirement 8.2's CLI arguments are not in this repo, so `ssp` is built from scratch.

## Cost amount unit

The currency or unit of amounts is not specified.

## Diff confirmation

The CLI `diff` command only reports, so it does not ask for the confirmations in Requirements 4.3 and 4.9. `ssp update` asks for them. `--yes` is only the update confirmation. Removals and replacements need their own flags, or their own prompts when stdin is a terminal.

## Default manifest on create

`ssp create` applies the Default_Manifest when no manifest path is given. It is three entries, `bucket` (`s3-bucket`), `function` (`lambda-function`) and `logs` (`log-group`), each with empty options. Those names are a POC choice.

Requirement 3.1 names an optional `--manifest` option. `ssp create` keeps the manifest as an optional positional argument so that `create TENANT_ID MANIFEST` keeps working. This is a POC choice. The spec also names a `list` command; `ssp list` is an alias of `ssp tenants`.

## Lambda function and log group

`lambda-function` and `log-group` have empty options and unpriced line items. In the Default_Manifest their names are `function` and `logs`, which is a POC choice.

`dynamodb-table`, `sns-topic`, `eventbridge-rule` and `http-api` have empty options and unpriced line items. The permission names for events (PutRule, DeleteRule) and apigateway (POST, DELETE) do not follow the Create/Delete pattern. `apigateway:POST` and `apigateway:DELETE` are coarse actions that apply to any POST or DELETE under the API Gateway resource, not only to API creation. Permissions are catalog metadata only until requirement R7 (Permissions_Boundary) is built.

## AWS resource names

The Tenant_Id format and the AWS naming limits are unknown, so Requirement 2.3 is not enforced for a name that would violate those limits. `ssp create` rejects only an empty or whitespace-only id, and any id that contains a control character. It does not define a Tenant_Id format.

## Owner column

The `owner` column was added to the tenant table with no migration because there is no deployed data.

## Type change during update

When a component keeps its name but changes type, the removal verification (Requirement 4.8) must not mistake the newly created resource for a leftover.

## Failed update record

Failing to record a failed update leaves the tenant in `updating`.

## Retry of a failed update

Retrying a failed update with the old manifest gives an empty diff and no Executor call, so partial resources stay.

## Stuck in updating

A tenant stuck in `updating` after an exception that is not an ExecutorError cannot move on.

## Stuck in destroying

A tenant left in `destroying` (crash, or a failed write after a successful destroy) cannot be retried because no transition leaves `destroying` except to `destroyed` or `destroy_failed`.

## Replacement in the fake executor

The in-memory fake does not model replacement as delete and recreate, so real executors need their own tests.

## Stored manifest and a newer catalog

A stored manifest that no longer validates against a newer catalog needs a migration story.

## Stale plan race

The stale-plan check compares the stored manifest with the plan, then the registry's conditional write compares only the status, so a very narrow race window remains.

## Type change on the same AWS service

For a type change between two components on the same AWS service, the leftover check cannot tell old resources from new ones.

## Expired tenant update

Whether a tenant whose `expires_at` has already passed may still be updated is not defined by the spec.

## State directory permissions

The create command's first write creates the state directory. It is user-only (`0o700`) on POSIX systems. On Windows the default under the user profile is assumed private.

Two first creates could both pass the existence check while the state directory was still missing, and the process that lost `mkdir` exited 2 with `cannot write state` even when the tenant ids differed. `create_state_directory` treats an existing directory as success and applies mode `0o700` only on the call that created it.

## Local executor test hooks

`fail_next_provision`, `leave_one_resource_on_next_destroy`, `put_bucket_object`, and `delete_component_resource` live on `LocalExecutor`, as they do on the in-memory fake. `fail_next_provision_atomically` is on `LocalExecutor` only. `wiring` and the CLI do not call them. `LocalExecutor` is a persistent local simulation, not AWS. `ssp update`, `ssp destroy`, and `ssp sweep` use that simulation. They do not call AWS.

Real cloud tools can fail halfway and leave partial resources, so `fail_next_provision` commits the resources it wrote and then raises, and the service must show them. LocalExecutor's own writes are atomic, so `fail_next_provision_atomically` raises inside the provision transaction and rolls it back, and an interrupted LocalExecutor never corrupts its state.

A rolled-back first provision leaves a zero-byte database file, not a file with a SQLite header and no tables. A zero-byte file reads as no resources and no role. A non-empty file that lacks the executor tables, or is not a SQLite database, raises `ExecutorError` on read.

## Decision (ours, not in the spec): create TTL

Dev tenants expire after 1 day. Ephemeral tenants expire after 3 hours. Prod tenants do not expire. These durations are POC defaults, not from the spec.

## CLI initiator

`ssp create`, `ssp update`, and `ssp destroy` record an Initiator with kind `cli` and owner set to the operating-system user from `getpass.getuser()`. There is no authentication, so this identity is only as trustworthy as the local user. `--owner` on create is the tenant owner, a different field.

`--yes` confirms the update itself and nothing else. Removing a component also needs `--confirm-removals`. Each replacement needs its own `--confirm-replacement NAME`. Those flags are not implied by `--yes`.

Interactive destroy accepts only the exact tenant id, case-sensitive, without stripping surrounding whitespace. A tenant id that contains a control character, or is not printable ASCII, cannot be confirmed by typing it. The command tells the user to pass `--yes` and does not read stdin.

If another process wins the registry's conditional write, `apply_update` and `apply_destroy` raise `TransitionRejectedError`. The CLI prints `tenant changed while the command was running; no change was made by this command` and exits 1. It does not catch any other exception from the executor.

## Execution policy and permissions boundary

The `kms:Decrypt` Deny uses `StringNotEquals` on `kms:ViaService`. The IAM condition operators page (https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_condition_operators.html) describes `StringNotEquals` as negated matching. It says that if the key in a policy condition is not present in the request context, the values do not match and the condition is false, and that if the policy condition requires that the key is not matched, such as `StringNotLike` or `ArnNotLike`, and the key is not present, the condition is true. That logic applies to all condition operators except `...IfExists` and the Null check. `StringNotEquals` is negated matching, so under that rule a missing `kms:ViaService` makes this condition true.

Requirement 7.7 is not implemented. The IAM service-authorization reference (https://docs.aws.amazon.com/service-authorization/latest/reference/list_iam.html) lists these condition keys for `PassRole`: `aws:ResourceTag/${TagKey}`, `iam:AssociatedResourceArn`, `iam:PassedToService`, and `iam:ResourceTag/${TagKey}`. `iam:PermissionsBoundary` is not among them. The Execution_Policy has no `iam:PassRole` statement, so the blanket Deny covers `iam:PassRole`. A real deploy that needs to pass a role would not work. The code does not include a replacement mechanism.

Requirement 7.9 is not met. It asks for tests against a synthesized identity stack. This repo has no synthesized identity stack. The tests check the JSON documents against these rules. They do not call IAM, and they do not show that AWS accepts the documents.

The CloudFormation action list, the SSM action list (`ssm:GetParameter` and `ssm:GetParameters`), and `Resource: "*"` for those actions are a proposal for this POC. They are not from the spec.

`iam:CreateRole` and `iam:TagRole` share one Allow that requires `iam:PermissionsBoundary`. The Actions table does not list that key for `TagRole`. Whether tag-on-create works under that condition is unverified.

`permissions_boundary` and `execution_policy` are not called by the CLI or any executor, so nothing enforces these documents.

## Confirmation tokens

`create`, `update`, `destroy`, and `sweep` obtain a single-use token and pass it to `apply_create`, `apply_update`, and `apply_destroy`. A call that omits the token still uses the boolean confirmation. The store is in memory and process-local. The 15-minute lifetime is a POC value, not from the spec. The digest does not include the Initiator. An Audit_Log entry must never contain the token string (requirement 13.3). An agent does not use these tokens.

## Audit log

`plan_create`, `plan_update`, `plan_destroy`, `execute_create`, `execute_update`, and `destroy` write the audit log. `list_catalog`, `draft_manifest`, `validate_manifest`, `estimate`, `list_tenants`, and `tenant_status` write one entry when they are called. There is still no real CloudWatch sink, so 13.5 is half done. The fake CloudWatch sink is not wired.

A failed audit write refuses to start execute or destroy. If the outcome write fails after the registry has already changed, the change is left in place and the error is raised. The CLI exits 2 with `cannot write the audit log`.

The JSON-lines file is appended when an execute or destroy succeeds, after the state directory exists. A plan, a decline, a refusal, or a failed attempt stays in the process and does not create or change `SSP_HOME`.

Every write calls `fsync`, which costs speed. On Windows the file has no user-only ACL guarantee and concurrent append atomicity is not verified, and the same holds for network file systems.

The allow-list stops secret argument keys, but an identity field that a caller fills with a secret would be stored. The audit file contains the operating system user name. Requirement 9.4 and decision 8 are not expressed in `core/policy.py`.

## Model client

The Model_Client protocol and `FakeModelClient` exist in `core/`. Twelve tools in `agent/` call `PlatformService`. The host confirms a plan and passes the token to `execute_create`, `execute_update`, or `execute_destroy`. `execute_destroy` is recorded as `destroy`. The tools do not call the model yet. There is no Bedrock client. The Bedrock model id is still unverified, so requirement 9.2 is not started.

## Decided

Error text is truncated to 1024 characters (`ERROR_TEXT_LIMIT`).
