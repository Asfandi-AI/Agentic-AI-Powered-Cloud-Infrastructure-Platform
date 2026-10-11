# Requirements Document

## Introduction

This spec extends the completed Self-Service Tenant Provisioning POC (`.kiro/specs/self-service-tenant-provisioning/`). Today the Tenant_Stack is fixed: one S3 bucket, one Lambda function, its log group and one IAM role. Developers building a new service need other resources in their Tenant, so the stack has to grow without the platform team changing code for every request.

The extension has two phases:

1. **Component catalog.** A set of vetted building blocks over a fixed allowlist of commonly used AWS services. A Tenant declares the components it needs in a Manifest. The Platform validates the Manifest, estimates cost, provisions the components, and keeps every existing guarantee (verified zero-orphans teardown, tagging, TTL, idempotence, scoped identity).
2. **Agent.** A natural-language front door on Amazon Bedrock that maps requests such as "billing service needs a table and a queue" onto catalog components and drives the same Core operations as the CLI, with confirmation enforced in code.

POC scope is a single Tenant_Stack type. The catalog will change often during the POC; adding or changing a Component must be a small change that the test suite guards.

### Out of Scope

- Separate stack types (production). The design must not block them.
- Shared (cross-tenant) resources.
- Slack, ServiceNow and web front doors for the Agent.
- Services outside the Allowlist.
- The production control plane (API → Step Functions → CodeBuild).
- A Terraform implementation of Components (the Executor_Interface must still allow it).

## Glossary

Terms from the base spec (Platform, CLI, Core, Executor, Executor_Interface, Registry, Sweeper, Tenant, Tenant_Id, Tenant_Type, Tenant_Stack, TTL, Expires_At, Estimated_Cost, Cost_Tag, Orphaned_Resource, Provisioning_Role, Execution_Policy) keep their meaning. New terms:

- **Allowlist**: The fixed set of AWS services a Component may use: S3, DynamoDB, SQS, SNS, Lambda, EventBridge, API Gateway (HTTP API) and CloudWatch Logs.
- **Catalog**: The versioned collection of Component definitions available to Tenants.
- **Catalog_Version**: An identifier for the Catalog contents, recorded with each applied Manifest.
- **Component**: A Catalog entry bundling an IaC construct, the permissions CloudFormation needs for it, its cost line items, required tags, deletion behaviour and an options schema.
- **Component_Type**: The Catalog key of a Component, e.g. `dynamodb-table`.
- **Manifest**: A YAML document listing the Components a Tenant needs as `{type, name, options}` entries.
- **Default_Manifest**: The Manifest applied when none is given; it reproduces today's Tenant_Stack (bucket, function, log group).
- **Manifest_Diff**: The added, removed and changed Components between a Tenant's applied Manifest and a proposed one.
- **Permissions_Boundary**: The managed policy every IAM role in a Tenant_Stack must carry, capping what that role can ever do.
- **Agent**: The natural-language front door that calls Core through a fixed set of Tools.
- **Tool**: One operation the Agent may invoke. Tools map one-to-one onto Core operations.
- **Model_Client**: The interface the Agent uses to call the language model, implemented for Amazon Bedrock and faked in tests.
- **Initiator**: Who started an operation: `cli` or `agent`, plus the owner identity.
- **Audit_Log**: The record of Agent Tool calls, confirmations and outcomes.

## Requirements

### Requirement 1: Component Catalog

**User Story:** As a platform engineer, I want a catalog of vetted components over allowed services, so that developers can add resources without me writing code for each request.

#### Acceptance Criteria

1. THE Platform SHALL provide a Catalog in which each Component defines its Component_Type, IaC construct, CloudFormation permissions, cost line items, required tags, deletion behaviour and options schema.
2. THE Catalog SHALL contain only Components whose resources belong to services in the Allowlist.
3. THE Catalog SHALL expose a Catalog_Version that changes whenever any Component definition changes.
4. THE Catalog SHALL include at least one Component for each service in the Allowlist.
5. WHEN a Component is added to the Catalog, THE test suite SHALL fail unless that Component satisfies the tagging, deletion-policy, cost and permissions rules in Requirements 5, 6 and 7.
6. THE CLI SHALL provide a `catalog` command that lists every Component_Type with its description, options and cost line items.

### Requirement 2: Manifest Validation

**User Story:** As a developer, I want my manifest checked before anything happens, so that mistakes never leave partial infrastructure.

