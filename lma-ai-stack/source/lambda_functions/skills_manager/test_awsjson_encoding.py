#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Every AWSJSON value this Lambda returns reaches the client encoded once.

The same check as ``mcp_server_manager/test_awsjson_encoding.py``, for the skills
manager. Between the handler and the client, the response template
``$util.toJson($ctx.result)`` writes the result as JSON, AppSync parses it back,
and AppSync serializes each AWSJSON value as JSON again (a string becomes a JSON
string). So the handler returns AWSJSON values as objects, and these tests run
the real handler for every field of this Lambda whose result holds AWSJSON, push
the result through that template and serialization, and parse each value once
as a client does. A new field of that kind fails
``test_every_awsjson_field_of_this_lambda_is_evaluated`` until it has a case here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest
from graphql import (
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    build_ast_schema,
    get_named_type,
    parse,
)
from test_skills_manager import IAM, SKILL, USER, call, table  # noqa: F401 (fixture)

AI_STACK = Path(__file__).resolve().parents[3]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
SCHEMA = AI_STACK / "source" / "appsync" / "schema.graphql"
DATA_SOURCE = "SkillsManagerDataSource"

# The scalars and directives AppSync declares implicitly, so graphql-core can
# build the schema. Nothing about the schema's own types is changed.
PRELUDE = """
scalar AWSDate
scalar AWSTime
scalar AWSDateTime
scalar AWSTimestamp
scalar AWSEmail
scalar AWSJSON
scalar AWSURL
scalar AWSPhone
scalar AWSIPAddress
directive @aws_subscribe(mutations: [String!]!) on FIELD_DEFINITION
directive @aws_auth(cognito_groups: [String!]) on FIELD_DEFINITION
directive @aws_cognito_user_pools(cognito_groups: [String!]) on OBJECT | FIELD_DEFINITION
directive @aws_iam on OBJECT | FIELD_DEFINITION
directive @aws_api_key on OBJECT | FIELD_DEFINITION
directive @aws_oidc on OBJECT | FIELD_DEFINITION
directive @aws_lambda on OBJECT | FIELD_DEFINITION
"""

TO_JSON_TEMPLATE = re.compile(
    r"\s*#if\(\$ctx\.error\)\s*\$util\.error\(\$ctx\.error\.message, \$ctx\.error\.type\)\s*"
    r"#end\s*\$util\.toJson\(\$ctx\.result\)\s*"
)

SCHEMA_OBJECT = build_ast_schema(parse(PRELUDE + SCHEMA.read_text(encoding="utf-8")))


def _holds_awsjson(graphql_type: Any, seen: Tuple[str, ...] = ()) -> bool:
    named = get_named_type(graphql_type)
    if isinstance(named, GraphQLScalarType):
        return named.name == "AWSJSON"
    if isinstance(named, GraphQLObjectType) and named.name not in seen:
        return any(
            _holds_awsjson(field.type, seen + (named.name,)) for field in named.fields.values()
        )
    return False


def _awsjson_values(graphql_type: Any, value: Any) -> Iterator[Any]:
    """Every value in ``value`` at a position the schema types AWSJSON."""
    if value is None:
        return
    if isinstance(graphql_type, GraphQLNonNull):
        yield from _awsjson_values(graphql_type.of_type, value)
    elif isinstance(graphql_type, GraphQLList):
        for item in value:
            yield from _awsjson_values(graphql_type.of_type, item)
    elif isinstance(graphql_type, GraphQLScalarType):
        if graphql_type.name == "AWSJSON":
            yield value
    elif isinstance(graphql_type, GraphQLObjectType):
        for name, field in graphql_type.fields.items():
            if name in value:
                yield from _awsjson_values(field.type, value[name])


def _resolvers() -> Dict[Tuple[str, str], str]:
    """(type, field) -> response mapping template, for this data source's resolvers."""
    found = {}
    for block in re.split(r"\n  (?=\w+:\n)", TEMPLATE.read_text(encoding="utf-8")):
        if (
            "Type: AWS::AppSync::Resolver" not in block
            or f"!GetAtt {DATA_SOURCE}.Name" not in block
        ):
            continue
        type_name = re.search(r"^      TypeName: (\S+)", block, re.M).group(1)
        field_name = re.search(r"^      FieldName: (\S+)", block, re.M).group(1)
        template = re.search(
            r"^      ResponseMappingTemplate: \|\n((?:        .*\n|\s*\n)+)", block + "\n", re.M
        )
        found[(type_name, field_name)] = template.group(1) if template else ""
    return found


