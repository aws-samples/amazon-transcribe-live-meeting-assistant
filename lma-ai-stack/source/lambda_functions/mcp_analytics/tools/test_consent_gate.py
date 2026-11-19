# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The consent gate: the capture clients' rule applied to a meeting row.

The rule itself is the configuration layer's (``lma_consent.rules``) and is run
for real here. What these tests hold is the gate around it: the deployment's
mode is read only when the meeting's own record does not decide, a deployment
without the table has the shipped defaults, and everything else that stops the
gate from deciding refuses the request with one fixed message.
"""

import logging
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
# The configuration layer the deployed function carries, for lma_config and lma_consent.
LAYER = HERE.parents[2] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

import lma_config  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402
from lma_config import (  # noqa: E402
    ConfigurationError,
    ConfigurationTableNotConfigured,
    codec,
    keys,
)

from tools import consent_gate  # noqa: E402

SECRET_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/secret-config-table"


def _objection(name="Ana Silva"):
    return {"ParticipantName": name, "Decision": "objection"}


def _consent(name="Ana Silva"):
    return {"ParticipantName": name, "Decision": "consent"}


def _meeting(record="absent"):
    meeting = {"PK": "c#call-1", "SK": "c#call-1", "Owner": "owner@example.com"}
    if record != "absent":
        meeting["Consent"] = record
    return meeting


class FakeStore:
    """A configuration store that counts how it is used.

    ``values`` is what the meeting_consent section holds; ``error`` is raised by
    ``get_section`` when set.
    """

    created = 0
    reads = 0
    values: dict = {}
    error = None

    def __init__(self):
        type(self).created += 1

    def get_section(self, name):
        type(self).reads += 1
        assert name == "meeting_consent"
        if type(self).error is not None:
            raise type(self).error
        return SimpleNamespace(values=dict(type(self).values))

    @classmethod
    def reset(cls, values=None, error=None):
        cls.created = 0
        cls.reads = 0
        cls.values = values or {}
        cls.error = error


class GateTestCase(unittest.TestCase):
    """Starts each test with no store, and with the fake store in the layer's place."""

    def setUp(self):
        FakeStore.reset()
        self._saved_store = consent_gate._STORE
        consent_gate._STORE = None
        patcher = patch.object(lma_config, "ConfigurationStore", FakeStore)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, consent_gate, "_STORE", self._saved_store)

    def deployment(self, mode):
        FakeStore.reset(values={"mode": mode})

    def assertUnavailable(self, call):
        with self.assertRaises(consent_gate.ConsentSettingsUnavailable) as caught:
            call()
        self.assertEqual(str(caught.exception), consent_gate.SETTINGS_UNAVAILABLE_MESSAGE)
        self.assertEqual(
            str(caught.exception), "The meeting consent settings could not be read; try again"
        )
        # The cause stays in the log: it is neither chained nor in the text.
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("secret", repr(caught.exception))
        self.assertNotIn("arn:", repr(caught.exception))
        return caught.exception


