# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Shared pytest fixtures for LMA SDK tests."""

import sys
from pathlib import Path

# No test here sends a request to AWS; see scripts/testing/no_real_aws.py. First,
# so that a client built while the SDK is imported gets the fake credentials.
_GUARD_DIR = Path(__file__).resolve().parents[3] / "scripts" / "testing"
if str(_GUARD_DIR) not in sys.path:
    sys.path.append(str(_GUARD_DIR))

# The guard's pytest_configure hook and its autouse fixture.
from no_real_aws import *
from no_real_aws import activate

activate()

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