#### Acceptance Criteria

1. THE Platform SHALL accept a Manifest as a YAML list of entries, each with a `type`, a `name` and optional `options`.
2. IF a Manifest entry has a `type` that is not in the Catalog, THEN THE Platform SHALL reject the Manifest and name the unknown type.
3. IF a Manifest entry has a `name` that is empty, duplicated within the Manifest, or would produce a resource name that violates AWS naming limits for that Tenant_Id, THEN THE Platform SHALL reject the Manifest.
4. IF a Manifest entry has `options` that do not satisfy the Component's options schema, THEN THE Platform SHALL reject the Manifest and name the invalid option.
5. IF a Manifest lists more than 40 Components, THEN THE Platform SHALL reject the Manifest.
6. WHEN the Platform rejects a Manifest, THE Platform SHALL make no Executor call and no Registry write.
7. THE Platform SHALL validate every Manifest before computing an Estimated_Cost or presenting a plan.

### Requirement 3: Create a Tenant from a Manifest

**User Story:** As a developer, I want to create a tenant with the components my service needs, so that I get the right environment in one step.

#### Acceptance Criteria

1. THE CLI `create` command SHALL accept an optional `--manifest` path in addition to its existing arguments.
2. WHERE no Manifest is given, THE Platform SHALL apply the Default_Manifest.
3. WHEN a user invokes `create` with a valid Manifest, THE Platform SHALL present the Components to be created, the Estimated_Cost and, for `dev` and `ephemeral` Tenants, the Expires_At before requesting confirmation.
4. WHEN a create with a Manifest is confirmed, THE Platform SHALL provision every Component through the Executor_Interface and SHALL follow the existing record-then-provision-then-record flow.
5. WHEN a create with a Manifest succeeds, THE Registry SHALL store the applied Manifest, the Catalog_Version and the Initiator for the Tenant.

### Requirement 4: Update an Existing Tenant

**User Story:** As a developer, I want to add or remove components on my existing tenant, so that my environment grows with my service.

#### Acceptance Criteria

1. THE CLI SHALL provide an `update` command accepting a Tenant_Id and a Manifest path.
2. WHEN a user invokes `update`, THE Platform SHALL present the Manifest_Diff and the new Estimated_Cost and SHALL require explicit confirmation before any change.
3. WHERE the Manifest_Diff removes one or more Components, THE Platform SHALL warn that their resources and data will be deleted and SHALL require a separate explicit confirmation of the removal.
4. IF the Manifest_Diff is empty, THEN THE Platform SHALL report that there is nothing to change and SHALL make no Executor call.
5. WHEN an update is confirmed, THE Platform SHALL move the Tenant from `active` to an in-flight `updating` status with a conditional write, apply the Manifest through a convergent provision of the same deterministic stack, and then record `active` with the new Manifest and Catalog_Version.
6. IF the update provision fails, THEN THE Platform SHALL record `update_failed` with the truncated error, and THE Platform SHALL allow `update` or `destroy` to be invoked again from that status.
7. IF the Tenant is in flight or not `active` or `update_failed`, THEN THE Platform SHALL reject the update without side effects.
8. WHEN an update removes a Component, THE Platform SHALL verify that no resource tagged with the Tenant_Id and that Component's name remains before recording success.
9. WHERE a changed Component option forces its resources to be replaced, THE Platform SHALL treat the change as a removal followed by a recreation, SHALL state explicitly in the plan that the Component will be deleted and recreated and its data lost, and SHALL require a separate explicit confirmation for each such recreation in addition to the update confirmation.
10. THE Catalog SHALL declare, for each Component option, whether changing it forces replacement, so that the Platform can detect recreation before deploying.

### Requirement 5: Cost Estimation per Component

**User Story:** As a developer, I want to see what my components will cost, so that I can choose with the cost in front of me.

#### Acceptance Criteria

1. THE Platform SHALL compute the Estimated_Cost of a Manifest as the sum of the cost line items of its Components.
2. THE Platform SHALL present the Estimated_Cost broken down per Component.
3. WHERE a Component or a resource in the synthesized Tenant_Stack has no cost line item, THE Platform SHALL present it as "not priced" and SHALL NOT count it as zero cost.
4. THE `estimate` command SHALL accept a Manifest path and SHALL return its Estimated_Cost.
5. THE Platform SHALL label every Estimated_Cost as a POC heuristic.