RESOLVERS = _resolvers()


def _created() -> None:
    result = call("createSkill", input=SKILL)
    assert result["Success"], result


def _update() -> Any:
    _created()
    return call("updateSkill", skillId="launch-review", input={**SKILL, "Description": "Changed."})


def _delete() -> Any:
    _created()
    return call("deleteSkill", skillId="launch-review")


#: A handler run per field, and whether its result holds an AWSJSON value to
#: check. deleteSkill answers with no Skill, so it has none: its case asserts that.
CASES: Dict[Tuple[str, str], Tuple[Callable[[], Any], bool]] = {
    ("Mutation", "createSkill"): (lambda: call("createSkill", input=SKILL), True),
    ("Mutation", "updateSkill"): (_update, True),
    ("Mutation", "deleteSkill"): (_delete, False),
    ("Query", "listSkills"): (lambda: (_created(), call("listSkills", USER))[1], True),
    ("Query", "getSkill"): (
        lambda: (_created(), call("getSkill", USER, skillId="launch-review"))[1],
        True,
    ),
    ("Query", "exportSkill"): (
        lambda: (_created(), call("exportSkill", USER, skillId="launch-review"))[1],
        True,
    ),
    ("Query", "exportSkillLibrary"): (
        lambda: (_created(), call("exportSkillLibrary", IAM))[1],
        True,
    ),
}


def _as_the_client_reads_it(field: Tuple[str, str], result: Any) -> List[Any]:
    """The AWSJSON values of ``result`` as a client has them after one JSON parse."""
    value = json.loads(json.dumps(result))  # $util.toJson, then AppSync parses it
    graphql_type = SCHEMA_OBJECT.type_map[field[0]].fields[field[1]].type
    # Each AWSJSON value is serialized as JSON; the client parses it once.
    return [json.loads(json.dumps(item)) for item in _awsjson_values(graphql_type, value)]


def test_every_awsjson_field_of_this_lambda_is_evaluated() -> None:
    holding = {
        field
        for field in RESOLVERS
        if _holds_awsjson(SCHEMA_OBJECT.type_map[field[0]].fields[field[1]].type)
    }
    assert holding, "found no field of this Lambda holding AWSJSON; the parser is broken"
    assert holding == set(CASES), (
        "add a case to CASES for each field of this Lambda whose result holds AWSJSON"
    )


@pytest.mark.parametrize("field", list(CASES), ids=lambda field: ".".join(field))
def test_the_response_template_writes_the_result_as_json(field: Tuple[str, str]) -> None:
    # The model above holds for this template only; another needs its own case.
    assert TO_JSON_TEMPLATE.fullmatch(RESOLVERS[field]), RESOLVERS[field]


@pytest.mark.parametrize("field", list(CASES), ids=lambda field: ".".join(field))
def test_each_awsjson_value_reaches_the_client_as_an_object(
    table,  # noqa: F811
    field: Tuple[str, str],
) -> None:
    run, holds_values = CASES[field]
    values = _as_the_client_reads_it(field, run())
    assert bool(values) == holds_values, values
    for value in values:
        assert isinstance(value, (dict, list)), (
            f"{field[1]}: one parse gave {type(value).__name__} {str(value)[:60]!r}; "
            "return the object from the handler, not its JSON text"
        )


def test_the_exports_and_metadata_are_what_a_client_reads_after_one_parse(table) -> None:  # noqa: F811
    _created()
    (library,) = _as_the_client_reads_it(
        ("Query", "exportSkillLibrary"), call("exportSkillLibrary", IAM)
    )
    assert library["kind"] == "lma-skill-library" and len(library["skills"]) == 1
    (exported,) = _as_the_client_reads_it(
        ("Query", "exportSkill"), call("exportSkill", USER, skillId="launch-review")
    )
    assert exported["fileName"] == "SKILL.md" and exported["format"] == "skill-md"
    (metadata,) = _as_the_client_reads_it(
        ("Query", "getSkill"), call("getSkill", USER, skillId="launch-review")
    )
    assert metadata == {"trigger": "review the launch"}
