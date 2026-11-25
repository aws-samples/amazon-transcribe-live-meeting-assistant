"""
Unit tests for search_meetings tool — specifically the model ARN builder.
Verifies that inference profile IDs get the correct ARN path.
"""

import ast
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("TRANSCRIPT_KB_ID", "test-kb-id")

# The configuration layer the deployed function carries, for lma_config.
LAYER = Path(__file__).resolve().parents[3] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

import search_meetings  # noqa: E402
from lma_config import parameters  # noqa: E402

REGION = "us-west-2"
ACCOUNT = "123456789012"


class TestBuildModelArn(unittest.TestCase):
    """_build_model_arn correctly distinguishes foundation models from inference profiles."""

    def test_foundation_model_arn(self):
        """Plain foundation model ID uses the foundation-model ARN path."""
        arn = search_meetings._build_model_arn(
            "anthropic.claude-3-haiku-20240307-v1:0", REGION, ACCOUNT
        )
        self.assertEqual(
            arn,
            f"arn:aws:bedrock:{REGION}::foundation-model/anthropic.claude-3-haiku-20240307-v1:0",
        )
        # Foundation model ARNs omit the account ID segment
        self.assertNotIn(ACCOUNT, arn)

    def test_amazon_foundation_model_arn(self):
        """Amazon Nova (no inference-profile prefix) is a foundation model."""
        arn = search_meetings._build_model_arn("amazon.nova-lite-v1:0", REGION, ACCOUNT)
        self.assertIn("foundation-model/amazon.nova-lite-v1:0", arn)

    def test_us_inference_profile_arn(self):
        """'us.' prefix → inference-profile ARN."""
        arn = search_meetings._build_model_arn(
            "us.anthropic.claude-3-5-haiku-20241022-v1:0", REGION, ACCOUNT
        )
        self.assertEqual(
            arn,
            f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/"
            f"us.anthropic.claude-3-5-haiku-20241022-v1:0",
        )

    def test_global_inference_profile_arn(self):
        """'global.' prefix → inference-profile ARN (this was the bug)."""
        arn = search_meetings._build_model_arn(
            "global.anthropic.claude-haiku-4-5-20251001-v1:0", REGION, ACCOUNT
        )
        self.assertEqual(
            arn,
            f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/"
            f"global.anthropic.claude-haiku-4-5-20251001-v1:0",
        )

    def test_eu_inference_profile_arn(self):
        """'eu.' prefix → inference-profile ARN."""
        arn = search_meetings._build_model_arn(
            "eu.anthropic.claude-3-5-sonnet-20240620-v1:0", REGION, ACCOUNT
        )
        self.assertIn(":inference-profile/", arn)
        self.assertIn(ACCOUNT, arn)

    def test_apac_inference_profile_arn(self):
        """'apac.' prefix → inference-profile ARN."""
        arn = search_meetings._build_model_arn(
            "apac.anthropic.claude-3-5-sonnet-20240620-v1:0", REGION, ACCOUNT
        )
        self.assertIn(":inference-profile/", arn)

    def test_govcloud_arn_uses_the_govcloud_partition(self):
        """A GovCloud Region's ARN is in the aws-us-gov partition."""
        arn = search_meetings._build_model_arn(
            "us-gov.anthropic.claude-sonnet-4-5", "us-gov-west-1", ACCOUNT
        )
        self.assertTrue(arn.startswith("arn:aws-us-gov:bedrock:us-gov-west-1:"))


