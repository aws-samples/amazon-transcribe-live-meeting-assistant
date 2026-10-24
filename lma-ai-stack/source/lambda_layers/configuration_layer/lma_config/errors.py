#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Errors raised by the configuration store.

Each is a distinct type because the callers act differently on them: an AppSync
resolver turns :class:`ConcurrentModification` into a "reload and try again"
message for the admin, while :class:`UnknownConfigurationSection` is a
programming error that should fail the build's tests rather than reach a user.
"""

from __future__ import annotations


class ConfigurationError(Exception):
    """Base class for every error this package raises."""


class ConfigurationTableNotConfigured(ConfigurationError):
    """No table name was supplied and ``CONFIGURATION_TABLE_NAME`` is unset."""


class InvalidProfileName(ConfigurationError):
    """The profile name is outside the permitted character set or too long."""


class ReservedProfileName(ConfigurationError):
    """The name is one the store keeps for itself.

    ``default`` is the deployment baseline that CloudFormation rewrites and
    ``__active`` is the pointer naming the profile in force, so neither may be
    taken by a profile: the store owns those two keys outright.
    """


class UnknownConfigurationSection(ConfigurationError):
    """The section is not in the registry, so no caching policy is declared.

    Raised instead of guessing a policy. A section's cache tier is part of its
    declaration precisely so that the decision is made once, in the registry,
    rather than at each call site.
    """


class ConfigurationTooLarge(ConfigurationError):
    """The compressed record would not fit in a DynamoDB item.

    Raised rather than truncating: a configuration silently missing its tail is
    harder to diagnose than a save that refuses.
    """


class ConcurrentModification(ConfigurationError):
    """The record changed since the version the caller read.

    The write is refused so that the edit the caller did not see is kept.
    """
