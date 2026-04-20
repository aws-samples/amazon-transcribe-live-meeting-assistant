"""
Unit tests for search_meetings tool — specifically the model ARN builder.
Verifies that inference profile IDs get the correct ARN path.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("TRANSCRIPT_KB_ID", "test-kb-id")

import search_meetings

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


class TestExecuteResolvesArn(unittest.TestCase):
    """Verify execute() uses BEDROCK_MODEL_ID to build the right ARN."""

    def _run(self, env_overrides):
        """Helper: run execute with mocked boto3 and return the modelArn sent to KB."""
        with patch.dict(os.environ, env_overrides, clear=False):
            with patch.object(search_meetings, "boto3") as mock_boto3:
                mock_kb = MagicMock()
                mock_kb.retrieve_and_generate.return_value = {
                    "output": {"text": "ok"},
                    "citations": [],
                }
                mock_boto3.client.return_value = mock_kb
                search_meetings.execute(
                    query="test query", max_results=5, user_id="u1", is_admin=True
                )
                call_kwargs = mock_kb.retrieve_and_generate.call_args[1]
                return call_kwargs["retrieveAndGenerateConfiguration"][
                    "knowledgeBaseConfiguration"
                ]["modelArn"]

    def test_inference_profile_model_resolves_correctly(self):
        """The original bug — global.* model now produces correct ARN."""
        model_arn = self._run(
            {
                "BEDROCK_MODEL_ID": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                "AWS_REGION": "us-west-2",
                "AWS_ACCOUNT_ID": "123456789012",
            }
        )
        self.assertIn(":inference-profile/", model_arn)
        self.assertNotIn("foundation-model", model_arn)

    def test_foundation_model_resolves_correctly(self):
        """Plain foundation model still works."""
        model_arn = self._run(
            {
                "BEDROCK_MODEL_ID": "anthropic.claude-3-haiku-20240307-v1:0",
                "AWS_REGION": "us-west-2",
                "AWS_ACCOUNT_ID": "123456789012",
                # Clear legacy env var so it falls through to the new logic
                "MODEL_ARN": "",
            }
        )
        self.assertIn("::foundation-model/", model_arn)

    def test_legacy_model_arn_fallback(self):
        """Backward compat: MODEL_ARN env var still works if BEDROCK_MODEL_ID missing."""
        legacy_arn = "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-lite-v1:0"
        with patch.dict(
            os.environ,
            {"BEDROCK_MODEL_ID": "", "MODEL_ARN": legacy_arn},
            clear=False,
        ):
            with patch.object(search_meetings, "boto3") as mock_boto3:
                mock_kb = MagicMock()
                mock_kb.retrieve_and_generate.return_value = {
                    "output": {"text": "ok"},
                    "citations": [],
                }
                mock_boto3.client.return_value = mock_kb
                search_meetings.execute(query="q", user_id="u", is_admin=True)
                call_kwargs = mock_kb.retrieve_and_generate.call_args[1]
                self.assertEqual(
                    call_kwargs["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"][
                        "modelArn"
                    ],
                    legacy_arn,
                )

    def test_missing_model_raises(self):
        """When neither BEDROCK_MODEL_ID nor MODEL_ARN is set, raise a clear error."""
        with patch.dict(
            os.environ,
            {"BEDROCK_MODEL_ID": "", "MODEL_ARN": ""},
            clear=False,
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