### Requirement 6: Tagging and Deletion Guarantees for Components

**User Story:** As a platform operator, I want every component resource to be tagged and deletable, so that cost attribution and zero-orphans teardown keep working as the catalog grows.

#### Acceptance Criteria

1. THE Platform SHALL apply the `tenant_id`, `tenant_type`, `owner` and `managed_by` tags, and a `component` tag naming the Manifest entry, to every taggable resource a Component creates.
2. THE Platform SHALL give every resource in a Tenant_Stack a deletion policy that deletes it when the stack is deleted.
3. IF any resource in a synthesized Tenant_Stack lacks the required tags or has a retain or snapshot deletion policy, THEN the test suite SHALL fail.
4. WHERE a Component creates a resource type that cannot be tagged, THE Component SHALL declare that resource type explicitly, and THE test suite SHALL fail for any untagged resource that is not declared.
5. WHEN a Tenant built from any Manifest is destroyed, THE Platform SHALL apply the existing teardown verification and SHALL report success only when zero tagged resources and no tenant IAM role remain.
6. WHEN tearing down a Tenant_Stack, THE Executor SHALL empty every S3 bucket in the stack before deleting it.

### Requirement 7: Service-Level Execution Policy with a Permissions Boundary

**User Story:** As a security owner, I want new components to deploy without per-resource IAM changes while privilege escalation stays impossible, so that the catalog can grow safely.

#### Acceptance Criteria

1. THE Execution_Policy SHALL grant CloudFormation permissions only on services in the Allowlist plus the IAM, CloudFormation and SSM actions the Tenant_Stack needs.
2. THE Execution_Policy SHALL deny all actions on services outside the Allowlist, except `kms:Decrypt` when it is called through S3 (`kms:ViaService` = `s3.<region>.amazonaws.com`), which CloudFormation needs to read CDK assets from the SSE-KMS bootstrap assets bucket.
3. THE Execution_Policy SHALL allow creating an IAM role only when the request attaches the Permissions_Boundary.
4. THE Execution_Policy SHALL deny deleting or changing the Permissions_Boundary of any role, and SHALL deny changing the Permissions_Boundary policy itself.
5. THE Execution_Policy SHALL deny all IAM changes to the Provisioning_Role, the identity deployer role, the CDK bootstrap roles and the Execution_Policy.
6. THE Permissions_Boundary SHALL limit tenant roles to actions on services in the Allowlist and SHALL deny all IAM actions.
7. THE Execution_Policy SHALL restrict `iam:PassRole` to roles carrying the Permissions_Boundary and to services in the Allowlist.
8. WHEN the Execution_Policy is in place, THE `role/*-role` scope noted as a known gap in the base spec SHALL no longer be relied on for safety.
9. THE test suite SHALL verify criteria 1 through 7 against the synthesized identity stack.

### Requirement 8: Backward Compatibility and Existing Guarantees

**User Story:** As a platform operator, I want existing tenants and commands to keep working, so that the extension does not break what the POC proved.

#### Acceptance Criteria

1. WHEN an existing Registry record has no stored Manifest, THE Platform SHALL treat that Tenant as having the Default_Manifest.
2. THE existing `create`, `list`, `status`, `destroy` and `estimate` commands SHALL keep their current arguments and behaviour when no Manifest is given.
3. THE Sweeper SHALL tear down expired Tenants built from any Manifest through the same path as `destroy`, and SHALL never select a `prod` Tenant.
4. THE Core and CLI SHALL NOT import CDK; Components SHALL reach the Executor only as data (the validated Manifest).
5. THE Executor_Interface SHALL accept the Manifest as a parameter, so that an IaC tool other than CDK can implement Components later.
6. THE `status` command SHALL show the applied Manifest, the Catalog_Version and the Initiator.

### Requirement 9: Agent Model Access

**User Story:** As a platform engineer, I want the agent's model behind an interface, so that it runs on Bedrock in use and is fully testable without it.

#### Acceptance Criteria

1. THE Agent SHALL call the language model only through the Model_Client interface.
2. THE Platform SHALL implement the Model_Client for Amazon Bedrock using Claude Sonnet 5 with tool use in `us-east-1` by default, with the model id and region configurable.
3. THE test suite SHALL exercise the Agent with a fake Model_Client and SHALL NOT call Bedrock.
4. THE Provisioning_Role SHALL be granted `bedrock:InvokeModel` only on the configured model, and no other new permission, other than the Audit_Log write required by Requirement 13.5, for the Agent to run.

