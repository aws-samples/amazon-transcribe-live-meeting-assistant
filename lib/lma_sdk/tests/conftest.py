# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Shared pytest fixtures for LMA SDK tests."""

import pytest
import boto3
from unittest.mock import MagicMock, patch

from lma_sdk import LMAClient


@pytest.fixture
def stack_name():
    return "test-lma-stack"


@pytest.fixture
def region():
    return "us-east-1"


@pytest.fixture
def mock_session():
    """Create a mock boto3 session."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"
    return session


@pytest.fixture
def client(stack_name, region, mock_session):
    """Create an LMAClient with a mocked boto3 session."""
    return LMAClient(stack_name=stack_name, region=region, session=mock_session)


@pytest.fixture
def client_no_stack(region, mock_session):
    """Create an LMAClient without a stack name."""
    return LMAClient(stack_name="", region=region, session=mock_session)
