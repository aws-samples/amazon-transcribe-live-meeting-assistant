# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The Cognito stack's PostAuthentication trigger, which records each user's last
sign-in time (no AWS). See docs/user-management.md#last-sign-in.

The trigger's code is inline in lma-cognito-stack/deployment/lma-cognito-stack.yaml,
as the stack's other two triggers are. These tests take it out of the template and
run it against an in-process DynamoDB double, and check how the template wires it:

* a sign-in writes the time, in AppSync's AWSDateTime form, under the user's
  Cognito sub, and only ever moves it forward;
* the trigger returns the event it was given whatever happens, so a write that
  fails, times out or is refused never fails the user's sign-in, and its AWS
  calls give up well inside the five seconds Cognito waits for a trigger;
* the user pool calls it as its PostAuthentication trigger, Cognito may invoke
  it for this pool only, and its role may only update the sign-in table;
* the user pool's schema is unchanged (no attribute is added to it), the table
  is encrypted with the stack's key and kept with the user pool, and its name
  reaches the AI stack's user management resolver through lma-main.yaml.
"""

from __future__ import annotations

import re
import types
from pathlib import Path
from unittest import mock

import pytest
from ai_stack_templates import load
from botocore.exceptions import ClientError, ConnectTimeoutError

REPO_ROOT = Path(__file__).resolve().parents[2]
COGNITO = REPO_ROOT / "lma-cognito-stack" / "deployment" / "lma-cognito-stack.yaml"
MAIN = REPO_ROOT / "lma-main.yaml"
AI_STACK = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"

FUNCTION = "CognitoPostAuthenticationFunction"
TABLE = "UserSignInTable"
#: How long Cognito waits for a trigger before failing the sign-in.
COGNITO_TRIGGER_WAIT_S = 5
#: What is left of that wait for the function's cold start.
COLD_START_ALLOWANCE_S = 1


@pytest.fixture(scope="module")
def cognito() -> dict:
    return load(COGNITO)


@pytest.fixture(scope="module")
def resources(cognito: dict) -> dict:
    return cognito["Resources"]


class ConditionalCheckFailedException(ClientError):
    """Stand-in for the modelled DynamoDB error the trigger catches by class."""

    def __init__(self) -> None:
        super().__init__(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "later"}},
            "UpdateItem",
        )


class FakeDynamo:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.error: Exception | None = None
        self.exceptions = types.SimpleNamespace(
            ConditionalCheckFailedException=ConditionalCheckFailedException
        )

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _trigger(resources: dict, table: str = "LMA-UserSignInTable"):
    """The inline code as a module, with its DynamoDB client replaced; and the client."""
    code = resources[FUNCTION]["Properties"]["InlineCode"]
    fake = FakeDynamo()
    made: list[dict] = []

    def client(service, **kwargs):
        assert service == "dynamodb"
        made.append(kwargs)
        return fake

    module = types.ModuleType("cognito_post_authentication")
    with (
        mock.patch("boto3.client", client),
        mock.patch.dict("os.environ", {"USER_SIGN_IN_TABLE": table}),
    ):
        exec(compile(code, "cognito_post_authentication.py", "exec"), module.__dict__)  # noqa: S102  # nosec B102
    module.made = made
    return module, fake


def _event(sub: str | None = "1111-2222") -> dict:
    attributes = {"email": "user@example.com", "email_verified": "true"}
    if sub is not None:
        attributes["sub"] = sub
    return {
        "version": "1",
        "triggerSource": "PostAuthentication_Authentication",
        "region": "us-east-1",
        "userPoolId": "us-east-1_example",
        "userName": "user@example.com",
        "callerContext": {"clientId": "exampleclientid"},
        "request": {"userAttributes": attributes, "newDeviceUsed": False},
        "response": {},
    }


# ── what a sign-in records ─────────────────────────────────────────────────


def test_a_sign_in_records_the_time_under_the_users_sub(resources: dict) -> None:
    trigger, dynamo = _trigger(resources)
    event = _event()

    assert trigger.lambda_handler(event, None) is event

    (update,) = dynamo.updates
    assert update["TableName"] == "LMA-UserSignInTable"
    assert update["Key"] == {"UserSub": {"S": "1111-2222"}}
    assert update["UpdateExpression"] == "SET LastSignInAt = :at"
    at = update["ExpressionAttributeValues"][":at"]["S"]
    # AWSDateTime needs the time zone designator; the resolver passes it through.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z", at), at


def test_the_recorded_time_only_moves_forward(resources: dict) -> None:
    """Two sign-ins at once: the earlier one's write must not replace the later."""
    trigger, dynamo = _trigger(resources)

    trigger.lambda_handler(_event(), None)

    assert dynamo.updates[0]["ConditionExpression"] == (
        "attribute_not_exists(LastSignInAt) OR LastSignInAt < :at"
    )


def test_an_event_without_a_sub_records_nothing(resources: dict) -> None:
    trigger, dynamo = _trigger(resources)
    event = _event(sub=None)

    assert trigger.lambda_handler(event, None) is event
    assert dynamo.updates == []


def test_nothing_is_recorded_without_a_table(resources: dict) -> None:
    trigger, dynamo = _trigger(resources, table="")
    event = _event()

    assert trigger.lambda_handler(event, None) is event
    assert dynamo.updates == []


# ── a sign-in never fails because of the trigger ───────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        ConditionalCheckFailedException(),
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "UpdateItem"),
        ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
            "UpdateItem",
        ),
        ConnectTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com"),
        RuntimeError("anything else"),
    ],
    ids=["later-time-recorded", "denied", "throttled", "timed-out", "unexpected"],
)
def test_a_failed_write_still_returns_the_event(resources: dict, error: Exception) -> None:
    trigger, dynamo = _trigger(resources)
    dynamo.error = error
    event = _event()

    assert trigger.lambda_handler(event, None) is event


