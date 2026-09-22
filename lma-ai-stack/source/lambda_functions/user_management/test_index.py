# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the user_management AppSync resolver (no AWS calls).

This resolver is the only path the web UI has for changing who can sign in to a
deployment, so the behaviour pinned here is what keeps a deployment
administrable:

* Admin membership is re-checked inside the Lambda, independently of the
  AppSync ``@aws_auth`` directive, and the check has to cope with every shape
  AppSync uses for the identity block: ``cognito:groups`` arrives as a list when
  the caller is in several groups and as a bare string when it is in one, and
  the block itself may be absent.
* Two guard rails keep a deployment from losing every administrator: a caller
  cannot delete its own account, and the last remaining Admin cannot be deleted.
  If either regresses, the user pool can end up with no Admin at all and the
  user-management page becomes unusable for everyone, with no in-product way
  back.
* Validation of the email address, of the optional allowed-domain list, and of
  the two-value role vocabulary all runs before Cognito is touched, so a
  rejected request leaves nothing behind. When the Admin group cannot be set on
  a user that was just created, the creation is rolled back rather than leaving
  a user with the wrong role.
* The mapping from Cognito's response shape to the GraphQL ``User`` type,
  including the timezone designator that AppSync requires on an ``AWSDateTime``
  and the Cognito field names, which differ between ``ListUsers``
  (``Attributes``) and ``AdminGetUser`` (``UserAttributes``).

Cognito is replaced by an in-process double. Its ``exceptions`` namespace holds
real ``ClientError`` subclasses because the module catches
``cognito.exceptions.*`` by class, which a plain mock cannot satisfy.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from botocore.exceptions import ClientError

# index.py reads USER_POOL_ID and builds a cognito-idp client at import time.
# Client construction resolves an endpoint, so without a region the import
# itself raises NoRegionError on a machine with no ambient AWS config.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("USER_POOL_ID", "us-east-1_testpool")


