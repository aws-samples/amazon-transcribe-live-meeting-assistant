# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for stack operations."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from lma_sdk.exceptions import LMAResourceNotFoundError, LMAStackError
from lma_sdk.models.stack import StackStatusResult


class TestStackStatus:
    """Tests for stack.status()."""

    def test_status_returns_stack_info(self, client):
        """status() returns stack info when stack exists."""
        mock_cfn = MagicMock()
        client.session.client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "test-lma-stack",
                    "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/abc",
                    "StackStatus": "CREATE_COMPLETE",
                    "StackStatusReason": "",
                    "CreationTime": datetime(2025, 1, 1, tzinfo=timezone.utc),
                    "Outputs": [
                        {
                            "OutputKey": "CloudFrontEndpoint",
                            "OutputValue": "https://d123.cloudfront.net",
                            "Description": "UI endpoint",
                        }
                    ],
                    "Parameters": [
                        {"ParameterKey": "AdminEmail", "ParameterValue": "admin@example.com"}
                    ],
                    "Tags": [{"Key": "Project", "Value": "LMA"}],
                }
            ]
        }

        result = client.stack.status()

        assert result.success is True
        assert result.exists is True
        assert result.stack.stack_name == "test-lma-stack"
        assert result.stack.status == "CREATE_COMPLETE"
        assert len(result.stack.outputs) == 1
        assert result.stack.outputs[0].key == "CloudFrontEndpoint"
        assert result.stack.parameters["AdminEmail"] == "admin@example.com"
        assert result.stack.tags["Project"] == "LMA"

    def test_status_stack_not_found(self, client):
        """status() returns exists=False when stack doesn't exist."""
        mock_cfn = MagicMock()
        client.session.client.return_value = mock_cfn
        error_response = {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}
        mock_cfn.describe_stacks.side_effect = botocore.exceptions.ClientError(
            error_response, "DescribeStacks"
        )

        result = client.stack.status()

        assert result.success is True
        assert result.exists is False

    def test_status_no_stack_name(self, client_no_stack):
        """status() returns error when no stack name set."""
        result = client_no_stack.stack.status()
        assert result.success is False
        assert result.exists is False


class TestStackOutputs:
    """Tests for stack.outputs()."""

    def test_outputs_returns_dict(self, client):
        """outputs() returns dict keyed by output key."""
        mock_cfn = MagicMock()
        client.session.client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "test-lma-stack",
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": "Endpoint", "OutputValue": "https://example.com", "Description": "API"},
                        {"OutputKey": "BucketName", "OutputValue": "my-bucket", "Description": "S3"},
                    ],
                    "Parameters": [],
                    "Tags": [],
                }
            ]
        }

        outputs = client.stack.outputs()

        assert "Endpoint" in outputs
        assert outputs["Endpoint"].value == "https://example.com"
        assert "BucketName" in outputs

    def test_outputs_raises_when_not_found(self, client):
        """outputs() raises when stack doesn't exist."""
        mock_cfn = MagicMock()
        client.session.client.return_value = mock_cfn
        error_response = {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}
        mock_cfn.describe_stacks.side_effect = botocore.exceptions.ClientError(
            error_response, "DescribeStacks"
        )

        with pytest.raises(LMAResourceNotFoundError):
            client.stack.outputs()


class TestStackDeploy:
    """Tests for stack.deploy()."""

    def test_deploy_creates_new_stack(self, client):
        """deploy() creates stack when it doesn't exist."""
        mock_cfn = MagicMock()
        client.session.client.return_value = mock_cfn

        # First call: stack doesn't exist
        error_response = {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}
        mock_cfn.describe_stacks.side_effect = [
            botocore.exceptions.ClientError(error_response, "DescribeStacks"),
            # After create, status check returns complete
            {"Stacks": [{"StackName": "test-lma-stack", "StackId": "arn:id", "StackStatus": "CREATE_COMPLETE", "Outputs": [], "Parameters": [], "Tags": []}]},
        ]
        mock_cfn.get_waiter.return_value.wait.return_value = None

        result = client.stack.deploy(
            template_url="https://s3.amazonaws.com/bucket/template.yaml",
            wait=True,
        )

        mock_cfn.create_stack.assert_called_once()
        assert result.success is True

    def test_deploy_requires_template(self, client):
        """deploy() raises when no template provided."""
        mock_cfn = MagicMock()
        client.session.client.return_value = mock_cfn
        error_response = {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}
        mock_cfn.describe_stacks.side_effect = botocore.exceptions.ClientError(
            error_response, "DescribeStacks"
        )

        with pytest.raises(LMAStackError, match="template_url or template_file"):
            client.stack.deploy()


class TestStackDelete:
    """Tests for stack.delete()."""

    def test_delete_succeeds(self, client):
        """delete() calls delete_stack and waits."""
        mock_cfn = MagicMock()
        client.session.client.return_value = mock_cfn
        mock_cfn.get_waiter.return_value.wait.return_value = None

        result = client.stack.delete()

        mock_cfn.delete_stack.assert_called_once_with(StackName="test-lma-stack")
        assert result.success is True

    def test_delete_no_stack_name(self, client_no_stack):
        """delete() raises when no stack name set."""
        with pytest.raises(LMAStackError, match="No stack name"):
            client_no_stack.stack.delete()


class TestStackLogGroups:
    """Tests for stack.get_log_groups()."""

    def test_get_log_groups(self, client):
        """get_log_groups() returns list of log group names."""
        mock_logs = MagicMock()
        client.session.client.return_value = mock_logs
        mock_logs.get_paginator.return_value.paginate.return_value = [
            {"logGroups": [
                {"logGroupName": "/test-lma-stack/lambda/FetchTranscript"},
                {"logGroupName": "/test-lma-stack/lambda/ProcessTranscript"},
            ]}
        ]

        groups = client.stack.get_log_groups()

        assert len(groups) == 2
        assert "/test-lma-stack/lambda/FetchTranscript" in groups
