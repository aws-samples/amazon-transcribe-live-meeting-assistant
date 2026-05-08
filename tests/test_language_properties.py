"""Property-based tests for streaming language support.

Tests validate the AllowedPattern regex, template consistency,
and validation logic for language identification parameters.
"""
import re
import string

import pytest
import yaml
from hypothesis import given, settings, assume
from hypothesis import strategies as st

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import (
    CANONICAL_LANGUAGE_CODES,
    TEMPLATE_PATHS,
    ALLOWED_PATTERN_REGEX,
    load_cfn_template,
)


# --- Strategies ---

def valid_language_code_lists():
    """Generate non-empty subsets of valid language codes."""
    return st.lists(
        st.sampled_from(CANONICAL_LANGUAGE_CODES),
        min_size=1,
        max_size=10,
        unique=True,
    )


def whitespace():
    """Generate optional whitespace (spaces only, as typical in CSV)."""
    return st.text(alphabet=" ", min_size=0, max_size=3)


def invalid_language_codes():
    """Generate strings that are NOT valid language codes.

    Generates codes in xx-YY format that are not in the canonical list.
    """
    # Generate a 2-letter lowercase prefix and 2-letter uppercase suffix
    prefix = st.text(alphabet=string.ascii_lowercase, min_size=2, max_size=2)
    suffix = st.text(alphabet=string.ascii_uppercase, min_size=2, max_size=2)
    return st.tuples(prefix, suffix).map(
        lambda t: f"{t[0]}-{t[1]}"
    ).filter(lambda code: code not in CANONICAL_LANGUAGE_CODES)


# --- Property 1: AllowedPattern regex accepts all valid language code combinations ---


class TestProperty1RegexAcceptsValid:
    """Property 1: AllowedPattern regex accepts all valid language code combinations.

    **Validates: Requirements 2.1**
    """

    @given(
        codes=valid_language_code_lists(),
        leading_ws=whitespace(),
        trailing_ws=whitespace(),
        separator_ws=whitespace(),
    )
    @settings(max_examples=200)
    def test_regex_accepts_valid_combinations(
        self, codes, leading_ws, trailing_ws, separator_ws
    ):
        """For any comma-separated string of valid language codes with optional
        whitespace, the AllowedPattern regex must match."""
        separator = f",{separator_ws}"
        value = f"{leading_ws}{separator.join(codes)}{trailing_ws}"
        pattern = re.compile(ALLOWED_PATTERN_REGEX)
        assert pattern.match(value) is not None, (
            f"Regex should accept valid combination: '{value}'"
        )

    def test_regex_accepts_empty_string(self):
        """The regex must accept an empty string (requirement 2.3)."""
        pattern = re.compile(ALLOWED_PATTERN_REGEX)
        assert pattern.match("") is not None

    def test_regex_accepts_single_code(self):
        """The regex must accept a single valid language code."""
        pattern = re.compile(ALLOWED_PATTERN_REGEX)
        for code in CANONICAL_LANGUAGE_CODES:
            assert pattern.match(code) is not None, (
                f"Regex should accept single code: '{code}'"
            )


# --- Property 2: AllowedPattern regex rejects invalid language codes ---