def _load_index():
    """Load this directory's index.py under a name of its own.

    Every Lambda source directory in this tree has an ``index.py``, so a plain
    ``import index`` resolves to whichever one reached ``sys.modules`` first when
    the whole tree is collected in one pytest run.
    """
    spec = importlib.util.spec_from_file_location(
        "user_management_index", Path(__file__).resolve().parent / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"):
    index = _load_index()

ADMIN_GROUP = "Admin"
CALLER = "admin@example.com"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CognitoError(ClientError):
    """A ClientError shaped like the ones botocore raises for cognito-idp."""

    def __init__(self, code: str = "InternalErrorException") -> None:
        super().__init__({"Error": {"Code": code, "Message": code}}, "CognitoOperation")


class UsernameExistsException(_CognitoError):
    """Stand-in for the modelled Cognito error of the same name."""


class UserNotFoundException(_CognitoError):
    """Stand-in for the modelled Cognito error of the same name."""


class _FakePaginator:
    def __init__(self, client: FakeCognito, operation_name: str) -> None:
        self.client = client
        self.operation_name = operation_name

    def paginate(self, **kwargs):
        self.client.record(f"paginate:{self.operation_name}", kwargs)
        if self.operation_name == "list_users":
            if self.client.list_users_error is not None:
                raise self.client.list_users_error
            return list(self.client.user_pages)
        if self.operation_name == "list_users_in_group":
            if self.client.admin_group_pages is not None:
                return list(self.client.admin_group_pages)
            return [{"Users": [{"Username": name} for name in self.client.admins]}]
        raise AssertionError(f"unexpected paginator requested: {self.operation_name}")


class FakeCognito:
    """In-process stand-in for the module-scope cognito-idp client.

    Records every call so a test can assert that a refused request reached
    Cognito with nothing at all, which is the point of the guard rails.
    """

    def __init__(self) -> None:
        self.user_pages: list[dict] = [{"Users": []}]
        self.admins: list[str] = []
        self.admin_group_pages: list[dict] | None = None
        self.get_user_result: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.group_lookup_error: ClientError | None = None
        self.list_users_error: ClientError | None = None
        self.create_error: ClientError | None = None
        self.add_to_group_error: ClientError | None = None
        self.delete_error: ClientError | None = None
        self.exceptions = SimpleNamespace(
            UsernameExistsException=UsernameExistsException,
            UserNotFoundException=UserNotFoundException,
        )

    # -- bookkeeping --------------------------------------------------------

    def record(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, kwargs))

    def names_called(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_for(self, name: str) -> list[dict]:
        return [kwargs for called, kwargs in self.calls if called == name]

    # -- the slice of the cognito-idp API the resolver uses -----------------

    def get_paginator(self, operation_name: str) -> _FakePaginator:
        return _FakePaginator(self, operation_name)

    def admin_list_groups_for_user(self, **kwargs):
        self.record("admin_list_groups_for_user", kwargs)
        if self.group_lookup_error is not None:
            raise self.group_lookup_error
        if kwargs["Username"] in self.admins:
            return {"Groups": [{"GroupName": ADMIN_GROUP}]}
        # A non-admin is still in groups; the role must come from the name.
        return {"Groups": [{"GroupName": "SomeOtherGroup"}]}

    def admin_create_user(self, **kwargs):
        self.record("admin_create_user", kwargs)
        if self.create_error is not None:
            raise self.create_error
        return {}

    def admin_add_user_to_group(self, **kwargs):
        self.record("admin_add_user_to_group", kwargs)
        if self.add_to_group_error is not None:
            raise self.add_to_group_error
        self.admins.append(kwargs["Username"])
        return {}

    def admin_delete_user(self, **kwargs):
        self.record("admin_delete_user", kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return {}

    def admin_get_user(self, **kwargs):
        self.record("admin_get_user", kwargs)
        return self.get_user_result


@pytest.fixture(name="cognito")
def cognito_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeCognito:
    """Swap the module-scope client; the resolver looks it up on every call."""
    fake = FakeCognito()
    monkeypatch.setattr(index, "cognito", fake)
    return fake


# ---------------------------------------------------------------------------
# Event and user builders
# ---------------------------------------------------------------------------


def admin_event(field: str, arguments: dict | None = None, *, username: str = CALLER) -> dict:
    """An AppSync event whose caller is a member of the Admin group."""
    return {
        "info": {"fieldName": field},
        "identity": {"claims": {"cognito:username": username, "cognito:groups": [ADMIN_GROUP]}},
        "arguments": arguments or {},
    }


def cognito_user(
    username: str,
    *,
    email: str = "",
    created: datetime | None = None,
    status: str = "CONFIRMED",
    enabled: bool | None = True,
) -> dict:
    """A user dict shaped like a ListUsers entry.

    Defaults omit the email attribute and the creation date so that the
    resolver's handling of absent fields is the easy case to write.
    """
    user: dict = {"Username": username, "UserStatus": status}
    if enabled is not None:
        user["Enabled"] = enabled
    if email:
        user["Attributes"] = [{"Name": "email", "Value": email}]
    if created is not None:
        user["UserCreateDate"] = created
    return user


def create_input(email: str, role: str) -> dict:
    return {"input": {"email": email, "role": role}}


def delete_input(username: str) -> dict:
    return {"input": {"username": username}}


# ---------------------------------------------------------------------------
# Authorization re-check
# ---------------------------------------------------------------------------


def test_an_admin_caller_reaches_the_operation(cognito: FakeCognito) -> None:
    cognito.user_pages = [{"Users": [cognito_user("someone@example.com")]}]

    result = index.handler(admin_event("listUsers"), None)

    assert [user["username"] for user in result["users"]] == ["someone@example.com"]


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {},
        {"claims": None},
        {"claims": {}},
        {"claims": {"cognito:groups": []}},
        {"claims": {"cognito:groups": ["Users", "Analysts"]}},
        {"claims": {"cognito:groups": "Users"}},
        {"claims": {"cognito:groups": "admin"}},
        {"claims": {"cognito:groups": "SuperAdmins"}},
        {"username": "someone@example.com"},
    ],
    ids=[
        "null-identity",
        "empty-identity",
        "null-claims",
        "no-claims",
        "empty-group-list",
        "other-groups-as-list",
        "other-group-as-string",
        "group-name-differing-in-case",
        "group-name-containing-the-admin-group",
        "username-without-claims",
    ],
)
def test_a_caller_outside_the_admin_group_is_refused(
    cognito: FakeCognito, identity: dict | None
) -> None:
    """Every identity shape that does not name the Admin group is refused.

    The claim is a list for a caller in several groups and a bare string for a
    caller in one, so both spellings have to be understood before the membership
    test is meaningful: the string form has to become a one-element list, since
    a membership test against a bare string matches any group whose name merely
    contains the admin group's name.
    """
    event = {"info": {"fieldName": "listUsers"}, "identity": identity, "arguments": {}}

    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(event, None)

    assert cognito.names_called() == []