def test_the_write_gives_up_inside_the_time_cognito_waits(resources: dict) -> None:
    trigger, _dynamo = _trigger(resources)

    (kwargs,) = trigger.made
    config = kwargs["config"]
    attempts = config.retries["total_max_attempts"]
    # botocore's standard mode waits up to a second before each retry.
    worst_case = attempts * (config.connect_timeout + config.read_timeout) + (attempts - 1) * 1.0
    # A second is left for the cold start (importing boto3), which Cognito counts too.
    assert worst_case <= COGNITO_TRIGGER_WAIT_S - COLD_START_ALLOWANCE_S
    assert resources[FUNCTION]["Properties"]["Timeout"] <= COGNITO_TRIGGER_WAIT_S


# ── how the template wires it ──────────────────────────────────────────────


def test_the_user_pool_calls_it_after_each_sign_in(resources: dict) -> None:
    config = resources["UserPool"]["Properties"]["LambdaConfig"]
    assert config["PostAuthentication"] == {"Fn::GetAtt": f"{FUNCTION}.Arn"}


def test_only_this_user_pool_may_invoke_it(resources: dict) -> None:
    permission = resources["CognitoPostAuthenticationPermission"]["Properties"]
    assert permission["FunctionName"] == {"Ref": FUNCTION}
    assert permission["Principal"] == "cognito-idp.amazonaws.com"
    assert permission["SourceArn"] == {"Fn::GetAtt": "UserPool.Arn"}
    assert permission["SourceAccount"] == {"Ref": "AWS::AccountId"}


def test_its_role_may_only_update_the_sign_in_table(resources: dict) -> None:
    role = resources["CognitoPostAuthenticationFunctionRole"]["Properties"]
    statements = [s for policy in role["Policies"] for s in policy["PolicyDocument"]["Statement"]]
    dynamo = [s for s in statements if any(a.startswith("dynamodb:") for a in s["Action"])]
    assert dynamo == [
        {
            "Effect": "Allow",
            "Action": ["dynamodb:UpdateItem"],
            "Resource": {"Fn::GetAtt": f"{TABLE}.Arn"},
        }
    ]
    assert "PermissionsBoundary" in role
    assert resources[FUNCTION]["Properties"]["Environment"]["Variables"] == {
        "USER_SIGN_IN_TABLE": {"Ref": TABLE}
    }


def test_it_writes_to_the_log_group_the_stack_declares(resources: dict) -> None:
    function = resources[FUNCTION]
    group = resources["CognitoPostAuthenticationFunctionLogGroup"]["Properties"]
    assert function["Properties"]["LoggingConfig"]["LogGroup"] == group["LogGroupName"]
    assert "CognitoPostAuthenticationFunctionLogGroup" in function["DependsOn"]
    assert group["KmsKeyId"] == {"Ref": "CustomerManagedEncryptionKeyArn"}


def test_the_user_pool_schema_is_unchanged(resources: dict) -> None:
    """The times live in a table: no attribute is added to the user pool."""
    schema = resources["UserPool"]["Properties"]["Schema"]
    assert [attribute["Name"] for attribute in schema] == ["email"]


def test_the_sign_in_table_is_keyed_by_sub_encrypted_and_kept_with_the_user_pool(
    resources: dict,
) -> None:
    table = resources[TABLE]
    properties = table["Properties"]
    assert properties["KeySchema"] == [{"AttributeName": "UserSub", "KeyType": "HASH"}]
    assert properties["SSESpecification"] == {
        "SSEEnabled": True,
        "SSEType": "KMS",
        "KMSMasterKeyId": {"Ref": "CustomerManagedEncryptionKeyArn"},
    }
    assert properties["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert table["DeletionPolicy"] == resources["UserPool"]["DeletionPolicy"]


def test_the_table_name_reaches_the_user_management_resolver(cognito: dict) -> None:
    assert cognito["Outputs"]["UserSignInTableName"]["Value"] == {"Ref": TABLE}

    main = load(MAIN)["Resources"]["AISTACK"]["Properties"]["Parameters"]
    assert main["UserSignInTableName"] == {
        "Fn::GetAtt": "COGNITOSTACK.Outputs.UserSignInTableName"
    }

    ai = load(AI_STACK)
    # Empty by default, so the AI stack still deploys without it.
    assert ai["Parameters"]["UserSignInTableName"]["Default"] == ""
    function = ai["Resources"]["UserManagementFunction"]["Properties"]
    assert function["Environment"]["Variables"]["USER_SIGN_IN_TABLE"] == {
        "Ref": "UserSignInTableName"
    }


def test_the_resolver_may_read_the_sign_in_table_and_delete_from_it(cognito: dict) -> None:
    ai = load(AI_STACK)
    role = ai["Resources"]["UserManagementFunctionExecutionRole"]["Properties"]
    statements = [s for policy in role["Policies"] for s in policy["PolicyDocument"]["Statement"]]
    conditional = [s["Fn::If"] for s in statements if "Fn::If" in s]
    assert len(conditional) == 1
    condition, statement, otherwise = conditional[0]
    assert condition == "HasUserSignInTable"
    assert otherwise == {"Ref": "AWS::NoValue"}
    assert statement["Action"] == ["dynamodb:BatchGetItem", "dynamodb:DeleteItem"]
    assert statement["Resource"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}"
            ":table/${UserSignInTableName}"
        )
    }
    assert ai["Conditions"]["HasUserSignInTable"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "UserSignInTableName"}, ""]}]
    }
