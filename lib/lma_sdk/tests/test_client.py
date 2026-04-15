# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for LMAClient."""

import os
from unittest.mock import MagicMock, patch

import pytest

from lma_sdk import LMAClient
from lma_sdk.exceptions import LMAConfigurationError
from lma_sdk.operations.publish import PublishOperations
from lma_sdk.operations.stack import StackOperations


class TestLMAClientInit:
    """Tests for LMAClient initialization."""

    def test_default_init(self):
        """Client initializes with defaults from env or hardcoded."""
        with patch.dict(os.environ, {}, clear=True):
            client = LMAClient()
            assert client.stack_name == "LMA"
            # Region comes from AWS CLI profile or falls back to us-east-1
            assert client.region is not None

    def test_explicit_params(self, mock_session):
        """Client uses explicit params over defaults."""
        mock_session.region_name = "eu-west-1"
        client = LMAClient(
            stack_name="MyStack",
            region="eu-west-1",
            session=mock_session,
        )
        assert client.stack_name == "MyStack"
        assert client.region == "eu-west-1"
        assert client.session is mock_session

    def test_env_vars(self):
        """Client reads from environment variables."""
        with patch.dict(os.environ, {
            "LMA_STACK_NAME": "EnvStack",
            "AWS_DEFAULT_REGION": "ap-southeast-1",
        }):
            client = LMAClient()
            assert client.stack_name == "EnvStack"
            assert client.region == "ap-southeast-1"

    def test_repr(self, client):
        """Client has useful repr."""
        r = repr(client)
        assert "test-lma-stack" in r
        assert "us-east-1" in r

    def test_require_stack_raises_when_empty(self, client_no_stack):
        """_require_stack raises when no stack name set."""
        with pytest.raises(LMAConfigurationError):
            client_no_stack._require_stack()

    def test_require_stack_returns_name(self, client):
        """_require_stack returns the stack name."""
        assert client._require_stack() == "test-lma-stack"


class TestLMAClientOperations:
    """Tests for lazy-loaded operation namespaces."""

    def test_stack_operations_lazy_loaded(self, client):
        """stack property returns StackOperations."""
        ops = client.stack
        assert isinstance(ops, StackOperations)
        # Same instance on second access
        assert client.stack is ops

    def test_publish_operations_lazy_loaded(self, client):
        """publish property returns PublishOperations."""
        ops = client.publish
        assert isinstance(ops, PublishOperations)
        assert client.publish is ops