def test_an_event_with_no_identity_block_at_all_is_refused(cognito: FakeCognito) -> None:
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler({"info": {"fieldName": "listUsers"}, "arguments": {}}, None)

    assert cognito.names_called() == []


def test_the_groups_claim_is_honoured_when_it_arrives_as_a_bare_string(
    cognito: FakeCognito,
) -> None:
    """AppSync sends cognito:groups as a string when the caller is in one group."""
    event = {
        "info": {"fieldName": "listUsers"},
        "identity": {"claims": {"cognito:username": CALLER, "cognito:groups": ADMIN_GROUP}},
        "arguments": {},
    }

    assert index.handler(event, None) == {"users": []}


def test_each_operation_requires_admin_membership(cognito: FakeCognito) -> None:
    """The re-check covers the mutations too, not only the read."""
    for field, arguments in (
        ("listUsers", {}),
        ("createUser", create_input("new@example.com", "User")),
        ("deleteUser", delete_input("victim@example.com")),
    ):
        event = {
            "info": {"fieldName": field},
            "identity": {"claims": {"cognito:username": "plain@example.com"}},
            "arguments": arguments,
        }
        with pytest.raises(Exception, match="Unauthorized"):
            index.handler(event, None)

    assert cognito.names_called() == []


def test_an_unknown_field_name_is_rejected(cognito: FakeCognito) -> None:
    """A resolver wired to an unmodelled field fails loudly instead of silently."""
    with pytest.raises(ValueError, match="Unknown operation"):
        index.handler(admin_event("resetEverything"), None)

    assert cognito.names_called() == []


def test_the_caller_username_is_read_from_whichever_field_is_populated() -> None:
    """The self-deletion guard compares against this, so the order matters.

    Deployments differ in which of these AppSync populates; if all three were
    missing the guard would compare against the empty string and stop working.
    """
    identify = index._get_caller_identity

    assert identify({"identity": {"claims": {"cognito:username": "c@x", "sub": "s"}}})[
        "username"
    ] == "c@x"
    assert identify(
        {"identity": {"claims": {"sub": "s"}, "username": "u@x"}}
    )["username"] == "u@x"
    assert identify({"identity": {"claims": {"sub": "s"}}})["username"] == "s"
    assert identify({})["username"] == ""


# ---------------------------------------------------------------------------
# Deletion guard rails
# ---------------------------------------------------------------------------


def test_a_caller_cannot_delete_its_own_account(cognito: FakeCognito) -> None:
    cognito.admins = [CALLER, "other@example.com"]

    with pytest.raises(Exception, match="cannot delete your own account"):
        index.handler(admin_event("deleteUser", delete_input(CALLER)), None)

    assert "admin_delete_user" not in cognito.names_called()


def test_the_self_deletion_guard_ignores_surrounding_whitespace(cognito: FakeCognito) -> None:
    """The username is trimmed before it is compared, not after."""
    cognito.admins = [CALLER, "other@example.com"]

    with pytest.raises(Exception, match="cannot delete your own account"):
        index.handler(admin_event("deleteUser", delete_input(f"  {CALLER}  ")), None)

    assert "admin_delete_user" not in cognito.names_called()


