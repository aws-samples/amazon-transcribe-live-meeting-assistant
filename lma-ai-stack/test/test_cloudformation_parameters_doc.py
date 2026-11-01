# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The CloudFormation parameter reference matches the template (no AWS).

`docs/cloudformation-parameters.md` is generated from `lma-main.yaml` by
`scripts/cloudformation_parameters_doc.py`. A reference page written by hand
drifts one parameter at a time -- a renamed parameter keeps its old row, a new
one never gets a row -- and a reader cannot tell a stale entry from a current
one. These tests make the drift a failure:

* every declared parameter is classified, and nothing else is;
* the generated block on the page is exactly what the script produces now, so a
  parameter added, removed, re-described or given a new default without
  regenerating the page fails here;
* the hand-written notes around that block name, in backticks, only parameters
  the template declares, or identifiers listed below as deliberately not
  parameters.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cloudformation_parameters_doc.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("cloudformation_parameters_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DOCGEN = _load_script()

#: Backticked identifiers the notes use that are not parameters of the main
#: template: mapping keys, CLI commands and values. A name added here must be a
#: real thing of the kind its comment says.
NOT_PARAMETERS = {
    # Keys of the AsrDefaults mapping in lma-main.yaml.
    "AsrDefaults",
    "ModelBundle",
    "MaxMeetingSeconds",
}

#: A backticked token shaped like a parameter name: CamelCase, starting with a
#: capital and holding at least one lower-case letter, so an all-capitals value
#: such as `MICROVM` is not mistaken for one.
PARAMETER_SHAPED = re.compile(r"`([A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*)`")


@pytest.fixture(scope="module")
def template():
    return DOCGEN.load_template()


@pytest.fixture(scope="module")
def page() -> str:
    return DOCGEN.DOC.read_text(encoding="utf-8")


def test_every_parameter_is_classified_and_nothing_else_is(template) -> None:
    assert DOCGEN.classification_problems(template) == []


def test_the_generated_reference_is_current(template, page) -> None:
    assert DOCGEN.render_page(page, template) == page, (
        "docs/cloudformation-parameters.md is out of date with lma-main.yaml; run "
        "python3 scripts/cloudformation_parameters_doc.py and commit the result"
    )


def test_every_parameter_has_an_entry_on_the_page(template, page) -> None:
    missing = [name for name in template["Parameters"] if f"### `{name}`" not in page]
    assert not missing, f"no entry on the parameter reference for: {missing}"


def test_the_notes_name_only_declared_parameters(template, page) -> None:
    start = page.index(DOCGEN.BEGIN_MARKER)
    end = page.index(DOCGEN.END_MARKER) + len(DOCGEN.END_MARKER)
    notes = page[:start] + page[end:]
    named = set(PARAMETER_SHAPED.findall(notes))
    unknown = sorted(named - set(template["Parameters"]) - NOT_PARAMETERS)
    assert not unknown, (
        f"the hand-written part of docs/cloudformation-parameters.md names {unknown}, "
        "which lma-main.yaml does not declare as parameters"
    )


def test_every_group_and_label_names_a_declared_parameter(template) -> None:
    interface = template["Metadata"]["AWS::CloudFormation::Interface"]
    declared = set(template["Parameters"])
    grouped = [name for group in interface["ParameterGroups"] for name in group["Parameters"]]
    assert sorted(set(grouped) - declared) == [], "a parameter group lists an undeclared parameter"
    assert sorted(declared - set(grouped)) == [], "a parameter is in no parameter group"
    labels = set(interface["ParameterLabels"])
    assert sorted(labels - declared) == [], "a label names an undeclared parameter"
    assert sorted(declared - labels) == [], "a parameter has no console label"


#: Directories whose Markdown is not this repository's own.
SKIPPED_DIRECTORIES = {"node_modules", ".git"}


def _slug(heading: str) -> str:
    """The anchor the documentation site gives a heading (github-slugger's rule)."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def test_every_link_into_the_page_names_a_heading_it_has(page) -> None:
    """Headings come from the template's group labels, so a relabel moves anchors.

    Every Markdown file in the repository is searched, the CHANGELOG included,
    because a link there breaks just as silently as one in the docs.
    """
    anchors = {_slug(line.lstrip("#")) for line in page.splitlines() if re.match(r"^#{1,6} ", line)}
    broken = []
    for path in REPO_ROOT.rglob("*.md"):
        if SKIPPED_DIRECTORIES.intersection(path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for anchor in re.findall(r"cloudformation-parameters\.md#([\w-]+)", text):
            if anchor not in anchors:
                broken.append(f"{path.relative_to(REPO_ROOT)}: #{anchor}")
    assert not broken, f"links to headings docs/cloudformation-parameters.md does not have: {broken}"