class RecordWithAModeTest(GateTestCase):
    """A record that names its mode decides alone, whatever the deployment's mode."""

    def test_off_is_allowed_in_every_deployment_mode(self):
        for mode in ("off", "notify", "opt_in"):
            with self.subTest(deployment=mode):
                self.assertTrue(consent_gate.capture_allowed(_meeting({"Mode": "off"}), mode))

    def test_notify_without_a_notice_or_objection_is_allowed(self):
        self.assertTrue(consent_gate.capture_allowed(_meeting({"Mode": "notify"}), "opt_in"))

    def test_notify_with_the_notice_posted_is_allowed(self):
        record = {"Mode": "notify", "NoticePostedAt": "2026-11-19T10:00:00Z"}
        self.assertTrue(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_notify_with_an_objection_is_refused(self):
        record = {"Mode": "notify", "Events": [_objection()]}
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_notify_objection_withdrawn_by_a_later_consent_is_allowed(self):
        record = {"Mode": "notify", "Events": [_objection(), _consent("ana  SILVA")]}
        self.assertTrue(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_notify_consent_followed_by_an_objection_is_refused(self):
        record = {"Mode": "notify", "Events": [_consent(), _objection()]}
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_one_objector_among_consenting_participants_refuses(self):
        record = {
            "Mode": "notify",
            "Events": [_consent("Bo Lee"), _objection("Ana Silva"), _consent("Cy Ng")],
        }
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_opt_in_each_participant_with_nobody_listed_is_refused(self):
        record = {"Mode": "opt_in", "Rule": "each_participant"}
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_opt_in_each_participant_with_a_participant_waiting_is_refused(self):
        record = {
            "Mode": "opt_in",
            "Rule": "each_participant",
            "Participants": ["Ana Silva", "Bo Lee"],
            "Events": [_consent("Ana Silva")],
        }
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_opt_in_each_participant_with_everyone_consented_is_allowed(self):
        record = {
            "Mode": "opt_in",
            "Rule": "each_participant",
            "Participants": ["Ana Silva", "Bo Lee"],
            "Events": [_consent("Ana Silva"), _consent("Bo Lee")],
        }
        self.assertTrue(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_opt_in_each_participant_with_an_objection_is_refused(self):
        record = {
            "Mode": "opt_in",
            "Rule": "each_participant",
            "Participants": ["Ana Silva", "Bo Lee"],
            "Events": [_consent("Ana Silva"), _consent("Bo Lee"), _objection("Bo Lee")],
        }
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_opt_in_owner_attests_waits_for_the_attestation(self):
        record = {"Mode": "opt_in", "Rule": "owner_attests"}
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))
        record["Attestation"] = {"By": "owner@example.com"}
        self.assertTrue(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_an_attestation_does_not_outweigh_an_objection(self):
        record = {
            "Mode": "opt_in",
            "Rule": "owner_attests",
            "Attestation": {"By": "owner@example.com"},
            "Events": [_objection()],
        }
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_a_mode_the_rules_do_not_know_is_refused(self):
        self.assertFalse(consent_gate.capture_allowed(_meeting({"Mode": "anything"}), "off"))

    def test_the_stored_capture_allowed_flag_is_not_what_decides(self):
        record = {"Mode": "opt_in", "Rule": "owner_attests", "CaptureAllowed": True}
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))


class RecordWithoutAModeTest(GateTestCase):
    """No record, or one with no mode: the deployment's mode decides."""

    def test_only_opt_in_withholds_a_meeting_with_no_record(self):
        for mode, expected in (("off", True), ("notify", True), ("opt_in", False)):
            for meeting in (_meeting(), _meeting(None), _meeting({}), _meeting({"Mode": ""})):
                with self.subTest(deployment=mode, consent=meeting.get("Consent", "missing")):
                    self.assertEqual(consent_gate.capture_allowed(meeting, mode), expected)

    def test_a_record_with_events_but_no_mode_is_judged_by_the_deployment(self):
        record = {"Events": [_objection()]}
        self.assertTrue(consent_gate.capture_allowed(_meeting(record), "notify"))
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "opt_in"))

    def test_the_mode_is_read_from_the_settings_when_not_given(self):
        for mode, expected in (("off", True), ("notify", True), ("opt_in", False)):
            with self.subTest(mode=mode):
                self.deployment(mode)
                consent_gate._STORE = None
                self.assertEqual(consent_gate.capture_allowed(_meeting()), expected)

    def test_a_section_with_no_mode_has_the_shipped_default(self):
        FakeStore.reset(values={})
        self.assertTrue(consent_gate.capture_allowed(_meeting()))
        self.assertEqual(consent_gate.deployment_consent_mode(), "off")

    def test_a_stored_mode_the_rules_do_not_know_falls_back_to_the_default(self):
        FakeStore.reset(values={"mode": "strictest"})
        self.assertEqual(consent_gate.deployment_consent_mode(), "off")

    def test_a_mode_of_the_wrong_type_falls_back_to_the_default(self):
        FakeStore.reset(values={"mode": ["opt_in"]})
        self.assertEqual(consent_gate.deployment_consent_mode(), "off")

    def test_a_mode_given_by_the_caller_is_not_read_again(self):
        self.deployment("opt_in")
        self.assertTrue(consent_gate.capture_allowed(_meeting(), "off"))
        self.assertEqual((FakeStore.created, FakeStore.reads), (0, 0))

    def test_a_mode_given_by_the_caller_that_the_rules_do_not_know_is_refused(self):
        for mode in ("OPT_IN", "", "strict", 1):
            with self.subTest(mode=mode):
                self.assertUnavailable(lambda: consent_gate.capture_allowed(_meeting(), mode))


