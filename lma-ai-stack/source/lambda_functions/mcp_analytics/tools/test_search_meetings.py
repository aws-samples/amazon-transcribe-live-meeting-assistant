"""
Unit tests for search_meetings tool — specifically the model ARN builder.
Verifies that inference profile IDs get the correct ARN path.
"""

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
        env = {**self.ENV}
        with (
            patch.dict(os.environ, env, clear=False),
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
