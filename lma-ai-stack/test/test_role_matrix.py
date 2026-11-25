# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The role matrix is the source of truth for who may call what, and nothing drifts from it.

lma-ai-stack/source/appsync/role-matrix.json names, for every Query, Mutation and
Subscription field and every other surface that accepts a user pool token, the
roles that may use it. These tests hold to it:

* schema.graphql: every root field names its roles itself, with exactly the
  matrix's group list and @aws_iam where the matrix says SigV4 is accepted, and
  the root types admit no signed-in role by default;
* the Cognito stack: the Participant group, the pre token generation trigger
  that names each caller's role in their tokens, and the identity pool's
  role mapping, which gives each role its IAM role and refuses any other;
* the role table in docs/user-based-access-control.md, which is generated from
  the matrix, and the doc's list of the other surfaces;
* that no template declares a Lambda function URL, a surface the matrix would
  otherwise have to list.

The UI, the transcriber and the Lambdas that apply the role rule hold their
copies to the matrix's vectors in their own suites.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import role_matrix
from ai_stack_templates import load as load_template
from graphql import parse
from graphql.language import ObjectTypeDefinitionNode, ObjectTypeExtensionNode

REPO_ROOT = role_matrix.REPO_ROOT
SCHEMA = REPO_ROOT / "lma-ai-stack" / "source" / "appsync" / "schema.graphql"
COGNITO = REPO_ROOT / "lma-cognito-stack" / "deployment" / "lma-cognito-stack.yaml"
ROOT_TYPES = ("Query", "Mutation", "Subscription")
AUTH_DIRECTIVES = (
    "aws_cognito_user_pools",
    "aws_iam",
    "aws_api_key",
    "aws_oidc",
    "aws_lambda",
    "aws_auth",
)

MATRIX = role_matrix.load()
ROLES = tuple(MATRIX["roles"])


def _root_nodes():
    for node in parse(SCHEMA.read_text()).definitions:
        if isinstance(node, (ObjectTypeDefinitionNode, ObjectTypeExtensionNode)):
            if node.name.value in ROOT_TYPES:
                yield node


def _auth(directives) -> dict:
    """The auth directives on a node: their names, and the cognito_groups list if any."""
    found: dict = {"names": [], "groups": None}
    for directive in directives or []:
        name = directive.name.value
        if name not in AUTH_DIRECTIVES:
            continue
        found["names"].append(name)
        if name == "aws_cognito_user_pools":
            found["groups"] = ()
            for argument in directive.arguments or []:
                if argument.name.value == "cognito_groups":
                    values = getattr(argument.value, "values", None)
                    found["groups"] = (
                        (argument.value.value,)
                        if values is None
                        else tuple(value.value for value in values)
                    )
    return found


FIELDS = {
    f"{node.name.value}.{field.name.value}": _auth(field.directives)
    for node in _root_nodes()
    for field in node.fields or []
}
MATRIX_ROLES = role_matrix.appsync_roles(MATRIX)
ALSO_IAM = set(MATRIX["alsoIam"]["appsync"])
BACKEND_ONLY = set(MATRIX["backendOnly"]["appsync"])


# ── the matrix itself ───────────────────────────────────────────────────────


def test_the_matrix_names_three_roles() -> None:
    assert ROLES == ("Admin", "User", "Participant")


def test_every_capability_names_known_roles_and_has_a_title() -> None:
    for capability in MATRIX["capabilities"]:
        assert capability["title"], capability["id"]
        assert capability["roles"], f"{capability['id']} admits no role; list it as backend only"
        unknown = set(capability["roles"]) - set(ROLES)
        assert not unknown, f"{capability['id']} names unknown roles {sorted(unknown)}"


def test_no_operation_route_or_surface_is_listed_twice() -> None:
    for key in ("appsync", "surfaces", "uiRoutes"):
        listed = [item for capability in MATRIX["capabilities"] for item in capability[key]]
        if key == "appsync":
            listed += MATRIX["backendOnly"]["appsync"]
        duplicates = sorted({item for item in listed if listed.count(item) > 1})
        assert not duplicates, f"{key} listed more than once: {duplicates}"


