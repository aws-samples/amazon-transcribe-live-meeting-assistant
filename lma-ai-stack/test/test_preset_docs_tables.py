# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The configuration presets' tables in the docs agree with the shipped presets (no AWS).

Three pages tabulate what each preset sets: the Configuration Guide's table of
every preset (columns named by preset id), the Privacy and Data Handling page's
(columns named by preset title), and the Hosting Models page's table for each
hosting preset (a Value column under a heading naming the preset). A cell is
compared with the value the preset gives the setting, or the shipped default
where the preset leaves it, rendered as the Configuration page shows it: On or
Off, an enum's label, or the value itself. A row is matched to a setting by its
`Section › Setting` label, or on the privacy page by the setting's own label.

The Configuration Guide's table must also list every setting the presets set,
apart from the two whiteboard and live guidance switches its prose covers.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAYER = REPO_ROOT / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config"
SEEDER_DEFAULTS = REPO_ROOT / "lma-ai-stack/source/lambda_functions/configuration_seeder/defaults"
DOCS = REPO_ROOT / "docs"

#: Settings every preset sets that the Configuration Guide covers in prose rather
#: than in its table.
COVERED_IN_PROSE = {
    ("meeting_assist", ("guidance", "defaultEnabled")),
    ("meeting_assist", ("whiteboards", "defaultEnabled")),
}

#: Row labels on the privacy page that shorten the setting's own label.
PRIVACY_ALIASES = {
    "meeting assistant: web search": "meeting assistant › web search",
    "meeting assistant: mcp servers": "meeting assistant › mcp servers",
    "meeting assistant: leave out sensitive personal information": (
        "meeting assistant › leave out sensitive personal information"
    ),
    "longest consent given in advance": "longest consent given in advance, in days",
}

Setting = Tuple[str, Tuple[str, ...]]


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


SCHEMAS = {p.stem: _load(p) for p in (LAYER / "schemas").glob("*.json")}
PRESETS = {p.stem: _load(p) for p in (LAYER / "presets").glob("*.json")}
DEFAULTS = _load(LAYER / "setting_defaults.json")
# The consent and retention sections take their shipped values from the seeder.
SEEDED = {
    "meeting_consent": _load(SEEDER_DEFAULTS / "MeetingConsent.json"),
    "retention": _load(SEEDER_DEFAULTS / "Retention.json"),
}


def _labels() -> Dict[str, Tuple[Setting, Dict[str, Any]]]:
    """Every setting's `Section › Setting` label, lower-cased, with its schema."""
    found: Dict[str, Tuple[Setting, Dict[str, Any]]] = {}

    def walk(section: str, props: Dict[str, Any], path: Tuple[str, ...], prefix: str) -> None:
        for key, prop in props.items():
            title = prop.get("title")
            if title:
                found.setdefault(_norm(prefix + title), ((section, path + (key,)), prop))
            if prop.get("type") == "object" and "properties" in prop:
                walk(
                    section,
                    prop["properties"],
                    path + (key,),
                    prefix + (f"{title} › " if title else ""),
                )

    for section, schema in SCHEMAS.items():
        walk(section, schema.get("properties", {}), (), f"{schema['sectionLabel']} › ")
    return found


def _norm(text: str) -> str:
    return re.sub(r"[*`]", "", text).strip().lower().replace(" (opt-in)", "")


LABELS = _labels()
_MISSING = object()