class SettingsReadOnlyWhenNeededTest(GateTestCase):
    """The settings are read only for a meeting whose own record names no mode."""

    def test_a_record_that_decides_never_reads_the_settings(self):
        FakeStore.reset(error=ClientError({"Error": {"Code": "Boom"}}, "GetItem"))
        for record in (
            {"Mode": "off"},
            {"Mode": "notify"},
            {"Mode": "opt_in", "Rule": "owner_attests"},
        ):
            with self.subTest(record=record):
                consent_gate.capture_allowed(_meeting(record))
        self.assertEqual((FakeStore.created, FakeStore.reads), (0, 0))

    def test_a_settings_failure_does_not_block_a_meeting_whose_record_decides(self):
        FakeStore.reset(error=RuntimeError("table unreachable"))
        self.assertTrue(consent_gate.capture_allowed(_meeting({"Mode": "off"})))
        self.assertFalse(consent_gate.capture_allowed(_meeting({"Mode": "opt_in"})))
        consent_gate.require_capture_allowed(_meeting({"Mode": "notify"}))

    def test_a_missing_configuration_layer_does_not_block_a_record_that_decides(self):
        with patch.dict(sys.modules, {"lma_config": None}):
            self.assertTrue(consent_gate.capture_allowed(_meeting({"Mode": "off"})))

    def test_a_meeting_with_no_mode_reads_the_settings(self):
        self.deployment("off")
        for meeting in (_meeting(), _meeting(None), _meeting({}), _meeting({"Events": []})):
            consent_gate.capture_allowed(meeting)
        self.assertEqual(FakeStore.reads, 4)

    def test_one_store_serves_the_whole_process(self):
        self.deployment("notify")
        for _ in range(5):
            consent_gate.capture_allowed(_meeting())
            consent_gate.deployment_consent_mode()
        self.assertEqual(FakeStore.created, 1)
        self.assertEqual(FakeStore.reads, 10)

    def test_a_store_that_could_not_be_built_is_built_again_on_the_next_call(self):
        calls = []

        def build():
            calls.append(1)
            if len(calls) == 1:
                raise EndpointConnectionError(endpoint_url="https://example.invalid")
            return FakeStore()

        with patch.object(lma_config, "ConfigurationStore", build):
            self.assertUnavailable(consent_gate.deployment_consent_mode)
            self.assertEqual(consent_gate.deployment_consent_mode(), "off")
            self.assertEqual(consent_gate.deployment_consent_mode(), "off")
        self.assertEqual(len(calls), 2)


class SettingsFailureTest(GateTestCase):
    """Whatever stops the settings being read refuses the request, the same way."""

    def refused_with(self, error):
        FakeStore.reset(error=error)
        return self.assertUnavailable(lambda: consent_gate.capture_allowed(_meeting()))

    def test_a_client_error(self):
        error = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": f"denied on {SECRET_ARN}"}},
            "GetItem",
        )
        self.refused_with(error)

    def test_a_botocore_error(self):
        self.refused_with(EndpointConnectionError(endpoint_url="https://secret.invalid"))

    def test_a_configuration_error(self):
        self.refused_with(ConfigurationError(f"broken at {SECRET_ARN}"))

    def test_a_corrupt_body(self):
        self.refused_with(ValueError("a configuration body must be a JSON object"))

    def test_any_other_error(self):
        self.refused_with(RuntimeError(SECRET_ARN))
        self.refused_with(KeyError("meeting_consent"))
        self.refused_with(AttributeError("values"))

    def test_the_kind_of_the_cause_is_logged_and_its_text_is_not(self):
        # The text of a failed read names the table and the role: it is neither
        # returned nor logged, and the traceback that would carry it is not written.
        with self.assertLogs(level=logging.ERROR) as logged:
            self.refused_with(RuntimeError(SECRET_ARN))
        written = "\n".join(logged.output)
        self.assertIn("RuntimeError", written)
        self.assertNotIn(SECRET_ARN, written)
        self.assertNotIn("Traceback", written)

    def test_a_record_that_cannot_be_judged_logs_the_kind_of_the_cause_only(self):
        with (
            patch.object(consent_gate, "_known_mode", side_effect=RuntimeError(SECRET_ARN)),
            self.assertLogs(level=logging.ERROR) as logged,
        ):
            self.assertUnavailable(lambda: consent_gate.capture_allowed(_meeting({"Mode": "off"})))
        written = "\n".join(logged.output)
        self.assertIn("RuntimeError", written)
        self.assertNotIn(SECRET_ARN, written)

    def test_a_missing_configuration_layer(self):
        with patch.dict(sys.modules, {"lma_config": None}):
            self.assertUnavailable(lambda: consent_gate.capture_allowed(_meeting()))
            self.assertUnavailable(consent_gate.deployment_consent_mode)

    def test_a_missing_consent_rules_module(self):
        with patch.dict(sys.modules, {"lma_consent": None}):
            self.assertUnavailable(lambda: consent_gate.capture_allowed(_meeting({"Mode": "off"})))
            self.assertUnavailable(consent_gate.deployment_consent_mode)

    def test_a_consent_value_that_is_not_a_mapping(self):
        for record in ("opt_in", "", ["Mode"], 0, 7, True, b"{}", {"Mode"}):
            with self.subTest(record=record):
                self.assertUnavailable(
                    lambda: consent_gate.capture_allowed(_meeting(record), "off")
                )
        self.assertEqual(FakeStore.reads, 0)

    def test_a_meeting_row_that_is_not_a_mapping(self):
        for meeting in (None, "c#call-1", ["Consent"], 3):
            with self.subTest(meeting=meeting):
                self.assertUnavailable(lambda: consent_gate.capture_allowed(meeting, "off"))

    def test_a_record_the_rules_cannot_read(self):
        for record in (
            {"Mode": "notify", "Events": 5},
            {"Mode": "notify", "Events": [7]},
            {"Mode": "opt_in", "Rule": "each_participant", "Participants": 5},
            {"Mode": "opt_in", "Rule": "each_participant", "Events": [None]},
        ):
            with self.subTest(record=record):
                self.assertUnavailable(
                    lambda: consent_gate.capture_allowed(_meeting(record), "off")
                )

    def test_participants_that_are_not_names_do_not_count_as_consent(self):
        record = {"Mode": "opt_in", "Rule": "each_participant", "Participants": [["x"], None]}
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))

    def test_a_failure_is_never_an_allowance(self):
        FakeStore.reset(error=RuntimeError("down"))
        with self.assertRaises(PermissionError):
            consent_gate.require_capture_allowed(_meeting())
        with self.assertRaises(PermissionError):
            consent_gate.require_capture_allowed(_meeting("opt_in"))

    def test_it_is_a_permission_error(self):
        self.assertTrue(issubclass(consent_gate.ConsentSettingsUnavailable, PermissionError))