def test_operations_that_also_accept_iam_are_operations_a_role_may_call() -> None:
    signed_in = {op for op, roles in MATRIX_ROLES.items() if roles}
    assert ALSO_IAM <= signed_in, sorted(ALSO_IAM - signed_in)


def test_the_role_vectors_follow_the_stated_precedence() -> None:
    rule = MATRIX["effectiveRole"]
    for vector in rule["vectors"]:
        expected = next((r for r in rule["precedence"] if r in vector["groups"]), rule["default"])
        assert vector["role"] == expected, vector


# ── the schema ──────────────────────────────────────────────────────────────


def test_the_matrix_lists_exactly_the_fields_the_schema_declares() -> None:
    assert FIELDS, "no root fields found; the SDL parser is not seeing the schema"
    missing = sorted(set(FIELDS) - set(MATRIX_ROLES))
    stale = sorted(set(MATRIX_ROLES) - set(FIELDS))
    assert not missing, (
        "these fields are in schema.graphql but not in role-matrix.json; add each to the "
        f"capability whose roles may call it, or to backendOnly: {missing}"
    )
    assert not stale, f"role-matrix.json lists fields the schema does not declare: {stale}"


def test_the_root_types_admit_no_signed_in_role_by_default() -> None:
    """A field that names no role inherits @aws_iam, which every signed-in caller is refused."""
    for node in _root_nodes():
        assert _auth(node.directives)["names"] == ["aws_iam"], node.name.value


@pytest.mark.parametrize("operation", sorted(FIELDS))
def test_every_field_names_exactly_the_roles_the_matrix_lists(operation: str) -> None:
    auth = FIELDS[operation]
    roles = MATRIX_ROLES.get(operation)
    assert roles is not None, f"{operation} is not in role-matrix.json"
    assert auth["names"], f"{operation} carries no auth directive of its own"
    assert set(auth["names"]) <= {"aws_cognito_user_pools", "aws_iam"}, (
        f"{operation} uses {auth['names']}; the matrix covers user pool roles and IAM only"
    )
    if roles:
        assert auth["groups"], (
            f"{operation} must name its roles: "
            f"@aws_cognito_user_pools(cognito_groups: {list(roles)})"
        )
        assert sorted(auth["groups"]) == sorted(roles), (
            f"{operation} names {list(auth['groups'])}; role-matrix.json lists {list(roles)}"
        )
    else:
        assert auth["groups"] is None, f"{operation} is backend only; it names no signed-in role"
    expects_iam = operation in ALSO_IAM or operation in BACKEND_ONLY
    assert ("aws_iam" in auth["names"]) == expects_iam, (
        f"{operation}: @aws_iam {'missing' if expects_iam else 'present'}; "
        "role-matrix.json alsoIam/backendOnly says otherwise"
    )


def test_a_participant_is_refused_every_field_but_those_the_matrix_lists() -> None:
    admitted = sorted(op for op, auth in FIELDS.items() if "Participant" in (auth["groups"] or ()))
    listed = sorted(op for op, roles in MATRIX_ROLES.items() if "Participant" in roles)
    assert admitted == listed


# ── the Cognito stack ───────────────────────────────────────────────────────

COGNITO_RESOURCES = load_template(COGNITO)["Resources"]


def _of_type(type_name: str) -> dict:
    return {k: v for k, v in COGNITO_RESOURCES.items() if v["Type"] == type_name}


def test_the_user_pool_has_a_group_for_each_role_but_user() -> None:
    names = {v["Properties"]["GroupName"] for v in _of_type("AWS::Cognito::UserPoolGroup").values()}
    assert names == {"Admin", "Participant"}


def test_every_token_is_issued_through_the_version_one_pre_token_trigger() -> None:
    config = COGNITO_RESOURCES["UserPool"]["Properties"]["LambdaConfig"]["PreTokenGenerationConfig"]
    assert config["LambdaVersion"] == "V1_0"
    assert config["LambdaArn"] == {"Fn::GetAtt": "CognitoPreTokenGenerationFunction.Arn"}
    function = COGNITO_RESOURCES["CognitoPreTokenGenerationFunction"]["Properties"]
    assert function["CodeUri"] == "../source/pre_token_generation"
    assert (COGNITO.parent / function["CodeUri"] / "index.py").is_file()
    permission = COGNITO_RESOURCES["CognitoPreTokenGenerationPermission"]["Properties"]
    assert permission["Principal"] == "cognito-idp.amazonaws.com"
    assert permission["SourceArn"] == {"Fn::GetAtt": "UserPool.Arn"}


