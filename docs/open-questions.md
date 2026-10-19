# Open questions

These gaps are unresolved in the spec. Nothing here adds a status, a transition, or other behaviour.

## Failed create

Requirement 3.4 records a tenant and then provisions it. The only defined exit from `creating` is `creating` → `active`, when the create succeeds (Requirement 3.5). The spec defines no status and no transition for a failed create. A tenant whose create provision fails stays in `creating`, and the service does not call destroy. A tenant stuck in `creating` can be neither retried, because that tenant id already exists, nor destroyed, because there is no transition out of `creating`. If provision succeeds but the `creating` → `active` write fails, the resources exist while the record stays `creating`.

## Failed destroy

The defined destroy transitions are `active` → `destroying` and `update_failed` → `destroying`. The spec defines no `destroyed` status, no `destroy_failed` status, and no transition out of `destroying`.

## Initiator history

Requirement 13.4 says the Registry records the Initiator for each create, update, and destroy. A tenant record stores one Initiator, so only the latest is kept.

## Record without a stored Manifest

Requirement 8.1 says an existing Registry record with no stored Manifest is treated as the Default Manifest. That case is not handled yet.

## YAML anchors and aliases

YAML anchors and aliases are not restricted when loading a manifest. Their effect on memory and CPU is unknown, so this is a known risk to revisit.

## Starter component prices

The real prices of the starter components are `None`. Every real estimate says "not priced" until someone supplies POC heuristic numbers.

## Base-spec CLI

Requirement 8.2's CLI arguments are not in this repo, so `ssp` is built from scratch.

## Cost amount unit

The currency or unit of amounts is not specified.

## Diff confirmation

The CLI `diff` command only reports, so it does not ask for the confirmations in Requirements 4.3 and 4.9. `apply_update` requires those confirmations.

## Manifest required on create

This create path requires a Manifest, so the Default_Manifest in Requirement 3.2 is not applied.

## AWS resource names

The Tenant_Id format and the AWS naming limits are unknown, so Requirement 2.3 is not enforced for a name that would violate those limits.

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

## Decided

Error text is truncated to 1024 characters (`ERROR_TEXT_LIMIT`).