def _dig(values: Any, path: Tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(values, dict) or key not in values:
            return _MISSING
        values = values[key]
    return values


def _default(setting: Setting) -> Any:
    section, path = setting
    declared = DEFAULTS.get(section, {}).get(path[0])
    if isinstance(declared, dict) and "type" in declared:
        value = _dig({path[0]: declared.get("default")}, path)
        if value is not _MISSING:
            return value
    return _dig(SEEDED.get(section, {}), path)


def _value(preset_id: str, setting: Setting) -> Any:
    section, path = setting
    change = PRESETS[preset_id]["sections"].get(section, {}).get("values", {})
    value = _dig(change, path)
    return _default(setting) if value is _MISSING else value


def _renderings(value: Any, prop: Dict[str, Any]) -> List[str]:
    if isinstance(value, bool):
        return ["on" if value else "off"]
    label = (prop.get("enumLabels") or {}).get(value)
    if label:
        return [_norm(label), _norm(re.split(r" \(|:", label)[0])]
    return [_norm(str(value))]


def _cell_matches(cell: str, setting: Setting, prop: Dict[str, Any], value: Any) -> bool:
    text = _norm(cell)
    if text.startswith("shipped") and value == _default(setting):
        return True
    text = re.sub(r" days$", "", text)
    for rendered in _renderings(value, prop):
        if text == rendered or re.match(re.escape(rendered) + r"[ ,;.(]", text):
            return True
        # A shortened label: "Refuse" for "Refuse the meeting".
        if len(text) >= 6 and rendered.startswith(text):
            return True
    return False


def _tables(page: Path):
    """Yield (heading, header cells, [(line number, row cells)]) for each table."""
    heading, header, rows = "", None, []
    for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("#"):
            heading = line
        if not line.startswith("|"):
            if header is not None:
                yield heading, header, rows
            header, rows = None, []
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
        elif not set("".join(cells)) <= set("-: "):
            rows.append((number, cells))
    if header is not None:
        yield heading, header, rows


def _preset_columns(header: List[str]) -> Dict[int, str]:
    by_title = {_norm(p["title"]): pid for pid, p in PRESETS.items()}
    columns = {}
    for index, cell in enumerate(header):
        name = _norm(cell)
        if name in PRESETS:
            columns[index] = name
        elif name in by_title:
            columns[index] = by_title[name]
    return columns


def _setting_for(label: str) -> Optional[Tuple[Setting, Dict[str, Any]]]:
    name = _norm(label)
    if name in LABELS:
        return LABELS[name]
    name = PRIVACY_ALIASES.get(name, name)
    # The privacy page names a setting by its own label; take the shallowest match.
    matches = sorted(
        (key for key in LABELS if key == name or key.endswith(" › " + name)),
        key=lambda k: k.count("›"),
    )
    return LABELS[matches[0]] if matches else None


def _preset_table(page: str) -> Tuple[Dict[int, str], List[Tuple[int, List[str]]]]:
    for _, header, rows in _tables(DOCS / page):
        columns = _preset_columns(header)
        if len(columns) == len(PRESETS):
            return columns, rows
    raise AssertionError(
        f"docs/{page} has no table with a column for each of the {len(PRESETS)} presets"
    )


@pytest.mark.parametrize("page", ["configuration-guide.md", "privacy-and-data-handling.md"])
def test_every_preset_column_matches_the_shipped_preset(page: str) -> None:
    columns, rows = _preset_table(page)
    wrong, unknown = [], []
    for number, cells in rows:
        found = _setting_for(cells[0])
        if found is None:
            unknown.append(f"{page}:{number} {cells[0]!r}")
            continue
        setting, prop = found
        for index, preset_id in columns.items():
            value = _value(preset_id, setting)
            if not _cell_matches(cells[index], setting, prop, value):
                wrong.append(
                    f"{page}:{number} {preset_id} {cells[0]}: says {cells[index]!r}, preset has {value!r}"
                )
    assert not unknown, f"rows that name no setting: {unknown}"
    assert not wrong, "preset table cells that disagree with the presets:\n" + "\n".join(wrong)


def test_the_configuration_guide_lists_every_setting_the_presets_set() -> None:
    _, rows = _preset_table("configuration-guide.md")
    listed = set()
    for _, cells in rows:
        found = _setting_for(cells[0])
        if found:
            listed.add((found[0][0], found[0][1]))
    set_by_presets = set()

    def walk(section: str, values: Dict[str, Any], path: Tuple[str, ...]) -> None:
        for key, value in values.items():
            if isinstance(value, dict):
                walk(section, value, path + (key,))
            else:
                set_by_presets.add((section, path + (key,)))

    for preset in PRESETS.values():
        for section, change in preset["sections"].items():
            walk(section, change["values"], ())
    missing = sorted(set_by_presets - listed - COVERED_IN_PROSE)
    assert not missing, (
        f"settings the presets set that docs/configuration-guide.md's preset table omits: {missing}"
    )


@pytest.mark.parametrize("preset_id", ["team-hosted-external", "customer-account"])
def test_each_hosting_preset_table_matches_the_shipped_preset(preset_id: str) -> None:
    title = _norm(PRESETS[preset_id]["title"]).split(" (")[0]
    checked, wrong, unknown = 0, [], []
    for heading, header, rows in _tables(DOCS / "hosting-models.md"):
        if title not in _norm(heading) or "value" not in [_norm(h) for h in header]:
            continue
        value_column = [_norm(h) for h in header].index("value")
        for number, cells in rows:
            for label_cell in cells[:value_column]:
                if "›" not in label_cell:
                    continue
                # A cell may name several settings, "A › x; B › y", with their
                # values in the same order in the Value cell: "Off; Global".
                labels = [part.strip() for part in label_cell.split(";")]
                values = [part.strip() for part in cells[value_column].split(";")]
                if len(values) != len(labels):
                    values = [cells[value_column]] * len(labels)
                for label, cell in zip(labels, values):
                    found = _setting_for(label)
                    if not found:
                        unknown.append(f"hosting-models.md:{number} {label!r}")
                        continue
                    setting, prop = found
                    value = _value(preset_id, setting)
                    checked += 1
                    if not _cell_matches(cell, setting, prop, value):
                        wrong.append(
                            f"hosting-models.md:{number} {label}: says {cell!r}, preset has {value!r}"
                        )
    assert not unknown, f"setting labels in docs/hosting-models.md that name no setting: {unknown}"
    assert checked >= 10, (
        f"found only {checked} settings in docs/hosting-models.md's table for {preset_id}"
    )
    assert not wrong, "hosting preset table cells that disagree with the preset:\n" + "\n".join(
        wrong
    )