class TestProperty2RegexRejectsInvalid:
    """Property 2: AllowedPattern regex rejects invalid language codes.

    **Validates: Requirements 2.2**
    """

    @given(invalid_code=invalid_language_codes())
    @settings(max_examples=200)
    def test_regex_rejects_single_invalid_code(self, invalid_code):
        """For any language code NOT in the canonical list, the regex must not match."""
        pattern = re.compile(ALLOWED_PATTERN_REGEX)
        match = pattern.match(invalid_code)
        # The match should either be None or match empty string only
        if match is not None:
            # If it matches, it should only match the empty prefix (zero-length)
            assert match.group() == "", (
                f"Regex should reject invalid code: '{invalid_code}'"
            )

    @given(
        valid_codes=valid_language_code_lists(),
        invalid_code=invalid_language_codes(),
    )
    @settings(max_examples=200)
    def test_regex_rejects_mixed_valid_and_invalid(self, valid_codes, invalid_code):
        """A list containing any invalid code must not fully match the regex."""
        # Insert the invalid code somewhere in the list
        codes = valid_codes + [invalid_code]
        value = ", ".join(codes)
        pattern = re.compile(ALLOWED_PATTERN_REGEX)
        match = pattern.match(value)
        if match is not None:
            # Full string should not be consumed
            assert match.group() != value, (
                f"Regex should reject list with invalid code: '{value}'"
            )

    def test_regex_rejects_known_invalid_codes(self):
        """Specific invalid codes must be rejected."""
        pattern = re.compile(ALLOWED_PATTERN_REGEX)
        invalid_examples = ["xx-YY", "en-XX", "zz-ZZ", "ab-CD", "foo", "en_US"]
        for code in invalid_examples:
            match = pattern.match(code)
            if match is not None:
                assert match.group() == "", (
                    f"Regex should reject: '{code}'"
                )


# --- Property 3: Template language list consistency ---


class TestProperty3TemplateConsistency:
    """Property 3: Template language list consistency.

    **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
    """

    @pytest.fixture(autouse=True)
    def load_templates(self):
        """Load all three templates."""
        self.templates = {}
        for name, path in TEMPLATE_PATHS.items():
            self.templates[name] = load_cfn_template(path)

    def _get_allowed_values(self, template, param_name):
        """Extract AllowedValues for a parameter from a template."""
        params = template.get("Parameters", {})
        param = params.get(param_name, {})
        return set(param.get("AllowedValues", []))

    def test_transcribe_language_code_consistent(self):
        """TranscribeLanguageCode AllowedValues must be identical across all templates."""
        sets = {}
        for name, template in self.templates.items():
            sets[name] = self._get_allowed_values(template, "TranscribeLanguageCode")

        template_names = list(sets.keys())
        for i in range(len(template_names)):
            for j in range(i + 1, len(template_names)):
                name_a = template_names[i]
                name_b = template_names[j]
                assert sets[name_a] == sets[name_b], (
                    f"TranscribeLanguageCode mismatch between {name_a} and {name_b}. "
                    f"Diff: {sets[name_a].symmetric_difference(sets[name_b])}"
                )

    def test_transcribe_preferred_language_consistent(self):
        """TranscribePreferredLanguage AllowedValues must be identical across templates."""
        sets = {}
        for name, template in self.templates.items():
            sets[name] = self._get_allowed_values(
                template, "TranscribePreferredLanguage"
            )

        template_names = list(sets.keys())
        for i in range(len(template_names)):
            for j in range(i + 1, len(template_names)):
                name_a = template_names[i]
                name_b = template_names[j]
                assert sets[name_a] == sets[name_b], (
                    f"TranscribePreferredLanguage mismatch between "
                    f"{name_a} and {name_b}. "
                    f"Diff: {sets[name_a].symmetric_difference(sets[name_b])}"
                )

    @given(code=st.sampled_from(CANONICAL_LANGUAGE_CODES))
    @settings(max_examples=54)
    def test_every_canonical_code_in_all_templates(self, code):
        """Every canonical language code must appear in all templates."""
        for name, template in self.templates.items():
            codes = self._get_allowed_values(template, "TranscribeLanguageCode")
            assert code in codes, (
                f"Code '{code}' missing from {name} TranscribeLanguageCode"
            )

            preferred = self._get_allowed_values(
                template, "TranscribePreferredLanguage"
            )
            assert code in preferred, (
                f"Code '{code}' missing from {name} TranscribePreferredLanguage"
            )


# --- Property 4: Validator requires at least two language codes for identification mode ---


def validate_language_options_count(language_code, language_options):
    """Simulate the validation Lambda logic for language options count.

    When TranscribeLanguageCode is set to identify-language or
    identify-multiple-languages, TranscribeLanguageOptions must contain
    at least two language codes.

    Returns True if valid, False if invalid.
    """
    identification_modes = ["identify-language", "identify-multiple-languages"]
    if language_code not in identification_modes:
        return True

    if not language_options or language_options.strip() == "":
        return False

    codes = [c.strip() for c in language_options.split(",") if c.strip()]
    return len(codes) >= 2