def test_the_last_admin_cannot_be_deleted(cognito: FakeCognito) -> None:
    """With one Admin left, removing it would leave the pool unadministrable."""
    cognito.admins = ["sole@example.com"]

    with pytest.raises(Exception, match="last remaining Admin"):
        index.handler(admin_event("deleteUser", delete_input("sole@example.com")), None)

    assert "admin_delete_user" not in cognito.names_called()


def test_an_admin_is_deleted_when_another_admin_remains(cognito: FakeCognito) -> None:
    cognito.admins = ["kept@example.com", "going@example.com"]

    result = index.handler(admin_event("deleteUser", delete_input("going@example.com")), None)

    assert result == {"username": "going@example.com", "success": True}
    assert cognito.kwargs_for("admin_delete_user") == [
        {"UserPoolId": index.USER_POOL_ID, "Username": "going@example.com"}
    ]


def test_admins_are_counted_across_every_page(cognito: FakeCognito) -> None:
    """A paged count read as a single page would report a lone Admin.

    Cognito returns members of a group in pages, so a second Admin can sit on
    the second page while the first page holds only the deletion target.
    """
    cognito.admins = ["going@example.com", "kept@example.com"]
    cognito.admin_group_pages = [
        {"Users": [{"Username": "going@example.com"}]},
        {"Users": [{"Username": "kept@example.com"}]},
    ]

    result = index.handler(admin_event("deleteUser", delete_input("going@example.com")), None)

    assert result["success"] is True


def test_a_non_admin_is_deleted_even_when_only_one_admin_exists(cognito: FakeCognito) -> None:
    """The lock-out guard applies to Admins only; ordinary deletions proceed."""
    cognito.admins = [CALLER]

    result = index.handler(admin_event("deleteUser", delete_input("plain@example.com")), None)

    assert result == {"username": "plain@example.com", "success": True}
    # The Admin population is irrelevant here and must not even be counted.
    assert "paginate:list_users_in_group" not in cognito.names_called()


@pytest.mark.parametrize("username", ["", "   ", None], ids=["empty", "spaces", "absent"])
def test_deletion_requires_a_username(cognito: FakeCognito, username: str | None) -> None:
    arguments: dict = {"input": {}} if username is None else delete_input(username)

    with pytest.raises(Exception, match="username is required"):
        index.handler(admin_event("deleteUser", arguments), None)

    assert "admin_delete_user" not in cognito.names_called()


def test_deleting_an_unknown_user_reports_a_readable_message(cognito: FakeCognito) -> None:
    cognito.delete_error = UserNotFoundException("UserNotFoundException")

    with pytest.raises(Exception, match="ghost@example.com not found"):
        index.handler(admin_event("deleteUser", delete_input("ghost@example.com")), None)


def test_a_cognito_failure_during_deletion_is_surfaced(cognito: FakeCognito) -> None:
    """Anything other than a missing user propagates rather than reading as success."""
    cognito.delete_error = _CognitoError("TooManyRequestsException")

    with pytest.raises(ClientError):
        index.handler(admin_event("deleteUser", delete_input("plain@example.com")), None)


# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "no-at-sign",
        "missing@tld",
        "short@tld.c",
        "two@@example.com",
        "space in@example.com",
        "user@exam ple.com",
        "@example.com",
        "user@.com",
    ],
)
def test_rejects_a_malformed_email(cognito: FakeCognito, email: str) -> None:
    with pytest.raises(Exception, match="Invalid email format"):
        index.handler(admin_event("createUser", create_input(email, "User")), None)

    assert cognito.names_called() == []


@pytest.mark.parametrize(
    "email",
    ["user@example.com", "first.last+tag@sub.example.co.uk", "u_1%x-y@ex-ample.io"],
)
def test_accepts_a_well_formed_email(cognito: FakeCognito, email: str) -> None:
    cognito.get_user_result = cognito_user(email, email=email)

    assert index.handler(admin_event("createUser", create_input(email, "User")), None)["email"] == (
        email
    )