def test_the_identity_pool_gives_each_role_its_iam_role_and_refuses_any_other() -> None:
    attachment = COGNITO_RESOURCES["CognitoIdentityPoolSetRole"]["Properties"]
    (mapping,) = attachment["RoleMappings"].values()
    assert mapping["Type"] == "Rules"
    assert mapping["AmbiguousRoleResolution"] == "Deny"
    rules = {
        rule["Value"]: rule["RoleARN"]
        for rule in mapping["RulesConfiguration"]["Rules"]
        if rule["Claim"] == "lma_role" and rule["MatchType"] == "Equals"
    }
    assert len(rules) == len(mapping["RulesConfiguration"]["Rules"])
    web_ui = {"Fn::GetAtt": "CognitoAuthorizedRole.Arn"}
    credentials = next(c for c in MATRIX["capabilities"] if "identity-pool-credentials" in c["surfaces"])
    assert rules == {
        role: web_ui if role in credentials["roles"] else {"Fn::GetAtt": "CognitoParticipantRole.Arn"}
        for role in ROLES
    }


def test_the_participant_identity_pool_role_grants_nothing() -> None:
    role = COGNITO_RESOURCES["CognitoParticipantRole"]["Properties"]
    assert "Policies" not in role
    assert "ManagedPolicyArns" not in role
    # Assumable only through this identity pool, by an authenticated identity.
    statement = role["AssumeRolePolicyDocument"]["Statement"][0]
    assert statement["Condition"]["StringEquals"] == {
        "cognito-identity.amazonaws.com:aud": {"Ref": "IdentityPool"}
    }
    assert statement["Condition"]["ForAnyValue:StringLike"] == {
        "cognito-identity.amazonaws.com:amr": "authenticated"
    }


# ── other surfaces ──────────────────────────────────────────────────────────


def test_every_surface_belongs_to_one_capability_and_names_real_files() -> None:
    in_capabilities = {s for c in MATRIX["capabilities"] for s in c["surfaces"]}
    assert in_capabilities == set(MATRIX["surfaces"])
    for surface_id, surface in MATRIX["surfaces"].items():
        for path in [surface["enforcedIn"], *surface["tests"]]:
            assert (REPO_ROOT / path).is_file(), f"{surface_id} names {path}, which does not exist"


def _templates() -> list[Path]:
    skip = {"node_modules", ".aws-sam", "build", "dist", "cdk.out"}
    return [
        path
        for pattern in ("*.yaml", "*.yml")
        for path in REPO_ROOT.rglob(pattern)
        if not skip.intersection(path.parts) and "AWSTemplateFormatVersion" in path.read_text(errors="ignore")
    ]


def test_no_template_declares_a_lambda_function_url() -> None:
    """A function URL would be a surface outside the matrix, reached with no user pool role."""
    templates = _templates()
    assert len(templates) > 5, "the template scan found too few templates to be looking in the right place"
    offending = [
        str(path.relative_to(REPO_ROOT))
        for path in templates
        if re.search(r"AWS::Lambda::Url|FunctionUrlConfig", path.read_text(errors="ignore"))
    ]
    assert not offending, offending


# ── the documentation ───────────────────────────────────────────────────────


def test_the_role_table_in_the_docs_is_the_one_the_matrix_generates() -> None:
    text = role_matrix.DOC_PATH.read_text(encoding="utf-8")
    assert role_matrix.doc_table(text) == role_matrix.render_table(MATRIX), (
        "the role table in docs/user-based-access-control.md differs from role-matrix.json; "
        "regenerate it with: python3 lma-ai-stack/test/role_matrix.py --write-docs"
    )


def test_the_docs_list_every_surface() -> None:
    text = role_matrix.DOC_PATH.read_text(encoding="utf-8")
    missing = [s["title"] for s in MATRIX["surfaces"].values() if s["title"] not in text]
    assert not missing, f"docs/user-based-access-control.md does not list: {missing}"