class NoConfigurationTableTest(unittest.TestCase):
    """A deployment without the table name has the shipped defaults: consent off."""

    def setUp(self):
        self._saved_store = consent_gate._STORE
        consent_gate._STORE = None
        self.addCleanup(setattr, consent_gate, "_STORE", self._saved_store)

    def test_the_real_store_without_a_table_name_means_off(self):
        env = {key: value for key, value in os.environ.items() if key != "CONFIGURATION_TABLE_NAME"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(consent_gate.deployment_consent_mode(), "off")
            self.assertTrue(consent_gate.capture_allowed(_meeting()))
            consent_gate.require_capture_allowed(_meeting(None))

    def test_the_error_the_store_raises_is_the_one_that_means_off(self):
        def unset():
            raise ConfigurationTableNotConfigured("unset")

        with patch.object(lma_config, "ConfigurationStore", unset):
            self.assertEqual(consent_gate.deployment_consent_mode(), "off")

    def test_a_record_that_decides_still_decides_without_a_table(self):
        env = {key: value for key, value in os.environ.items() if key != "CONFIGURATION_TABLE_NAME"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(consent_gate.capture_allowed(_meeting({"Mode": "opt_in"})))
            self.assertTrue(consent_gate.capture_allowed(_meeting({"Mode": "notify"})))


class FakeDynamo:
    """``get_item`` over a dict of stored configuration items."""

    def __init__(self, items=None, error=None):
        self.items = items or {}
        self.error = error
        self.requests = []

    def get_item(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        item = self.items.get(request["Key"][codec.KEY_ATTRIBUTE]["S"])
        return {"Item": item} if item is not None else {}


def _stored(key, body):
    return {
        codec.KEY_ATTRIBUTE: {"S": key},
        codec.BODY_ATTRIBUTE: {"B": codec.encode_body(body)},
        "Version": {"N": "1"},
    }


class WithTheRealStoreTest(unittest.TestCase):
    """The deployment's mode read through the layer's own store, over a fake table."""

    def setUp(self):
        self._saved_store = consent_gate._STORE
        consent_gate._STORE = None
        self.addCleanup(setattr, consent_gate, "_STORE", self._saved_store)
        env = patch.dict(os.environ, {"CONFIGURATION_TABLE_NAME": "config-table"})
        env.start()
        self.addCleanup(env.stop)

    def mode_with(self, dynamo):
        with patch("boto3.client", return_value=dynamo):
            return consent_gate.deployment_consent_mode()

    def baseline(self, section):
        return FakeDynamo(
            {keys.DEFAULT_KEY: _stored(keys.DEFAULT_KEY, {"meeting_consent": section})}
        )

    def test_each_stored_mode_is_read(self):
        for mode in ("off", "notify", "opt_in"):
            with self.subTest(mode=mode):
                consent_gate._STORE = None
                self.assertEqual(self.mode_with(self.baseline({"mode": mode})), mode)

    def test_a_section_that_holds_nothing_is_off(self):
        self.assertEqual(self.mode_with(self.baseline({})), "off")
        self.assertEqual(self.mode_with(FakeDynamo()), "off")

    def test_an_unknown_stored_mode_is_off(self):
        self.assertEqual(self.mode_with(self.baseline({"mode": "strictest"})), "off")

    def test_the_gate_withholds_a_meeting_with_no_record_in_an_opt_in_deployment(self):
        with patch("boto3.client", return_value=self.baseline({"mode": "opt_in"})):
            self.assertFalse(consent_gate.capture_allowed(_meeting()))
            with self.assertRaises(PermissionError) as caught:
                consent_gate.require_capture_allowed(_meeting({}))
        self.assertEqual(str(caught.exception), consent_gate.CAPTURE_NOT_ALLOWED_MESSAGE)

    def test_a_table_that_cannot_be_read_refuses(self):
        error = ClientError({"Error": {"Code": "ThrottlingException"}}, "GetItem")
        with patch("boto3.client", return_value=FakeDynamo(error=error)):
            with self.assertRaises(consent_gate.ConsentSettingsUnavailable):
                consent_gate.capture_allowed(_meeting())

    def test_a_body_that_is_not_readable_refuses(self):
        corrupt = {
            codec.KEY_ATTRIBUTE: {"S": keys.DEFAULT_KEY},
            codec.BODY_ATTRIBUTE: {"B": b"not gzip at all"},
        }
        with patch("boto3.client", return_value=FakeDynamo({keys.DEFAULT_KEY: corrupt})):
            with self.assertRaises(consent_gate.ConsentSettingsUnavailable):
                consent_gate.deployment_consent_mode()

    def test_the_section_is_read_with_a_consistent_read_each_time(self):
        dynamo = self.baseline({"mode": "notify"})
        with patch("boto3.client", return_value=dynamo):
            consent_gate.deployment_consent_mode()
            first = len(dynamo.requests)
            consent_gate.deployment_consent_mode()
        self.assertGreater(len(dynamo.requests), first)
        self.assertTrue(all(request["ConsistentRead"] for request in dynamo.requests))


class RequireCaptureAllowedTest(GateTestCase):
    def test_returns_nothing_when_capture_is_allowed(self):
        self.assertIsNone(consent_gate.require_capture_allowed(_meeting({"Mode": "off"})))

    def test_raises_the_fixed_sentence_when_it_is_not(self):
        record = {"Mode": "notify", "Events": [_objection()]}
        with self.assertRaises(PermissionError) as caught:
            consent_gate.require_capture_allowed(_meeting(record))
        self.assertNotIsInstance(caught.exception, consent_gate.ConsentSettingsUnavailable)
        self.assertEqual(
            str(caught.exception),
            "Capture is not allowed for this meeting under its consent settings, "
            "so its content is not available",
        )

    def test_the_sentence_names_neither_the_meeting_nor_the_participants(self):
        record = {"Mode": "notify", "Events": [_objection("Zed Quill")]}
        meeting = _meeting(record)
        with self.assertRaises(PermissionError) as caught:
            consent_gate.require_capture_allowed(meeting)
        for text in ("call-1", "Zed", "owner@example.com", "notify"):
            self.assertNotIn(text, str(caught.exception))

    def test_an_opt_in_deployment_withholds_a_meeting_with_no_record(self):
        self.deployment("opt_in")
        with self.assertRaises(PermissionError):
            consent_gate.require_capture_allowed(_meeting())

    def test_the_meeting_row_is_not_changed(self):
        record = {"Mode": "notify", "Events": [_objection()], "Participants": ["Ana Silva"]}
        meeting = _meeting(record)
        before = repr(meeting)
        consent_gate.capture_allowed(meeting, "off")
        self.assertEqual(repr(meeting), before)

    def test_a_record_read_back_from_dynamodb_with_decimals_is_judged(self):
        record = {
            "Mode": "notify",
            "Version": Decimal("3"),
            "Events": [{**_objection(), "Confidence": Decimal("0.9")}],
        }
        self.assertFalse(consent_gate.capture_allowed(_meeting(record), "off"))


if __name__ == "__main__":
    unittest.main()