class TestProperty4ValidatorMinimumCodes:
    """Property 4: Validator requires at least two language codes for identification mode.

    **Validates: Requirements 6.1**
    """

    @given(
        mode=st.sampled_from(["identify-language", "identify-multiple-languages"]),
        code=st.sampled_from(CANONICAL_LANGUAGE_CODES),
    )
    @settings(max_examples=200)
    def test_single_code_rejected_in_identification_mode(self, mode, code):
        """A single language code must be rejected when in identification mode."""
        assert validate_language_options_count(mode, code) is False, (
            f"Validator should reject single code '{code}' for mode '{mode}'"
        )

    @given(
        mode=st.sampled_from(["identify-language", "identify-multiple-languages"]),
    )
    @settings(max_examples=100)
    def test_empty_options_rejected_in_identification_mode(self, mode):
        """Empty language options must be rejected when in identification mode."""
        assert validate_language_options_count(mode, "") is False
        assert validate_language_options_count(mode, "   ") is False

    @given(
        mode=st.sampled_from(["identify-language", "identify-multiple-languages"]),
        codes=st.lists(
            st.sampled_from(CANONICAL_LANGUAGE_CODES),
            min_size=2,
            max_size=5,
            unique=True,
        ),
    )
    @settings(max_examples=200)
    def test_two_or_more_codes_accepted_in_identification_mode(self, mode, codes):
        """Two or more language codes must be accepted in identification mode."""
        options = ", ".join(codes)
        assert validate_language_options_count(mode, options) is True, (
            f"Validator should accept '{options}' for mode '{mode}'"
        )


# --- Property 5: Validator rejects preferred language not in options ---


def validate_preferred_language_in_options(preferred_language, language_options):
    """Simulate the validation Lambda logic for preferred language.

    If TranscribePreferredLanguage is set to a value other than "None",
    it must be present in TranscribeLanguageOptions.

    Returns True if valid, False if invalid.
    """
    if preferred_language == "None":
        return True

    if not language_options or language_options.strip() == "":
        return False

    codes = [c.strip() for c in language_options.split(",") if c.strip()]
    return preferred_language in codes


class TestProperty5ValidatorPreferredLanguage:
    """Property 5: Validator rejects preferred language not in options.

    **Validates: Requirements 6.4**
    """

    @given(
        preferred=st.sampled_from(CANONICAL_LANGUAGE_CODES),
        options_codes=st.lists(
            st.sampled_from(CANONICAL_LANGUAGE_CODES),
            min_size=2,
            max_size=5,
            unique=True,
        ),
    )
    @settings(max_examples=200)
    def test_preferred_not_in_options_rejected(self, preferred, options_codes):
        """A preferred language not in the options list must be rejected."""
        # Ensure preferred is NOT in the options list
        assume(preferred not in options_codes)
        options = ", ".join(options_codes)
        assert validate_preferred_language_in_options(preferred, options) is False, (
            f"Validator should reject preferred '{preferred}' not in '{options}'"
        )

    @given(
        options_codes=st.lists(
            st.sampled_from(CANONICAL_LANGUAGE_CODES),
            min_size=2,
            max_size=5,
            unique=True,
        ),
    )
    @settings(max_examples=200)
    def test_preferred_in_options_accepted(self, options_codes):
        """A preferred language that IS in the options list must be accepted."""
        # Pick one of the options as preferred
        preferred = options_codes[0]
        options = ", ".join(options_codes)
        assert validate_preferred_language_in_options(preferred, options) is True, (
            f"Validator should accept preferred '{preferred}' in '{options}'"
        )

    def test_none_preferred_always_accepted(self):
        """'None' as preferred language is always valid."""
        assert validate_preferred_language_in_options("None", "") is True
        assert validate_preferred_language_in_options("None", "en-US, es-US") is True