def test_rejects_an_email_outside_the_allowed_domains(
    cognito: FakeCognito, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", "example.com,corp.example.net")

    with pytest.raises(Exception, match="not allowed"):
        index.handler(admin_event("createUser", create_input("user@other.com", "User")), None)

    assert cognito.names_called() == []


def test_the_allowed_domain_comparison_ignores_case_and_spacing(
    cognito: FakeCognito, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list is operator-written configuration, so it is normalized, not matched raw."""
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", " Example.COM , corp.example.net ")
    cognito.get_user_result = cognito_user("User@EXAMPLE.com", email="User@EXAMPLE.com")

    result = index.handler(
        admin_event("createUser", create_input("User@EXAMPLE.com", "User")), None
    )

    assert result["username"] == "User@EXAMPLE.com"


def test_a_subdomain_of_an_allowed_domain_is_not_itself_allowed(
    cognito: FakeCognito, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The domain must equal an entry; a suffix match would widen the list."""
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", "example.com")

    with pytest.raises(Exception, match="not allowed"):
        index.handler(
            admin_event("createUser", create_input("user@notexample.com", "User")), None
        )


@pytest.mark.parametrize("configured", ["", "   ", " , "], ids=["unset", "spaces", "only-commas"])
def test_any_domain_is_accepted_when_no_allow_list_is_configured(
    cognito: FakeCognito, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """The allow list is optional; an unset one must not reject everything."""
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", configured)
    cognito.get_user_result = cognito_user("user@anywhere.test", email="user@anywhere.test")

    result = index.handler(
        admin_event("createUser", create_input("user@anywhere.test", "User")), None
    )

    assert result["username"] == "user@anywhere.test"


# ---------------------------------------------------------------------------
# Role handling on creation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role", ["", "   ", "admin", "ADMIN", "user", "SuperAdmin", "Owner", "Admin,User"]
)
def test_rejects_a_role_outside_the_two_known_roles(cognito: FakeCognito, role: str) -> None:
    """Only Admin and User exist; anything else would create a user with no role."""
    with pytest.raises(Exception, match="Invalid role"):
        index.handler(admin_event("createUser", create_input("new@example.com", role)), None)

    assert cognito.names_called() == []


def test_creating_an_admin_adds_the_admin_group(cognito: FakeCognito) -> None:
    cognito.get_user_result = cognito_user("boss@example.com", email="boss@example.com")

    result = index.handler(
        admin_event("createUser", create_input("boss@example.com", "Admin")), None
    )

    assert cognito.kwargs_for("admin_add_user_to_group") == [
        {
            "UserPoolId": index.USER_POOL_ID,
            "Username": "boss@example.com",
            "GroupName": ADMIN_GROUP,
        }
    ]
    assert result["role"] == "Admin"


def test_creating_a_plain_user_joins_no_group(cognito: FakeCognito) -> None:
    cognito.get_user_result = cognito_user("plain@example.com", email="plain@example.com")

    result = index.handler(
        admin_event("createUser", create_input("plain@example.com", "User")), None
    )

    assert "admin_add_user_to_group" not in cognito.names_called()
    assert result["role"] == "User"


def test_the_created_user_is_registered_with_a_verified_email_and_an_invitation(
    cognito: FakeCognito,
) -> None:
    """The email doubles as the username, and the invitation is what lets them in."""
    cognito.get_user_result = cognito_user("new@example.com", email="new@example.com")

    index.handler(admin_event("createUser", create_input("  new@example.com  ", "User")), None)

    assert cognito.kwargs_for("admin_create_user") == [
        {
            "UserPoolId": index.USER_POOL_ID,
            "Username": "new@example.com",
            "UserAttributes": [
                {"Name": "email", "Value": "new@example.com"},
                {"Name": "email_verified", "Value": "true"},
            ],
            "DesiredDeliveryMediums": ["EMAIL"],
        }
    ]


def test_creating_a_user_that_already_exists_reports_a_readable_message(
    cognito: FakeCognito,
) -> None:
    cognito.create_error = UsernameExistsException("UsernameExistsException")

    with pytest.raises(Exception, match="already exists"):
        index.handler(admin_event("createUser", create_input("taken@example.com", "User")), None)


def test_a_new_user_is_removed_again_when_the_admin_group_cannot_be_set(
    cognito: FakeCognito,
) -> None:
    """Otherwise the pool keeps a user whose role is not the one that was asked for."""
    cognito.add_to_group_error = _CognitoError("AccessDeniedException")

    with pytest.raises(ClientError):
        index.handler(admin_event("createUser", create_input("boss@example.com", "Admin")), None)

    assert cognito.kwargs_for("admin_delete_user") == [
        {"UserPoolId": index.USER_POOL_ID, "Username": "boss@example.com"}
    ]


def test_the_original_failure_is_reported_when_the_rollback_also_fails(
    cognito: FakeCognito,
) -> None:
    """A failed clean-up must not hide why the request failed."""
    cognito.add_to_group_error = _CognitoError("AccessDeniedException")
    cognito.delete_error = _CognitoError("TooManyRequestsException")

    with pytest.raises(ClientError) as raised:
        index.handler(admin_event("createUser", create_input("boss@example.com", "Admin")), None)

    assert raised.value.response["Error"]["Code"] == "AccessDeniedException"


# ---------------------------------------------------------------------------
# Listing and the GraphQL User shape
# ---------------------------------------------------------------------------


def test_list_users_maps_cognito_fields_onto_the_graphql_user_shape(cognito: FakeCognito) -> None:
    # The email attribute is deliberately not the username here: a pool can be
    # configured with opaque usernames, and the UI shows the email attribute.
    cognito.admins = ["boss@example.com"]
    cognito.user_pages = [
        {
            "Users": [
                cognito_user(
                    "boss@example.com",
                    email="boss.alias@example.com",
                    created=datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc),
                    status="CONFIRMED",
                )
            ]
        }
    ]

    assert index.handler(admin_event("listUsers"), None) == {
        "users": [
            {
                "username": "boss@example.com",
                "email": "boss.alias@example.com",
                "role": "Admin",
                "status": "CONFIRMED",
                "enabled": True,
                "createdAt": "2026-03-04T05:06:07.000Z",
            }
        ]
    }


def test_create_user_reads_the_attributes_key_that_admin_get_user_returns(
    cognito: FakeCognito,
) -> None:
    """AdminGetUser names the list UserAttributes where ListUsers names it Attributes.

    Reading only one of the two would report every freshly created user's email
    as its username instead.
    """
    cognito.get_user_result = {
        "Username": "new@example.com",
        "UserStatus": "FORCE_CHANGE_PASSWORD",
        "Enabled": True,
        "UserAttributes": [{"Name": "email", "Value": "preferred@example.com"}],
        "UserCreateDate": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    }

    assert index.handler(
        admin_event("createUser", create_input("new@example.com", "User")), None
    ) == {
        "username": "new@example.com",
        "email": "preferred@example.com",
        "role": "User",
        "status": "FORCE_CHANGE_PASSWORD",
        "enabled": True,
        "createdAt": "2026-01-02T03:04:05.000Z",
    }


def test_the_creation_timestamp_carries_a_timezone_designator(cognito: FakeCognito) -> None:
    """AppSync rejects an AWSDateTime without one, failing the whole query."""
    cognito.user_pages = [
        {
            "Users": [
                cognito_user(
                    "someone@example.com",
                    created=datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
                )
            ]
        }
    ]

    created = index.handler(admin_event("listUsers"), None)["users"][0]["createdAt"]

    assert created == "2026-05-06T07:08:09.000Z"


def test_a_user_without_a_creation_date_is_still_listed(cognito: FakeCognito) -> None:
    """A null date is a valid AWSDateTime; dropping the user is not."""
    cognito.user_pages = [{"Users": [cognito_user("someone@example.com")]}]

    users = index.handler(admin_event("listUsers"), None)["users"]

    assert len(users) == 1
    assert users[0]["createdAt"] is None


def test_the_username_stands_in_for_a_missing_email_attribute(cognito: FakeCognito) -> None:
    """The UI lists users by email, so the column must never come back empty."""
    cognito.user_pages = [{"Users": [cognito_user("someone@example.com")]}]

    assert index.handler(admin_event("listUsers"), None)["users"][0]["email"] == (
        "someone@example.com"
    )


def test_a_user_with_no_enabled_flag_is_reported_as_enabled(cognito: FakeCognito) -> None:
    cognito.user_pages = [{"Users": [cognito_user("someone@example.com", enabled=None)]}]

    assert index.handler(admin_event("listUsers"), None)["users"][0]["enabled"] is True


def test_a_disabled_user_is_reported_as_disabled(cognito: FakeCognito) -> None:
    cognito.user_pages = [{"Users": [cognito_user("someone@example.com", enabled=False)]}]

    assert index.handler(admin_event("listUsers"), None)["users"][0]["enabled"] is False


def test_listed_users_are_returned_newest_first(cognito: FakeCognito) -> None:
    cognito.user_pages = [
        {
            "Users": [
                cognito_user("old@example.com", created=datetime(2024, 1, 1, tzinfo=timezone.utc)),
                cognito_user("new@example.com", created=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                cognito_user("mid@example.com", created=datetime(2025, 1, 1, tzinfo=timezone.utc)),
            ]
        }
    ]

    users = index.handler(admin_event("listUsers"), None)["users"]

    assert [user["username"] for user in users] == [
        "new@example.com",
        "mid@example.com",
        "old@example.com",
    ]


def test_users_from_every_page_are_listed(cognito: FakeCognito) -> None:
    """A pool larger than one page must not be reported as truncated."""
    cognito.user_pages = [
        {"Users": [cognito_user("first@example.com")]},
        {"Users": [cognito_user("second@example.com")]},
    ]

    users = index.handler(admin_event("listUsers"), None)["users"]

    assert {user["username"] for user in users} == {"first@example.com", "second@example.com"}


def test_the_role_comes_from_admin_group_membership_not_from_any_group(
    cognito: FakeCognito,
) -> None:
    cognito.admins = ["boss@example.com"]
    cognito.user_pages = [
        {
            "Users": [
                cognito_user("boss@example.com"),
                cognito_user("plain@example.com"),
            ]
        }
    ]

    listed = index.handler(admin_event("listUsers"), None)["users"]
    roles = {user["username"]: user["role"] for user in listed}

    assert roles == {"boss@example.com": "Admin", "plain@example.com": "User"}


def test_a_role_lookup_failure_falls_back_to_the_lower_privilege_role(
    cognito: FakeCognito,
) -> None:
    """A throttled group lookup degrades the listing rather than failing it."""
    cognito.group_lookup_error = _CognitoError("TooManyRequestsException")
    cognito.user_pages = [{"Users": [cognito_user("someone@example.com")]}]

    users = index.handler(admin_event("listUsers"), None)["users"]

    assert users[0]["role"] == "User"


def test_a_cognito_failure_while_listing_is_surfaced(cognito: FakeCognito) -> None:
    """An empty list would read as an empty pool; the error has to reach the client."""
    cognito.list_users_error = _CognitoError("TooManyRequestsException")

    with pytest.raises(ClientError):
        index.handler(admin_event("listUsers"), None)


def test_every_cognito_call_names_the_configured_user_pool(cognito: FakeCognito) -> None:
    """A call without the pool id fails at runtime with an unhelpful error."""
    cognito.admins = ["kept@example.com", "going@example.com"]
    index.handler(admin_event("deleteUser", delete_input("going@example.com")), None)
    cognito.get_user_result = cognito_user("new@example.com", email="new@example.com")
    index.handler(admin_event("createUser", create_input("new@example.com", "Admin")), None)
    index.handler(admin_event("listUsers"), None)

    assert cognito.calls, "no Cognito calls were recorded"
    for name, kwargs in cognito.calls:
        assert kwargs.get("UserPoolId") == index.USER_POOL_ID, name
