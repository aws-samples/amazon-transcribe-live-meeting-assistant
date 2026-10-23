# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Reading values out of an AppSync GraphQL response.

A GraphQL response is not guaranteed to be a nested mapping with the field the
caller asked for. The schema declares most fields nullable, so a successful
response may carry JSON ``null`` where an object is expected; a response that
carries ``errors`` may carry no ``data`` at all; and a transport that hands back
something other than a mapping is possible too. All of those are responses, not
crashes, so the code that reads a field has to say what it wants done with each
of them instead of chaining attribute access and hoping.

These helpers make that choice explicit at the call site:

* :func:`optional_graphql_field` for a value the caller can carry on without. It
  returns ``default`` and logs the shape that came back.
* :func:`require_graphql_field` for a value the caller cannot proceed without.
  It raises :class:`GraphQLResponseError`, which fails the unit of work holding
  the field -- for a Kinesis consumer, the record -- so the work is redelivered.
  Once the event source mapping's retry budget is spent, the batch's shard and
  sequence-number range are described to its on-failure destination: the
  record's location, not its data, which stays readable from the stream until
  its retention period expires. Returning a default instead would finish the
  work with part of it missing and report success, which is the one outcome
  worth avoiding.

Both accept a ``logger``; pass the calling module's Powertools logger so the
line lands in that function's log stream with its request id.

Log hygiene
-----------
The descriptions built here name the *shape* of a response and nothing else: the
field names present, which of them are null, and the type or length of a value.
No value is ever included. A GraphQL error's ``message`` is left out for the same
reason -- the mutations in this pipeline inline their arguments into the query
document, so a server-side validation message can quote the argument it rejected,
and those arguments carry meeting content. ``errorType``, ``path`` and
``locations`` name the failure and the field it happened on without quoting
anything, and the message's length is reported in its place.
:func:`graphql_error_messages` exists for matching an error, and its result is
deliberately not part of any description.

A rendered Python traceback ends in the exception's own text, so the places that
log a failure at the pipeline's default level use :func:`traceback_frames`
instead: the chain of frames a traceback is read for, without the line it ends
in. That chain is what makes an unexpected response shape diagnosable, so it
goes wherever a traceback would have rather than being traded away -- for the
exception in hand, which is what these paths raise; a chained cause's own frames
are not followed.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

# Keys of a GraphQL error entry that describe the failure rather than the data
# that provoked it. Everything else in the entry is dropped before logging.
LOGGABLE_ERROR_KEYS: Tuple[str, ...] = ("errorType", "path", "locations")

# ``errorType`` values that name a problem with the request itself, which the
# same request will hit again. Anything else -- including an absent errorType --
# counts as worth retrying: the cost of retrying a permanent failure is a delay,
# while the cost of not retrying a transient one is a lost transcript segment.
PERMANENT_ERROR_TYPES = frozenset(
    {
        "BadRequest",
        "FieldUndefined",
        "InvalidSyntax",
        "MalformedHttpRequestException",
        "MissingFieldArgument",
        "Unauthorized",
        "UnknownArgument",
        "UnknownType",
        "UnsupportedOperation",
        "ValidationError",
        "WrongType",
    }
)

# How far into a value the shape description goes. Two levels name the field
# that is null and the object it sits in, which is what identifies the response;
# deeper than that the line grows without saying more.
_DESCRIBE_DEPTH = 2


class GraphQLResponseError(RuntimeError):
    """A GraphQL response did not carry a field the caller needs.

    Carries the shape description that was logged, so a handler further up can
    report it without re-deriving it, and :attr:`retryable` as classified by
    :func:`is_retryable_graphql_response`.
    """

    def __init__(
        self,
        path: Sequence[str],
        description: Dict[str, Any],
        retryable: bool,
    ) -> None:
        self.path: Tuple[str, ...] = tuple(path)
        self.description = description
        self.retryable = retryable
        super().__init__(
            f"AppSync response does not carry {'.'.join(self.path)} "
            f"(retryable: {retryable}, response shape: {description})"
        )


