"""Shared fixtures for property-based tests."""
import os
import pytest
import yaml

# Canonical list of 54 Amazon Transcribe streaming language codes
CANONICAL_LANGUAGE_CODES = [
    "af-ZA", "ar-AE", "ar-SA", "ca-ES", "cs-CZ", "da-DK", "de-CH", "de-DE",
    "el-GR", "en-AB", "en-AU", "en-GB", "en-IE", "en-IN", "en-NZ", "en-US",
    "en-WL", "en-ZA", "es-ES", "es-US", "eu-ES", "fa-IR", "fi-FI", "fr-CA",
    "fr-FR", "gl-ES", "he-IL", "hi-IN", "hr-HR", "id-ID", "it-IT", "ja-JP",
    "ko-KR", "lv-LV", "ms-MY", "nl-NL", "no-NO", "pl-PL", "pt-BR", "pt-PT",
    "ro-RO", "ru-RU", "sk-SK", "so-SO", "sr-RS", "sv-SE", "tl-PH", "th-TH",
    "uk-UA", "vi-VN", "zh-CN", "zh-HK", "zh-TW", "zu-ZA",
]

# Template paths relative to the project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEMPLATE_PATHS = {
    "lma-main": os.path.join(PROJECT_ROOT, "lma-main.yaml"),
    "lma-websocket-transcriber": os.path.join(
        PROJECT_ROOT,
        "lma-websocket-transcriber-stack",
        "deployment",
        "lma-websocket-transcriber.yaml",
    ),
    "lma-virtual-participant": os.path.join(
        PROJECT_ROOT,
        "lma-virtual-participant-stack",
        "template.yaml",
    ),
}

# The AllowedPattern regex used across all three templates
ALLOWED_PATTERN_REGEX = (
    r'^(?:\s*(?:af-ZA|ar-AE|ar-SA|ca-ES|cs-CZ|da-DK|de-CH|de-DE|el-GR|en-AB'
    r'|en-AU|en-GB|en-IE|en-IN|en-NZ|en-US|en-WL|en-ZA|es-ES|es-US|eu-ES'
    r'|fa-IR|fi-FI|fr-CA|fr-FR|gl-ES|he-IL|hi-IN|hr-HR|id-ID|it-IT|ja-JP'
    r'|ko-KR|lv-LV|ms-MY|nl-NL|no-NO|pl-PL|pt-BR|pt-PT|ro-RO|ru-RU|sk-SK'
    r'|so-SO|sr-RS|sv-SE|tl-PH|th-TH|uk-UA|vi-VN|zh-CN|zh-HK|zh-TW|zu-ZA'
    r')\s*(?:,\s*(?:af-ZA|ar-AE|ar-SA|ca-ES|cs-CZ|da-DK|de-CH|de-DE|el-GR'
    r'|en-AB|en-AU|en-GB|en-IE|en-IN|en-NZ|en-US|en-WL|en-ZA|es-ES|es-US'
    r'|eu-ES|fa-IR|fi-FI|fr-CA|fr-FR|gl-ES|he-IL|hi-IN|hr-HR|id-ID|it-IT'
    r'|ja-JP|ko-KR|lv-LV|ms-MY|nl-NL|no-NO|pl-PL|pt-BR|pt-PT|ro-RO|ru-RU'
    r'|sk-SK|so-SO|sr-RS|sv-SE|tl-PH|th-TH|uk-UA|vi-VN|zh-CN|zh-HK|zh-TW'
    r'|zu-ZA)\s*)*)?$'
)


# Custom YAML loader for CloudFormation templates
# CloudFormation uses custom tags like !Ref, !Equals, !Or, !And, !If, !Sub, etc.
# We need to handle these gracefully when parsing templates.

class CloudFormationLoader(yaml.SafeLoader):
    """YAML loader that handles CloudFormation intrinsic function tags."""
    pass


def _cfn_tag_constructor(loader, tag_suffix, node):
    """Generic constructor for CloudFormation tags."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


# Register constructors for all CloudFormation intrinsic functions
_CFN_TAGS = [
    "!Ref", "!Sub", "!GetAtt", "!Select", "!Split", "!Join",
    "!Equals", "!If", "!Not", "!And", "!Or", "!Condition",
    "!FindInMap", "!GetAZs", "!ImportValue", "!Base64",
    "!Cidr", "!Transform",
]

for tag in _CFN_TAGS:
    CloudFormationLoader.add_constructor(
        tag, lambda loader, node, t=tag: _cfn_tag_constructor(loader, t, node)
    )

# Also handle any unknown tags with a multi-constructor
CloudFormationLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: _cfn_tag_constructor(loader, suffix, node)
)


def load_cfn_template(path):
    """Load a CloudFormation YAML template, handling intrinsic function tags."""
    with open(path, "r") as f:
        return yaml.load(f, Loader=CloudFormationLoader)


@pytest.fixture
def canonical_language_codes():
    """Return the canonical list of 54 streaming language codes."""
    return CANONICAL_LANGUAGE_CODES


@pytest.fixture
def template_paths():
    """Return paths to the three CloudFormation templates."""
    return TEMPLATE_PATHS


@pytest.fixture
def allowed_pattern_regex():
    """Return the AllowedPattern regex used in templates."""
    return ALLOWED_PATTERN_REGEX
