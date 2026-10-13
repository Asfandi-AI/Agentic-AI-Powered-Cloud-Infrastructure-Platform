# Open questions

These gaps are unresolved in the spec. Nothing here adds a status, a transition, or other behaviour.

## Failed create

Requirement 3.4 records a tenant and then provisions it. The only defined exit from `creating` is `creating` → `active`, when the create succeeds (Requirement 3.5). The spec defines no status and no transition for a failed create. A tenant whose create provision fails stays in `creating`.

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

## Decided

Error text is truncated to 1024 characters (`ERROR_TEXT_LIMIT`).