class TestExecuteResolvesArn(unittest.TestCase):
    """Verify execute() builds the right ARN for the configured model."""

    ENV = {"AWS_REGION": "us-west-2", "AWS_ACCOUNT_ID": "123456789012", "MODEL_ARN": ""}

    def _run(self, model_id, env_overrides=None):
        """Helper: run execute with mocked boto3 and return the modelArn sent to KB."""
        env = {**self.ENV, **(env_overrides or {})}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(search_meetings, "_configured_model_id", return_value=model_id),
            patch.object(search_meetings, "boto3") as mock_boto3,
        ):
            mock_kb = MagicMock()
            mock_kb.retrieve_and_generate.return_value = {
                "output": {"text": "ok"},
                "citations": [],
            }
            mock_boto3.client.return_value = mock_kb
            search_meetings.execute(query="test query", max_results=5, user_id="u1", is_admin=True)
            call_kwargs = mock_kb.retrieve_and_generate.call_args[1]
            return call_kwargs["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"][
                "modelArn"
            ]

    def test_generation_and_orchestration_prompts_are_sent(self):
        """Claude 5.5 models are refused by RetrieveAndGenerate without both prompts."""
        with (
            patch.dict(os.environ, self.ENV, clear=False),
            patch.object(
                search_meetings,
                "_configured_model_id",
                return_value="global.anthropic.claude-sonnet-5-5",
            ),
            patch.object(search_meetings, "boto3") as mock_boto3,
        ):
            mock_kb = MagicMock()
            mock_kb.retrieve_and_generate.return_value = {"output": {"text": "ok"}, "citations": []}
            mock_boto3.client.return_value = mock_kb
            search_meetings.execute(query="q", max_results=5, user_id="u1", is_admin=True)
            kb = mock_kb.retrieve_and_generate.call_args[1]["retrieveAndGenerateConfiguration"][
                "knowledgeBaseConfiguration"
            ]
        self.assertIn("generationConfiguration", kb)
        self.assertIn("orchestrationConfiguration", kb)

    def test_inference_profile_model_resolves_correctly(self):
        """A global.* model produces an inference-profile ARN."""
        model_arn = self._run("global.anthropic.claude-haiku-4-5-20251001-v1:0")
        self.assertIn(":inference-profile/", model_arn)
        self.assertNotIn("foundation-model", model_arn)

    def test_foundation_model_resolves_correctly(self):
        """Plain foundation model still works."""
        model_arn = self._run("anthropic.claude-3-haiku-20240307-v1:0")
        self.assertIn("::foundation-model/", model_arn)

    def test_legacy_model_arn_fallback(self):
        """A pre-built MODEL_ARN is used when no model is configured."""
        legacy_arn = "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-lite-v1:0"
        self.assertEqual(self._run("", {"MODEL_ARN": legacy_arn}), legacy_arn)

    def test_the_configured_model_is_read_from_the_section(self):
        """The Amazon Bedrock Models section, when it names a model, is used."""

        class _Store:
            def get_section(self, _name):
                return SimpleNamespace(values={"summaryModelId": "us.anthropic.claude-sonnet-4-6"})

        with patch.object(parameters, "_SHARED_STORE", _Store()):
            model_id = search_meetings._configured_model_id()
        self.assertEqual(model_id, "us.anthropic.claude-sonnet-4-6")

    def test_without_a_configured_model_the_shipped_default_is_used(self):
        class _Store:
            def get_section(self, _name):
                return SimpleNamespace(values={})

        with patch.object(parameters, "_SHARED_STORE", _Store()):
            model_id = search_meetings._configured_model_id()
        shipped = parameters.shipped_setting_defaults()["language_models"]["summaryModelId"]
        self.assertEqual(model_id, shipped)

    def test_missing_model_raises(self):
        """With no model and no MODEL_ARN, execute raises a clear error."""
        with (
            patch.dict(os.environ, {"MODEL_ARN": ""}, clear=False),
            patch.object(search_meetings, "_configured_model_id", return_value=""),
        ):
            with self.assertRaises(ValueError) as ctx:
                search_meetings.execute(query="q", user_id="u", is_admin=True)
            self.assertIn("Bedrock model not configured", str(ctx.exception))