def traceback_frames(error: BaseException) -> List[str]:
    """An exception's traceback as ``file:line in function`` entries.

    Outermost first, the same order a rendered traceback prints them, so the
    chain identifies both the call that failed and where inside it. The frames
    and nothing else: a rendered traceback ends in the exception's own text,
    which for a GraphQL transport exception is its first error message, the thing
    the rest of this module keeps out of a log line. Source lines are left out
    too, so the entries stay one line each.

    The innermost frame on its own would not do. A ``TransportQueryError``
    re-raised by a bare ``raise`` keeps the frame gql first raised it in, and a
    :class:`GraphQLResponseError` keeps :func:`require_graphql_field` -- the same
    frame whichever call failed. It is the rest of the chain that names the call.

    This exception's own traceback only: a chained ``__cause__`` or
    ``__context__`` is not followed, so a wrapped exception's inner frames are
    not listed. An exception with no traceback, one built but never raised, gives
    ``[]``.
    """
    return [
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    ]


def response_data(response: Any) -> Any:
    """The ``data`` half of a GraphQL response, whichever form it arrives in.

    gql's ``session.execute`` returns the data mapping on its own, while a raw
    response and an ``ExecutionResult`` keep ``data`` and ``errors`` side by
    side. A mapping carrying either key is treated as the full envelope, so a
    response of ``errors`` with no ``data`` yields ``None`` rather than being
    mistaken for a data mapping whose only field is ``errors``. No field in this
    project's schema is named ``data`` or ``errors``, so the two forms do not
    overlap.
    """
    if isinstance(response, Mapping):
        if "data" in response or "errors" in response:
            return response.get("data")
        return response
    return getattr(response, "data", None)


def graphql_errors(response: Any) -> List[Dict[str, Any]]:
    """Shape-only summaries of the ``errors`` a response or error object carries.

    Accepts a response mapping, an ``ExecutionResult``, or a gql
    ``TransportQueryError`` -- anything with an ``errors`` attribute or key.
    Each summary keeps :data:`LOGGABLE_ERROR_KEYS` and replaces the message with
    its length, so the result is safe to log.
    """
    if isinstance(response, Mapping):
        raw = response.get("errors")
    else:
        raw = getattr(response, "errors", None)
    if not isinstance(raw, (list, tuple)):
        return []

    summaries: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            summary: Dict[str, Any] = {
                key: entry[key] for key in LOGGABLE_ERROR_KEYS if key in entry
            }
            message = entry.get("message")
            summary["messageLength"] = len(message) if isinstance(message, str) else 0
        else:
            # A GraphQLError object rather than the wire form.
            summary = {"errorType": type(entry).__name__}
        summaries.append(summary)
    return summaries


def graphql_error_messages(error: Any) -> List[str]:
    """The ``message`` strings a GraphQL error carries, for matching on.

    The counterpart to :func:`graphql_errors`: that one is for logging and omits
    the messages, this one returns them so a caller can recognise a specific
    server-side condition. Accepts a gql ``TransportQueryError`` (messages live
    under ``.errors``), a response mapping with an ``errors`` list, a single
    error entry with a ``message`` key, or anything else -- for which the result
    is empty rather than an exception, because this is called from inside an
    ``except`` block where a second failure would mask the first.
    """
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        return [error["message"]]

    if isinstance(error, Mapping):
        raw = error.get("errors")
    else:
        raw = getattr(error, "errors", None)
    if not isinstance(raw, (list, tuple)):
        return []

    messages = []
    for entry in raw:
        message = (
            entry.get("message") if isinstance(entry, Mapping) else getattr(entry, "message", None)
        )
        if isinstance(message, str):
            messages.append(message)
    return messages


