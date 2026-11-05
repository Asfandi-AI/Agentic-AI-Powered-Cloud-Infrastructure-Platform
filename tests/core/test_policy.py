import json

from self_service_platform.catalog.component import AwsService
from self_service_platform.core.policy import (
    PolicyParameters,
    execution_policy,
    permissions_boundary,
)

_SERVICE_WILDCARDS = frozenset(f"{service.value}:*" for service in AwsService)
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
_UNCONDITIONAL_ROLE_READS = frozenset(
    {
        "iam:GetRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
    }
)
_CREATE_ROLE_ACTIONS = frozenset({"iam:CreateRole", "iam:TagRole"})
_IAM_ALLOWS = _BOUNDARY_ROLE_ACTIONS | _UNCONDITIONAL_ROLE_READS | _CREATE_ROLE_ACTIONS
_CLOUDFORMATION_ALLOWS = frozenset(
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
_SSM_ALLOWS = frozenset({"ssm:GetParameter", "ssm:GetParameters"})
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


def _parameters() -> PolicyParameters:
    return PolicyParameters(
        partition="aws",
        account_id="000000000000",
        region="region-test",
        boundary_name="boundary-test",
        execution_policy_name="execution-test",
        provisioning_role_name="provisioning-test",
        deployer_role_name="deployer-test",
        bootstrap_role_names=("bootstrap-a", "bootstrap-b"),
    )


def _role_arn(parameters: PolicyParameters, name: str) -> str:
    return f"arn:{parameters.partition}:iam::{parameters.account_id}:role/{name}"


def _policy_arn(parameters: PolicyParameters, name: str) -> str:
    return f"arn:{parameters.partition}:iam::{parameters.account_id}:policy/{name}"


def _statements(document: dict[str, object]) -> list[dict[str, object]]:
    statement = document["Statement"]
    if isinstance(statement, dict):
        return [statement]
    if isinstance(statement, list):
        return [item for item in statement if isinstance(item, dict)]
    raise AssertionError("Statement must be one statement or a list of statements")


def _string_list(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    raise AssertionError("expected a string or a list of strings")


def _statement_with_actions(
    document: dict[str, object],
    effect: str,
    actions: set[str],
) -> dict[str, object]:
    matches = [
        statement
        for statement in _statements(document)
        if statement.get("Effect") == effect
        and "Action" in statement
        and _string_list(statement["Action"]) == actions
    ]
    assert len(matches) == 1
    return matches[0]


def _boundary_condition(parameters: PolicyParameters) -> dict[str, dict[str, str]]:
    return {
        "StringEquals": {
            "iam:PermissionsBoundary": _policy_arn(parameters, parameters.boundary_name)
        }
    }


def test_permissions_boundary_allows_each_allowlist_service_wildcard() -> None:
    statement = _statement_with_actions(
        permissions_boundary(_parameters()),
        "Allow",
        set(_SERVICE_WILDCARDS),
    )

    assert _string_list(statement["Resource"]) == {"*"}


def test_permissions_boundary_denies_every_iam_action() -> None:
    statement = _statement_with_actions(
        permissions_boundary(_parameters()),
        "Deny",
        {"iam:*"},
    )

    assert _string_list(statement["Resource"]) == {"*"}


def test_execution_policy_allows_the_same_allowlist_service_wildcards() -> None:
    statement = _statement_with_actions(
        execution_policy(_parameters()),
        "Allow",
        set(_SERVICE_WILDCARDS),
    )

    assert _string_list(statement["Resource"]) == {"*"}


def test_only_the_listed_iam_cloudformation_and_ssm_actions_are_allowed() -> None:
    found = {
        action
        for statement in _statements(execution_policy(_parameters()))
        if statement.get("Effect") == "Allow" and "Action" in statement
        for action in _string_list(statement["Action"])
        if action.startswith(("iam:", "cloudformation:", "ssm:"))
    }

    assert found == _IAM_ALLOWS | _CLOUDFORMATION_ALLOWS | _SSM_ALLOWS


def test_execution_policy_denies_an_action_outside_the_allowlist() -> None:
    document = execution_policy(_parameters())
    for statement in _statements(document):
        if statement.get("Effect") == "Allow" and "Action" in statement:
            assert "ec2:RunInstances" not in _string_list(statement["Action"])
    blanket = [
        statement
        for statement in _statements(document)
        if statement.get("Effect") == "Deny" and "NotAction" in statement
    ]

    assert len(blanket) == 1
    assert "ec2:RunInstances" not in _string_list(blanket[0]["NotAction"])
    assert _string_list(blanket[0]["Resource"]) == {"*"}


def test_kms_decrypt_allow_uses_the_supplied_region() -> None:
    parameters = _parameters()
    statement = _statement_with_actions(
        execution_policy(parameters),
        "Allow",
        {"kms:Decrypt"},
    )

    assert statement["Condition"] == {
        "StringEquals": {"kms:ViaService": f"s3.{parameters.region}.amazonaws.com"}
    }


def test_kms_decrypt_deny_requires_string_not_equals_for_the_region() -> None:
    parameters = _parameters()
    statement = _statement_with_actions(
        execution_policy(parameters),
        "Deny",
        {"kms:Decrypt"},
    )

    assert statement["Condition"] == {
        "StringNotEquals": {"kms:ViaService": f"s3.{parameters.region}.amazonaws.com"}
    }


def test_create_role_and_tag_role_require_the_supplied_boundary_arn() -> None:
    parameters = _parameters()
    statement = _statement_with_actions(
        execution_policy(parameters),
        "Allow",
        set(_CREATE_ROLE_ACTIONS),
    )

    assert _string_list(statement["Resource"]) == {_role_arn(parameters, "*")}
    assert statement["Condition"] == _boundary_condition(parameters)


def test_boundary_conditioned_role_actions_require_the_boundary_arn() -> None:
    parameters = _parameters()
    statement = _statement_with_actions(
        execution_policy(parameters),
        "Allow",
        set(_BOUNDARY_ROLE_ACTIONS),
    )

    assert _string_list(statement["Resource"]) == {_role_arn(parameters, "*")}
    assert statement["Condition"] == _boundary_condition(parameters)


def test_role_policy_reads_stay_unconditional() -> None:
    parameters = _parameters()
    statement = _statement_with_actions(
        execution_policy(parameters),
        "Allow",
        set(_UNCONDITIONAL_ROLE_READS),
    )

    assert _string_list(statement["Resource"]) == {_role_arn(parameters, "*")}
    assert "Condition" not in statement


def test_changing_a_role_boundary_is_denied() -> None:
    statement = _statement_with_actions(
        execution_policy(_parameters()),
        "Deny",
        set(_ROLE_BOUNDARY_DENIES),
    )

    assert _string_list(statement["Resource"]) == {"*"}
    assert "Condition" not in statement


def test_changing_the_boundary_policy_is_denied() -> None:
    parameters = _parameters()
    statement = _statement_with_actions(
        execution_policy(parameters),
        "Deny",
        set(_BOUNDARY_POLICY_DENIES),
    )

    assert _string_list(statement["Resource"]) == {
        _policy_arn(parameters, parameters.boundary_name)
    }
    assert "Condition" not in statement


def test_iam_is_denied_on_every_supplied_protected_arn() -> None:
    parameters = _parameters()
    statement = _statement_with_actions(
        execution_policy(parameters),
        "Deny",
        {"iam:*"},
    )

    assert _string_list(statement["Resource"]) == {
        _role_arn(parameters, parameters.provisioning_role_name),
        _role_arn(parameters, parameters.deployer_role_name),
        _role_arn(parameters, parameters.bootstrap_role_names[0]),
        _role_arn(parameters, parameters.bootstrap_role_names[1]),
        _policy_arn(parameters, parameters.execution_policy_name),
    }


def test_policy_documents_are_deterministic_json() -> None:
    parameters = _parameters()
    boundary = permissions_boundary(parameters)
    execution = execution_policy(parameters)

    assert permissions_boundary(parameters) == boundary
    assert execution_policy(parameters) == execution
    assert json.loads(json.dumps(boundary)) == boundary
    assert json.loads(json.dumps(execution)) == execution


def test_blanket_deny_not_action_is_exactly_the_set_of_allowed_actions() -> None:
    document = execution_policy(_parameters())
    allowed = {
        action
        for statement in _statements(document)
        if statement.get("Effect") == "Allow" and "Action" in statement
        for action in _string_list(statement["Action"])
    }
    blanket = [
        statement
        for statement in _statements(document)
        if statement.get("Effect") == "Deny" and "NotAction" in statement
    ]

    assert len(blanket) == 1
    assert _string_list(blanket[0]["NotAction"]) == allowed


def test_iam_pass_role_is_neither_allowed_nor_in_the_blanket_not_action() -> None:
    document = execution_policy(_parameters())
    for statement in _statements(document):
        if statement.get("Effect") == "Allow" and "Action" in statement:
            assert "iam:PassRole" not in _string_list(statement["Action"])
    blanket = [
        statement
        for statement in _statements(document)
        if statement.get("Effect") == "Deny" and "NotAction" in statement
    ]

    assert len(blanket) == 1
    assert "iam:PassRole" not in _string_list(blanket[0]["NotAction"])


def test_both_documents_use_the_2012_10_17_version() -> None:
    parameters = _parameters()

    assert permissions_boundary(parameters)["Version"] == "2012-10-17"
    assert execution_policy(parameters)["Version"] == "2012-10-17"


def test_the_allowlist_wildcards_are_the_eight_spec_services() -> None:
    assert _SERVICE_WILDCARDS == {
        "apigateway:*",
        "dynamodb:*",
        "events:*",
        "lambda:*",
        "logs:*",
        "s3:*",
        "sns:*",
        "sqs:*",
    }
