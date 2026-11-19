#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Every AWSJSON value this Lambda returns reaches the client encoded once.

An AWSJSON field arrives at the client as one JSON string, which the SDK and the
UI parse once into the object. Three steps sit between the handler and that
string, and each of them encodes or decodes:

1. the resolver's response mapping template, ``$util.toJson($ctx.result)``,
   writes the handler's return value as JSON text;
2. AppSync parses that text back into the field's value;
3. AppSync serializes each AWSJSON value as JSON for the response. A string is
   serialized as a JSON string, not passed through (measured on a deployed
   stack: a handler that returned ``json.dumps(document)`` reached the SDK as a
   string whose content was itself a JSON string).

So a handler has to return an AWSJSON value as an object or a list. These tests
run the real handler for every field served by this Lambda whose result holds
an AWSJSON value, push its return value through the response template the stack
deploys for that field and through AppSync's serialization, and parse it once
as a client does: the result must be the object, not text. A new field of that
kind fails ``test_every_awsjson_field_of_this_lambda_is_evaluated`` until it has
a case here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

import mcp_library
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
from test_mcp_library import ADMIN, IAM, RECORDS, FakeTable, _export, _run

AI_STACK = Path(__file__).resolve().parents[3]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
SCHEMA = AI_STACK / "source" / "appsync" / "schema.graphql"
DATA_SOURCE = "MCPServerManagerDataSource"

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

# The response template every resolver of this data source uses: any AppSync
# error first, then the handler's result as JSON.
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


def _imported_table() -> FakeTable:
    """A table holding two imported servers that still wait for their credentials."""
    table = FakeTable([])
    # AppSync hands an AWSJSON argument to the request template as a parsed map,
    # so the handler receives the library as an object.
    result = _run(table, "importMCPServerLibrary", ADMIN, library=_export([RECORDS[2], RECORDS[5]]))
    assert result["Success"], result
    assert all(server["CredentialsRequired"] for server in result["Servers"])
    return table


def _export_case() -> Any:
    return _run(FakeTable(RECORDS), "exportMCPServerLibrary", IAM)


def _list_case() -> Any:
    return _run(_imported_table(), "listInstalledMCPServers", ADMIN)


def _get_case() -> Any:
    table = _imported_table()
    server_id = next(iter(table.items))
    return _run(table, "getMCPServer", ADMIN, serverId=server_id)


#: A handler run per field, each one returning at least one AWSJSON value.
CASES: Dict[Tuple[str, str], Callable[[], Any]] = {
    ("Query", "exportMCPServerLibrary"): _export_case,
    ("Query", "listInstalledMCPServers"): _list_case,
    ("Query", "getMCPServer"): _get_case,
}


def _as_the_client_reads_it(field: Tuple[str, str], result: Any) -> List[Any]:
    """The AWSJSON values of ``result`` as a client has them after one JSON parse."""
    # 1-2. $util.toJson($ctx.result), then AppSync parses the template's output.
    value = json.loads(json.dumps(result))
    graphql_type = SCHEMA_OBJECT.type_map[field[0]].fields[field[1]].type
    # 3. each AWSJSON value is serialized as JSON; the client parses it once.
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
    # The model of steps 1-2 above holds for this template only. Another
    # template needs its own evaluation here.
    assert TO_JSON_TEMPLATE.fullmatch(RESOLVERS[field]), RESOLVERS[field]


@pytest.mark.parametrize("field", list(CASES), ids=lambda field: ".".join(field))
def test_each_awsjson_value_reaches_the_client_as_an_object(field: Tuple[str, str]) -> None:
    values = _as_the_client_reads_it(field, CASES[field]())
    assert values, f"the {field[1]} case returned no AWSJSON value to check"
    for value in values:
        assert isinstance(value, (dict, list)), (
            f"{field[1]}: one parse gave {type(value).__name__} {str(value)[:60]!r}; "
            "return the object from the handler, not its JSON text"
        )


def test_the_exported_library_is_the_document_after_one_parse() -> None:
    (document,) = _as_the_client_reads_it(("Query", "exportMCPServerLibrary"), _export_case())
    assert document["kind"] == mcp_library.LIBRARY_KIND
    assert len(document["servers"]) == len(RECORDS)


def test_the_imported_settings_are_the_settings_after_one_parse() -> None:
    settings = _as_the_client_reads_it(("Query", "listInstalledMCPServers"), _list_case())
    by_type = {entry["authType"]: entry for entry in settings}
    assert by_type["custom_headers"]["headers"] == {"X-API-Key": "", "X-Team": ""}
    assert by_type["oauth_client_credentials"]["oauth"]["clientId"] == "client-id-123"