def is_retryable_graphql_response(response: Any) -> bool:
    """Whether the response's errors look like something a redelivery can clear.

    True unless every error carries an ``errorType`` in
    :data:`PERMANENT_ERROR_TYPES`. A response with no errors at all -- a field
    that simply came back null -- is retryable on the same reasoning: the likely
    causes are ordering and eventual consistency, both of which pass.

    Informational, not a control. It can only answer from errors the caller
    actually holds, and a gql session configured the way this project's clients
    are raises ``TransportQueryError`` for any response carrying ``errors``
    before a caller sees the response at all -- so on that path this reports
    retryable for everything, which is also the behaviour the Kinesis consumer
    applies to every attributed failure. It becomes meaningful for a caller
    holding an ``ExecutionResult`` or a raw response envelope, and for reading an
    exception that kept its errors (see :func:`graphql_errors`).
    """
    error_types = {
        str(summary["errorType"])
        for summary in graphql_errors(response)
        if summary.get("errorType")
    }
    if not error_types:
        return True
    return not error_types.issubset(PERMANENT_ERROR_TYPES)


def describe_graphql_response(response: Any, path: Sequence[str] = ()) -> Dict[str, Any]:
    """A loggable description of a response: its shape, never its values.

    ``path``, when given, names the field the caller was looking for and is
    reported as ``missingField``.
    """
    description: Dict[str, Any] = {
        "responseType": type(response).__name__,
        "dataShape": describe_value(response_data(response)),
    }
    if path:
        description["missingField"] = ".".join(path)
    errors = graphql_errors(response)
    if errors:
        description["errors"] = errors
    return description


def describe_value(value: Any, depth: int = _DESCRIBE_DEPTH) -> str:
    """Name a value's shape without disclosing it.

    Mappings are rendered as their field names -- which come from the schema,
    not from the data -- with each field's own shape one level down. Strings,
    numbers and lists are reduced to a type name and, for a list, its length.
    """
    if value is None:
        return "null"
    if isinstance(value, Mapping):
        if depth <= 0:
            return f"object({len(value)} field{'' if len(value) == 1 else 's'})"
        # key=str so a mapping with keys of mixed types still renders: this runs
        # on a failure path, where raising would replace the description with a
        # second failure.
        rendered = ", ".join(
            f"{key}={describe_value(value[key], depth - 1)}" for key in sorted(value, key=str)
        )
        return f"object({rendered})"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _resolve(data: Any, path: Sequence[str]) -> Tuple[Any, Optional[Tuple[str, ...]]]:
    """Walk ``path`` through ``data``.

    Returns the value and ``None`` on success, or ``None`` and the prefix of
    ``path`` that could not be resolved. A field that is present but null counts
    as unresolved: a null object carries nothing the caller can read, which is
    the same position as the field being absent.
    """
    current = data
    for index, name in enumerate(path):
        if not isinstance(current, Mapping):
            return None, tuple(path[: index + 1])
        current = current.get(name)
        if current is None:
            return None, tuple(path[: index + 1])
    return current, None


def optional_graphql_field(
    response: Any,
    *path: str,
    logger: logging.Logger = LOGGER,
    default: Any = None,
) -> Any:
    """The value at ``path``, or ``default`` when the response does not carry it.

    For a field the caller can carry on without. A response that does not carry
    it is logged at warning level with its shape, and ``default`` is returned.
    """
    value, missing = _resolve(response_data(response), path)
    if missing is None:
        return value
    logger.warning(
        "AppSync response does not carry %s; continuing without it (response shape: %s)",
        ".".join(missing),
        describe_graphql_response(response, missing),
    )
    return default


def require_graphql_field(
    response: Any,
    *path: str,
    logger: logging.Logger = LOGGER,
) -> Any:
    """The value at ``path``, or :class:`GraphQLResponseError`.

    For a field the caller cannot proceed without. The response's shape is
    logged at error level together with the retryable classification from
    :func:`is_retryable_graphql_response` -- read that one's note on how far the
    classification can see -- and the exception carries both.
    """
    value, missing = _resolve(response_data(response), path)
    if missing is None:
        return value
    description = describe_graphql_response(response, missing)
    # Logged and attached to the exception, and read by neither: no caller
    # branches on it. It is there to tell an operator whether the failure looks
    # like one that will clear by itself, not to decide anything.
    retryable = is_retryable_graphql_response(response)
    logger.error(
        "AppSync response does not carry the required field %s (retryable: %s, response shape: %s)",
        ".".join(missing),
        retryable,
        description,
    )
    raise GraphQLResponseError(missing, description, retryable)