class TestOwnerFilter(unittest.TestCase):
    """A non-Admin caller's search is limited to the documents their meetings wrote."""

    ENV = {"AWS_REGION": "us-west-2", "AWS_ACCOUNT_ID": "123456789012", "MODEL_ARN": ""}

    def _filter(self, is_admin, **dates):
        with (
            patch.dict(os.environ, self.ENV, clear=False),
            patch.object(
                search_meetings, "_configured_model_id", return_value="amazon.nova-lite-v1:0"
            ),
            patch.object(search_meetings, "boto3") as mock_boto3,
        ):
            mock_kb = MagicMock()
            mock_kb.retrieve_and_generate.return_value = {"output": {"text": "ok"}, "citations": []}
            mock_boto3.client.return_value = mock_kb
            search_meetings.execute(query="q", user_id="user-1", is_admin=is_admin, **dates)
            kb = mock_kb.retrieve_and_generate.call_args[1]["retrieveAndGenerateConfiguration"][
                "knowledgeBaseConfiguration"
            ]
        return kb["retrievalConfiguration"]["vectorSearchConfiguration"].get("filter")

    def test_non_admin_search_filters_on_the_owner_metadata_key(self):
        """One condition is sent bare: a RetrievalFilter's andAll takes two or more."""
        self.assertEqual(
            self._filter(is_admin=False),
            {"equals": {"key": "Owner", "value": "user-1"}},
        )

    def test_several_conditions_are_combined_with_and_all(self):
        conditions = self._filter(is_admin=False, start_date="2026-11-01")["andAll"]
        self.assertEqual(len(conditions), 2)
        self.assertIn({"equals": {"key": "Owner", "value": "user-1"}}, conditions)

    def test_admin_search_is_not_filtered_by_owner(self):
        self.assertIsNone(self._filter(is_admin=True))

    def test_owner_key_is_the_one_the_summary_step_writes(self):
        """Metadata keys are case-sensitive: hold the filter to getKBMetadata's key list."""
        summary = Path(__file__).resolve().parents[2] / "bedrock_summary_lambda" / "index.py"
        tree = ast.parse(summary.read_text())
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "getKBMetadata"
        )
        assignment = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "keys_to_include"
        )
        written = ast.literal_eval(assignment.value)
        self.assertIn(search_meetings.OWNER_METADATA_KEY, written)

    def test_date_key_is_the_one_the_summary_step_writes(self):
        """The date bounds filter on the numeric key getKBMetadata adds, by that name."""
        summary = Path(__file__).resolve().parents[2] / "bedrock_summary_lambda" / "index.py"
        tree = ast.parse(summary.read_text())
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "CREATED_AT_EPOCH_KEY"
        )
        self.assertEqual(
            ast.literal_eval(assignment.value), search_meetings.CREATED_AT_METADATA_KEY
        )

    def test_every_filtered_key_is_one_the_summary_step_writes(self):
        """No condition names a key that no knowledge base document carries."""
        conditions = self._filter(is_admin=False, start_date="2026-11-01", end_date="2026-11-30")[
            "andAll"
        ]
        keys = {next(iter(condition.values()))["key"] for condition in conditions}
        self.assertEqual(
            keys,
            {search_meetings.OWNER_METADATA_KEY, search_meetings.CREATED_AT_METADATA_KEY},
        )