### Requirement 10: Agent Tools and Scope

**User Story:** As a security owner, I want the agent limited to the same operations as the CLI, so that it can never do more than a developer could.

#### Acceptance Criteria

1. THE Agent SHALL offer exactly these Tools: list catalog, draft manifest, validate manifest, estimate, plan create, plan update, execute create, execute update, list tenants, tenant status and destroy.
2. WHEN the Agent invokes a Tool, THE Tool SHALL call the corresponding Core operation and SHALL make no other AWS call.
3. THE Agent SHALL produce only Manifests over Components in the Catalog.
4. IF a user request cannot be met with Catalog Components, THEN THE Agent SHALL state which part cannot be met and SHALL NOT create resources outside the Catalog.
5. WHEN the Agent drafts a Manifest, THE Platform SHALL validate it with the same validation as the CLI before showing a plan.
6. THE Agent SHALL run under the same scoped identity as the CLI and SHALL have no permission the CLI does not have, other than the model invocation in Requirement 9.4.

### Requirement 11: Confirmation Enforced in Code

**User Story:** As a developer, I want the agent to never change infrastructure without my explicit approval, so that a model mistake cannot provision or delete anything on its own.

#### Acceptance Criteria

1. WHEN the Agent plans a create, update or destroy, THE Platform SHALL present the plan to the user, including the Manifest_Diff where applicable, the Estimated_Cost and the Expires_At where applicable.
2. THE execute create, execute update and destroy Tools SHALL require a confirmation token that is issued only when the user explicitly confirms that exact plan outside the model's output.
3. IF an execute or destroy Tool is invoked without a valid confirmation token for the exact plan, THEN THE Platform SHALL refuse the operation and SHALL make no Executor call and no Registry write.
4. WHERE the plan removes Components or destroys a Tenant, THE Platform SHALL require the user to confirm the data loss explicitly.
5. A confirmation token SHALL be single use and SHALL expire after a short period.

### Requirement 12: Agent Interfaces

**User Story:** As a developer, I want to use the agent from my terminal or from my IDE, so that I can provision where I already work.

#### Acceptance Criteria

1. THE CLI SHALL provide a `chat` command that runs an interactive Agent session in the terminal, with confirmation prompts handled by the CLI.
2. THE Platform SHALL provide an MCP server exposing the same Tools as the Agent, with the same validation and confirmation rules.
3. WHEN a Tool is called through the MCP server, THE Platform SHALL enforce the confirmation token rule in Requirement 11 regardless of the MCP client.

### Requirement 13: Audit and Attribution

**User Story:** As a security owner, I want every agent action recorded, so that I can see who asked for what and what happened.

#### Acceptance Criteria

1. WHEN the Agent invokes a Tool, THE Platform SHALL write an Audit_Log entry with the time, owner identity, Tool name, Tenant_Id where applicable, arguments summary and outcome.
2. WHEN the user confirms or declines a plan, THE Platform SHALL write an Audit_Log entry recording the decision.
3. THE Audit_Log SHALL NOT contain credentials, session tokens or model API keys.
4. THE Registry SHALL record the Initiator for each create, update and destroy.
5. THE Platform SHALL write each Audit_Log entry both to a local JSON-lines file and to a CloudWatch Logs log group.

## Resolved Decisions

1. **Per-Tenant Component limit:** 40 (Requirement 2.5).
2. **Model:** Claude Sonnet 5 on Amazon Bedrock in `us-east-1`, configurable (Requirement 9.2). The exact Bedrock model id must be verified before use.
3. **Audit_Log storage:** a local JSON-lines file plus a CloudWatch Logs log group (Requirement 13.5).
4. **Replacement-forcing option changes:** treated as removal plus recreation, called out explicitly, with a separate confirmation for each recreation (Requirements 4.9, 4.10).
5. **Starter Components:** `s3-bucket`, `dynamodb-table`, `sqs-queue`, `sns-topic`, `lambda-function`, `eventbridge-rule`, `http-api`, `log-group`. AppSync, RDS, KMS, EKS and ACM are deferred.
6. **Slack front door:** later; out of scope for this spec.
7. **`kms:Decrypt` via S3:** approved exception to the service deny (Requirement 7.2).
8. **Audit_Log write on the Provisioning_Role:** approved exception to Requirement 9.4.
