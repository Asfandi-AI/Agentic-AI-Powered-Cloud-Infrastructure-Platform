"""Policy documents for the Execution_Policy and the Permissions_Boundary.

The CLI and the executors do not call these builders.
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from self_service_platform.catalog.component import AwsService

_POLICY_VERSION = "2012-10-17"
_BOUNDARY_ROLE_ACTIONS = frozenset(
    {
        "iam:AttachRolePolicy",
        "iam:DeleteRole",
        "iam:DeleteRolePolicy",
        "iam:DetachRolePolicy",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:UpdateAssumeRolePolicy",
    }
)
_ROLE_READS = frozenset(
    {
        "iam:GetRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
    }
)
_CREATE_ROLE_ACTIONS = frozenset({"iam:CreateRole", "iam:TagRole"})
_CLOUDFORMATION_ACTIONS = frozenset(
    {
        "cloudformation:CreateChangeSet",
        "cloudformation:CreateStack",
        "cloudformation:DeleteChangeSet",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeChangeSet",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResources",
        "cloudformation:DescribeStacks",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:GetTemplate",
        "cloudformation:GetTemplateSummary",
        "cloudformation:ListStacks",
        "cloudformation:UpdateStack",
    }
)
_SSM_ACTIONS = frozenset({"ssm:GetParameter", "ssm:GetParameters"})
_ROLE_BOUNDARY_DENIES = frozenset(
    {
        "iam:DeleteRolePermissionsBoundary",
        "iam:PutRolePermissionsBoundary",
    }
)
_BOUNDARY_POLICY_DENIES = frozenset(
    {
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion",
    }
)


class PolicyParameters(BaseModel):
    model_config = ConfigDict(frozen=True)

    partition: str
    account_id: str
    region: str
    boundary_name: str
    execution_policy_name: str
    provisioning_role_name: str
    deployer_role_name: str
    bootstrap_role_names: tuple[str, ...]


def permissions_boundary(parameters: PolicyParameters) -> dict[str, Any]:
    """Return the service-level boundary. It does not read parameters."""
    del parameters
    return _document(
        [
            _statement("Allow", _service_wildcards(), frozenset({"*"})),
            _statement("Deny", frozenset({"iam:*"}), frozenset({"*"})),
        ]
    )


def execution_policy(parameters: PolicyParameters) -> dict[str, Any]:
    role_resources = frozenset({_role_arn(parameters, "*")})
    boundary = _boundary_condition(parameters)
    via_service = f"s3.{parameters.region}.amazonaws.com"
    allows = [
        _statement("Allow", _service_wildcards(), frozenset({"*"})),
        _statement(
            "Allow",
            _BOUNDARY_ROLE_ACTIONS,
            role_resources,
            condition=boundary,
        ),
        _statement("Allow", _ROLE_READS, role_resources),
        _statement(
            "Allow",
            _CREATE_ROLE_ACTIONS,
            role_resources,
            condition=boundary,
        ),
        _statement("Allow", _CLOUDFORMATION_ACTIONS, frozenset({"*"})),
        _statement("Allow", _SSM_ACTIONS, frozenset({"*"})),
        _statement(
            "Allow",
            frozenset({"kms:Decrypt"}),
            frozenset({"*"}),
            condition={"StringEquals": {"kms:ViaService": via_service}},
        ),
    ]
    allowed = frozenset(
        action for statement in allows for action in statement["Action"]
    )
    protected = frozenset(
        {
            _role_arn(parameters, parameters.provisioning_role_name),
            _role_arn(parameters, parameters.deployer_role_name),
            *(_role_arn(parameters, name) for name in parameters.bootstrap_role_names),
            _policy_arn(parameters, parameters.execution_policy_name),
        }
    )
    return _document(
        [
            *allows,
            _statement("Deny", allowed, frozenset({"*"}), negated=True),
            _statement(
                "Deny",
                frozenset({"kms:Decrypt"}),
                frozenset({"*"}),
                condition={"StringNotEquals": {"kms:ViaService": via_service}},
            ),
            _statement("Deny", _ROLE_BOUNDARY_DENIES, frozenset({"*"})),
            _statement(
                "Deny",
                _BOUNDARY_POLICY_DENIES,
                frozenset({_policy_arn(parameters, parameters.boundary_name)}),
            ),
            _statement("Deny", frozenset({"iam:*"}), protected),
        ]
    )


def _service_wildcards() -> frozenset[str]:
    return frozenset(f"{service.value}:*" for service in AwsService)


def _role_arn(parameters: PolicyParameters, name: str) -> str:
    return f"arn:{parameters.partition}:iam::{parameters.account_id}:role/{name}"


def _policy_arn(parameters: PolicyParameters, name: str) -> str:
    return f"arn:{parameters.partition}:iam::{parameters.account_id}:policy/{name}"


def _boundary_condition(parameters: PolicyParameters) -> dict[str, dict[str, str]]:
    arn = _policy_arn(parameters, parameters.boundary_name)
    return {"StringEquals": {"iam:PermissionsBoundary": arn}}


def _statement(
    effect: str,
    actions: frozenset[str],
    resources: frozenset[str],
    *,
    condition: dict[str, dict[str, str]] | None = None,
    negated: bool = False,
) -> dict[str, Any]:
    ordered_resources = sorted(resources)
    resource: str | list[str] = (
        ordered_resources[0] if len(ordered_resources) == 1 else ordered_resources
    )
    statement: dict[str, Any] = {
        "Effect": effect,
        "NotAction" if negated else "Action": sorted(actions),
        "Resource": resource,
    }
    if condition is not None:
        statement["Condition"] = condition
    return statement


def _document(statements: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        statements,
        key=lambda statement: json.dumps(statement, sort_keys=True),
    )
    return {"Version": _POLICY_VERSION, "Statement": ordered}
