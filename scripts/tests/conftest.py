# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Shared pytest configuration for the tests of the stand-alone scripts in scripts/."""


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: a unit test; needs no AWS, Chrome or network")