class TestDateBounds(unittest.TestCase):
    """startDate and endDate become numeric bounds on the meeting's creation time."""

    ENV = {"AWS_REGION": "us-west-2", "AWS_ACCOUNT_ID": "123456789012", "MODEL_ARN": ""}

    def _conditions(self, **dates):
        with (
            patch.dict(os.environ, self.ENV, clear=False),
            patch.object(
                search_meetings, "_configured_model_id", return_value="amazon.nova-lite-v1:0"
            ),
            patch.object(search_meetings, "boto3") as mock_boto3,
        ):
            mock_kb = MagicMock()
            mock_kb.retrieve_and_generate.return_value = {"output": {"text": "ok"}, "citations": []}
            mock_boto3.client.return_value = mock_kb
            search_meetings.execute(query="q", is_admin=True, **dates)
            kb = mock_kb.retrieve_and_generate.call_args[1]["retrieveAndGenerateConfiguration"][
                "knowledgeBaseConfiguration"
            ]
        found = kb["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
        return found["andAll"] if "andAll" in found else [found]

    def test_a_start_date_alone_starts_at_midnight_utc(self):
        self.assertEqual(
            self._conditions(start_date="2026-11-01"),
            [{"greaterThanOrEquals": {"key": "CreatedAtEpochSeconds", "value": 1793491200}}],
        )

    def test_an_end_date_alone_includes_the_whole_day(self):
        self.assertEqual(
            self._conditions(end_date="2026-11-01"),
            [{"lessThanOrEquals": {"key": "CreatedAtEpochSeconds", "value": 1793577599}}],
        )

    def test_the_basic_and_week_date_forms_are_a_whole_day(self):
        """The basic and week forms are dates alone too, not midnight."""
        for value in ("20261101", "2026-W44-7"):
            self.assertEqual(
                self._conditions(end_date=value),
                [{"lessThanOrEquals": {"key": "CreatedAtEpochSeconds", "value": 1793577599}}],
                value,
            )

    def test_a_date_and_time_with_an_offset_is_that_instant(self):
        self.assertEqual(
            self._conditions(start_date="2026-11-01T09:00:00+01:00"),
            [{"greaterThanOrEquals": {"key": "CreatedAtEpochSeconds", "value": 1793520000}}],
        )

    def test_a_date_and_time_without_an_offset_is_read_as_utc(self):
        self.assertEqual(
            self._conditions(end_date="2026-11-01T08:00:00"),
            [{"lessThanOrEquals": {"key": "CreatedAtEpochSeconds", "value": 1793520000}}],
        )

    def test_both_bounds_are_combined(self):
        conditions = self._conditions(start_date="2026-11-01", end_date="2026-11-01")
        self.assertEqual(len(conditions), 2)

    def test_a_start_date_and_end_date_on_the_same_day_are_accepted(self):
        """An end given as a date alone covers its whole day, so a start that day is before it."""
        conditions = self._conditions(start_date="2026-11-01T09:00:00Z", end_date="2026-11-01")
        self.assertEqual(len(conditions), 2)

    def test_a_start_date_later_than_the_end_date_is_refused_with_both_named(self):
        """Without this the search ran and matched nothing, as if no meeting were found."""
        for start, end in (
            ("2026-11-30", "2026-11-01"),
            ("2026-11-01T10:00:00Z", "2026-11-01T09:00:00Z"),
            ("2026-11-02", "2026-11-01T23:00:00Z"),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError) as ctx:
                    self._conditions(start_date=start, end_date=end)
                message = str(ctx.exception)
                self.assertIn(f"startDate ({start}) is later than endDate ({end})", message)

    def test_a_reversed_range_is_refused_before_any_aws_call(self):
        """Also on a stack without a knowledge base or a model."""
        with (
            patch.dict(os.environ, {"TRANSCRIPT_KB_ID": "", "MODEL_ARN": ""}, clear=False),
            patch.object(search_meetings, "_configured_model_id") as model,
            patch.object(search_meetings, "boto3") as mock_boto3,
        ):
            with self.assertRaises(ValueError) as ctx:
                search_meetings.execute(query="q", start_date="2026-11-30", end_date="2026-11-01")
        self.assertIn("is later than endDate", str(ctx.exception))
        model.assert_not_called()
        mock_boto3.client.assert_not_called()

    def test_a_value_that_is_not_a_date_is_refused_with_the_argument_named(self):
        with self.assertRaises(ValueError) as ctx:
            self._conditions(start_date="last week")
        self.assertIn("startDate must be an ISO 8601 date", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self._conditions(end_date="2026-13-01")
        self.assertIn("endDate must be an ISO 8601 date", str(ctx.exception))

    def test_a_date_that_is_not_one_is_refused_before_any_other_check(self):
        """Also on a stack without a knowledge base or a model, and with no AWS call."""
        with (
            patch.dict(os.environ, {"TRANSCRIPT_KB_ID": "", "MODEL_ARN": ""}, clear=False),
            patch.object(search_meetings, "_configured_model_id") as model,
            patch.object(search_meetings, "boto3") as mock_boto3,
        ):
            with self.assertRaises(ValueError) as ctx:
                search_meetings.execute(query="q", start_date="2026-305")
        self.assertIn("startDate must be an ISO 8601 date", str(ctx.exception))
        model.assert_not_called()
        mock_boto3.client.assert_not_called()


class TestInputValidation(unittest.TestCase):
    """Basic input validation."""

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError) as ctx:
            search_meetings.execute(query="")
        self.assertIn("Query is required", str(ctx.exception))

    def test_missing_kb_id_raises(self):
        with patch.dict(os.environ, {"TRANSCRIPT_KB_ID": ""}, clear=False):
            with self.assertRaises(ValueError) as ctx:
                search_meetings.execute(query="q")
            self.assertIn("Knowledge Base not configured", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
