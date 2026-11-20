#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Meeting assistant skills: what one is, the files it is read from and written to, and the library.

A skill is guidance an administrator writes for the meeting assistant: a name, a
description the assistant chooses it by, Markdown instructions, and optional
reference files. It holds text only. Nothing in a skill is run: a file that is
not one of the text types in ``TEXT_EXTENSIONS``, that is marked executable in a
zip, or that starts with an interpreter line (``#!``) is left out of an import
and reported, and a create or update that names one is refused.

The formats read (``parse_skill_file``):

- **Agent Skills** ``SKILL.md``: YAML front matter with ``name`` and
  ``description`` (and optionally ``license``, ``compatibility`` and
  ``metadata``), then the instructions in Markdown.
- **An Agent Skills folder as a zip**: ``SKILL.md`` at the root of the zip or of
  its one top-level folder, with reference files beside it.
- **Amazon Quick** skill ``.md``: front matter with ``name``, ``display_name``,
  ``trigger``, ``icon`` and ``inputs`` and no ``description``; the description
  is taken from the first paragraph of the body, and the other fields are kept
  as the skill's metadata.

The formats written: ``SKILL.md`` (``render_skill_md``), a zip of the folder
(``render_skill_zip``), and the library document of every skill
(``export_library``), ``kind: "lma-skill-library"``, ``version: 1``, which
``plan_import`` reads back, matching skills by name.

This module does no I/O.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

LIBRARY_KIND = "lma-skill-library"
LIBRARY_VERSION = 1

#: A skill name, as the Agent Skills format defines one: lowercase letters,
#: digits and single hyphens between them.
NAME_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_NAME = 64
#: A skill id: a name, or a name with a number added to keep it unique.
SKILL_ID_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_SKILL_ID = 80
MAX_DISPLAY_NAME = 128
MAX_DESCRIPTION = 1024

#: Sizes, in bytes of UTF-8. A skill is stored as one DynamoDB item (at most
#: 400 KB), and the library export of every skill is one AppSync response (at
#: most 5 MB). MAX_SKILLS skills of MAX_SKILL_BYTES are 2.5 MB of text; the
#: Python 3.12 Lambda runtime writes the export without escaping non-ASCII
#: characters, so text in any script stays near that size, and the AWSJSON
#: string escapes only quotes, backslashes and control characters.
MAX_INSTRUCTIONS_BYTES = 48 * 1024
MAX_FILE_BYTES = 32 * 1024
MAX_SKILL_BYTES = 64 * 1024
MAX_FILES = 20
MAX_SKILLS = 40
#: An uploaded zip, before it is read.
MAX_ZIP_BYTES = 512 * 1024
MAX_ZIP_ENTRIES = 200

#: The reference file types a skill can hold: text and Markdown.
TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".csv", ".json", ".yaml", ".yml")
PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MAX_PATH = 200
MAX_PATH_DEPTH = 4
SKILL_FILE_NAME = "SKILL.md"

#: Front-matter fields kept as the skill's metadata, beside name and description.
#: The Amazon Quick fields (display_name becomes DisplayName instead), and the
#: optional Agent Skills fields.
METADATA_TEXT_FIELDS = {"trigger": 256, "icon": 16, "license": 256, "compatibility": 500}
METADATA_LIST_FIELDS = {"inputs": (20, 128)}
METADATA_MAP_FIELD = "metadata"
MAX_METADATA_MAP = 20
MAX_METADATA_VALUE = 512

SOURCE_AGENT_SKILLS = "agent-skills"
SOURCE_QUICK = "amazon-quick"
SOURCE_MANUAL = "manual"
SOURCE_LIBRARY = "library"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


class SkillError(ValueError):
    """A skill, a skill file or a library that cannot be accepted, with the reason."""


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


# ── the skill itself ────────────────────────────────────────────────────────


@dataclass
class SkillFile:
    """One reference file of a skill: a relative path and its text."""

    path: str
    content: str


@dataclass
class Skill:
    """A skill, checked: what a create, an update or an import stores."""

    name: str
    description: str
    instructions: str
    display_name: str = ""
    files: List[SkillFile] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_format: str = SOURCE_MANUAL

    def to_item_fields(self) -> Dict[str, Any]:
        """The stored attributes this skill sets (the record's keys and audit fields aside)."""
        return {
            "Name": self.name,
            "DisplayName": self.display_name,
            "Description": self.description,
            "Instructions": self.instructions,
            "Files": [{"Path": f.path, "Content": f.content} for f in self.files],
            "Metadata": dict(self.metadata),
            "SourceFormat": self.source_format,
        }


@dataclass
class Skipped:
    """A file an import left out, and why."""

    path: str
    reason: str


def slug(value: str) -> str:
    """``value`` as a skill name: lowercase, with every run of other characters one hyphen."""
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text[:MAX_NAME].strip("-")


def check_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise SkillError("A skill needs a name.")
    if len(name) > MAX_NAME or not NAME_PATTERN.fullmatch(name):
        raise SkillError(
            f"The skill name {name!r} is not accepted: use lowercase letters, digits and single "
            f"hyphens between them, at most {MAX_NAME} characters."
        )
    return name


def _plain_text(value: Any, label: str, limit: int, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise SkillError(f"A skill needs a {label}.")
        return ""
    if not isinstance(value, str):
        raise SkillError(f"The {label} must be text.")
    text = value.strip()
    if required and not text:
        raise SkillError(f"A skill needs a {label}.")
    if len(text) > limit:
        raise SkillError(f"The {label} is {len(text)} characters; at most {limit} are accepted.")
    if _CONTROL.search(text):
        raise SkillError(f"The {label} holds control characters.")
    return text


def executable_reason(path: str, content: Optional[str] = None, mode: int = 0) -> Optional[str]:
    """Why a file is not accepted as a reference file, or None when it is."""
    lowered = path.lower()
    if not lowered.endswith(TEXT_EXTENSIONS):
        return (
            "only text and Markdown reference files are kept "
            f"({', '.join(TEXT_EXTENSIONS)}); scripts and other files are not"
        )
    if mode & 0o111:
        return "the file is marked executable; skills hold no scripts"
    if content is not None and content.lstrip("\ufeff").startswith("#!"):
        return "the file starts with an interpreter line (#!); skills hold no scripts"
    return None


def check_path(path: Any) -> str:
    """A reference file's path, relative to the skill: segments of letters, digits, '.', '_' and '-'."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH:
        raise SkillError(f"A reference file path must be 1 to {MAX_PATH} characters.")
    parts = path.split("/")
    if len(parts) > MAX_PATH_DEPTH or not all(PATH_SEGMENT.fullmatch(p) for p in parts):
        raise SkillError(
            f"The reference file path {path!r} is not accepted: use at most {MAX_PATH_DEPTH} "
            "folders and names of letters, digits, '.', '_' and '-', not starting with '.'."
        )
    if any(p in (".", "..") for p in parts):
        raise SkillError(f"The reference file path {path!r} leaves the skill's folder.")
    if path == SKILL_FILE_NAME:
        raise SkillError(f"{SKILL_FILE_NAME} holds the instructions and is not a reference file.")
    return path


def check_file(path: Any, content: Any) -> SkillFile:
    path = check_path(path)
    if not isinstance(content, str):
        raise SkillError(f"{path}: the content must be text.")
    reason = executable_reason(path, content)
    if reason:
        raise SkillError(f"{path}: {reason}.")
    if "\x00" in content:
        raise SkillError(f"{path}: the file is not text.")
    if _size(content) > MAX_FILE_BYTES:
        raise SkillError(
            f"{path} is {_size(content)} bytes; a reference file may be at most {MAX_FILE_BYTES}."
        )
    return SkillFile(path=path, content=content)


def check_metadata(value: Any) -> Dict[str, Any]:
    """The metadata kept with a skill: the known front-matter fields, each checked."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise SkillError("The metadata must be a JSON object.") from None
    if not isinstance(value, Mapping):
        raise SkillError("The metadata must be an object.")
    kept: Dict[str, Any] = {}
    for key, limit in METADATA_TEXT_FIELDS.items():
        if value.get(key) not in (None, ""):
            kept[key] = _plain_text(_scalar_text(value[key]), key, limit)
    for key, (count, limit) in METADATA_LIST_FIELDS.items():
        items = value.get(key)
        if items in (None, "", []):
            continue
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list) or len(items) > count:
            raise SkillError(f"The metadata field {key} must be a list of at most {count} items.")
        kept[key] = [_plain_text(_scalar_text(item), key, limit, required=True) for item in items]
    extra = value.get(METADATA_MAP_FIELD)
    if extra not in (None, "", {}):
        if not isinstance(extra, Mapping) or len(extra) > MAX_METADATA_MAP:
            raise SkillError(
                f"The metadata field {METADATA_MAP_FIELD} must be a map of at most "
                f"{MAX_METADATA_MAP} entries."
            )
        kept[METADATA_MAP_FIELD] = {
            _plain_text(str(k), "metadata key", 64, required=True): _plain_text(
                _scalar_text(v), "metadata value", MAX_METADATA_VALUE
            )
            for k, v in extra.items()
        }
    return kept


def _scalar_text(value: Any) -> Any:
    # YAML reads `version: 1.0` and `icon: 📋` alike; keep scalars as their text.
    if isinstance(value, (int, float, bool)):
        return str(value)
    return value


def check_skill(
    name: Any,
    description: Any,
    instructions: Any,
    *,
    display_name: Any = None,
    files: Optional[Sequence[Any]] = None,
    metadata: Any = None,
    source_format: str = SOURCE_MANUAL,
) -> Skill:
    """A skill from its parts, or SkillError naming the first thing that is not accepted."""
    name = check_name(name)
    description = _plain_text(description, "description", MAX_DESCRIPTION, required=True)
    if not isinstance(instructions, str) or not instructions.strip():
        raise SkillError("A skill needs instructions.")
    if "\x00" in instructions:
        raise SkillError("The instructions are not text.")
    if _size(instructions) > MAX_INSTRUCTIONS_BYTES:
        raise SkillError(
            f"The instructions are {_size(instructions)} bytes; at most "
            f"{MAX_INSTRUCTIONS_BYTES} are accepted."
        )
    checked_files: List[SkillFile] = []
    seen = set()
    for entry in files or []:
        if isinstance(entry, SkillFile):
            checked = check_file(entry.path, entry.content)
        elif isinstance(entry, Mapping):
            checked = check_file(
                entry.get("Path", entry.get("path")), entry.get("Content", entry.get("content"))
            )
        else:
            raise SkillError("A reference file must be an object with a path and its content.")
        if checked.path in seen:
            raise SkillError(f"The reference file {checked.path} is listed more than once.")
        seen.add(checked.path)
        checked_files.append(checked)
    if len(checked_files) > MAX_FILES:
        raise SkillError(f"A skill may have at most {MAX_FILES} reference files.")
    total = _size(instructions) + sum(_size(f.content) for f in checked_files)
    if total > MAX_SKILL_BYTES:
        raise SkillError(
            f"The skill's instructions and reference files are {total} bytes together; at most "
            f"{MAX_SKILL_BYTES} are accepted."
        )
    return Skill(
        name=name,
        description=description,
        instructions=instructions,
        display_name=_plain_text(display_name, "display name", MAX_DISPLAY_NAME),
        files=sorted(checked_files, key=lambda f: f.path),
        metadata=check_metadata(metadata),
        source_format=source_format,
    )


# ── reading skill files ─────────────────────────────────────────────────────


@dataclass
class ParsedSkill:
    """A skill read from a file, with the files the import left out."""

    skill: Skill
    skipped: List[Skipped] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def split_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """The front matter of a Markdown file as a map, and the body after it."""
    match = _FRONT_MATTER.match(text)
    if not match:
        raise SkillError(
            "The file has no front matter: a skill file starts with a '---' block that gives "
            "at least its name."
        )
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"The front matter is not valid YAML: {exc}") from None
    if not isinstance(front, dict):
        raise SkillError("The front matter is not a map of fields.")
    return front, text[match.end() :].lstrip("\r\n")


def _first_paragraph(body: str) -> str:
    """The first paragraph of a Markdown body that is not a heading, a list or a fence."""
    paragraph: List[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith(("#", "```", "-", "*", ">", "|")) and not paragraph:
            continue
        if stripped.startswith(("#", "```")):
            break
        paragraph.append(stripped)
    return " ".join(paragraph)


def is_quick_format(front: Mapping[str, Any]) -> bool:
    """An Amazon Quick skill: Quick's own fields, and no description."""
    return not front.get("description") and any(
        k in front for k in ("display_name", "trigger", "inputs", "icon")
    )


def parse_markdown_skill(text: str, files: Sequence[SkillFile] = ()) -> ParsedSkill:
    """A skill from a ``SKILL.md`` (Agent Skills) or an Amazon Quick skill ``.md``."""
    front, body = split_front_matter(text)
    notes: List[str] = []
    quick = is_quick_format(front)
    raw_name = front.get("name") or front.get("display_name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise SkillError("The front matter gives no name.")
    name = raw_name.strip()
    if not NAME_PATTERN.fullmatch(name) or len(name) > MAX_NAME:
        name = slug(name)
        if not name:
            raise SkillError(f"The name {raw_name!r} has no letters or digits to name a skill by.")
        notes.append(f"The name {raw_name!r} is stored as {name!r}.")
    display_name = front.get("display_name") or ""
    metadata = {
        key: front[key]
        for key in (*METADATA_TEXT_FIELDS, *METADATA_LIST_FIELDS, METADATA_MAP_FIELD)
        if key in front
    }
    if quick:
        description = _first_paragraph(body) or str(display_name or name)
        trigger = front.get("trigger")
        if isinstance(trigger, str) and trigger.strip():
            description = f'{description} Trigger phrase: "{trigger.strip()}".'
        if len(description) > MAX_DESCRIPTION:
            description = description[: MAX_DESCRIPTION - 3].rstrip() + "..."
        notes.append("Read as an Amazon Quick skill: the description is its first paragraph.")
    else:
        description = front.get("description")
    unknown = sorted(
        str(k)
        for k in front
        if k not in ("name", "description", "display_name", "allowed-tools", *metadata)
    )
    if unknown:
        notes.append(f"Front-matter fields not kept: {', '.join(unknown)}.")
    if "allowed-tools" in front:
        notes.append(
            "The allowed-tools field is not kept: the meeting assistant's tools are chosen in "
            "the configuration profile."
        )
    skill = check_skill(
        name,
        description,
        body,
        display_name=display_name,
        files=files,
        metadata=metadata,
        source_format=SOURCE_QUICK if quick else SOURCE_AGENT_SKILLS,
    )
    return ParsedSkill(skill=skill, notes=notes)


def _zip_root(names: Sequence[str]) -> str:
    """The folder of the zip that holds SKILL.md: the root, or its one top-level folder."""
    if SKILL_FILE_NAME in names:
        return ""
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    candidates = [t for t in tops if f"{t}/{SKILL_FILE_NAME}" in names]
    if len(candidates) == 1:
        return candidates[0] + "/"
    if not candidates:
        raise SkillError(
            f"The zip holds no {SKILL_FILE_NAME} at its root or in one top-level folder."
        )
    raise SkillError(f"The zip holds more than one skill folder with a {SKILL_FILE_NAME}.")


def parse_zip_skill(data: bytes) -> ParsedSkill:
    """A skill from a zip of an Agent Skills folder, leaving out what a skill does not hold."""
    if len(data) > MAX_ZIP_BYTES:
        raise SkillError(f"The zip is {len(data)} bytes; at most {MAX_ZIP_BYTES} are read.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("The file is not a zip archive.") from None
    try:
        return _read_zip(archive)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError):
        # A damaged entry, an unsupported compression method or an encrypted file.
        raise SkillError("The zip could not be read.") from None


def _read_zip(archive: zipfile.ZipFile) -> ParsedSkill:
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_ENTRIES:
            raise SkillError(
                f"The zip holds {len(infos)} files; at most {MAX_ZIP_ENTRIES} are read."
            )
        names = [i.filename for i in infos]
        root = _zip_root(names)
        skipped: List[Skipped] = []
        files: List[SkillFile] = []
        skill_text: Optional[str] = None
        for info in infos:
            name = info.filename
            if not name.startswith(root):
                skipped.append(Skipped(name, "outside the skill's folder"))
                continue
            relative = name[len(root) :]
            parts = relative.split("/")
            if name.startswith("__MACOSX/") or any(p.startswith(".") for p in parts):
                skipped.append(Skipped(relative, "a hidden or system file"))
                continue
            if posixpath.isabs(relative) or ".." in parts:
                skipped.append(Skipped(relative, "the path leaves the skill's folder"))
                continue
            mode = (info.external_attr >> 16) & 0o777
            if relative == SKILL_FILE_NAME:
                if info.file_size > MAX_INSTRUCTIONS_BYTES + 8192:
                    raise SkillError(f"{SKILL_FILE_NAME} is larger than a skill may be.")
                skill_text = _decoded(archive.read(info), SKILL_FILE_NAME)
                continue
            reason = executable_reason(relative, None, mode)
            if reason:
                skipped.append(Skipped(relative, reason))
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise SkillError(
                    f"{relative} is {info.file_size} bytes; a reference file may be at most "
                    f"{MAX_FILE_BYTES}."
                )
            try:
                content = _decoded(archive.read(info), relative)
            except SkillError as exc:
                skipped.append(Skipped(relative, str(exc)))
                continue
            reason = executable_reason(relative, content)
            if reason:
                skipped.append(Skipped(relative, reason))
                continue
            try:
                files.append(check_file(relative, content))
            except SkillError as exc:
                skipped.append(Skipped(relative, str(exc)))
    if skill_text is None:
        raise SkillError(f"The zip holds no {SKILL_FILE_NAME}.")
    parsed = parse_markdown_skill(skill_text, files)
    parsed.skipped = skipped
    return parsed


def _decoded(data: bytes, label: str) -> str:
    if b"\x00" in data:
        raise SkillError(f"{label} is not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SkillError(f"{label} is not UTF-8 text") from None


def parse_skill_file(file_name: str, data: bytes) -> ParsedSkill:
    """A skill from an uploaded file: a zip of a skill folder, or a Markdown skill file."""
    lowered = (file_name or "").lower()
    if lowered.endswith(".zip") or data[:4] == b"PK\x03\x04":
        return parse_zip_skill(data)
    if not lowered.endswith((".md", ".markdown")):
        raise SkillError(
            "A skill is imported from a SKILL.md, an Amazon Quick skill .md, or a .zip of a "
            "skill folder."
        )
    if len(data) > MAX_INSTRUCTIONS_BYTES + 8192:
        raise SkillError("The file is larger than a skill may be.")
    return parse_markdown_skill(_decoded(data, file_name or SKILL_FILE_NAME))


# ── writing skill files ─────────────────────────────────────────────────────


def front_matter(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The front matter of a stored skill's SKILL.md."""
    front: Dict[str, Any] = {"name": record.get("Name"), "description": record.get("Description")}
    if record.get("DisplayName"):
        front["display_name"] = record["DisplayName"]
    metadata = record.get("Metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    for key in (*METADATA_TEXT_FIELDS, *METADATA_LIST_FIELDS, METADATA_MAP_FIELD):
        if metadata.get(key) not in (None, "", [], {}):
            front[key] = metadata[key]
    return front


def render_skill_md(record: Mapping[str, Any]) -> str:
    """A stored skill as an Agent Skills ``SKILL.md``."""
    header = yaml.safe_dump(front_matter(record), sort_keys=False, allow_unicode=True, width=1000)
    body = str(record.get("Instructions") or "")
    return f"---\n{header}---\n\n{body}" + ("" if body.endswith("\n") else "\n")


def render_skill_zip(record: Mapping[str, Any]) -> bytes:
    """A stored skill as a zip of its Agent Skills folder, named for the skill."""
    folder = str(record.get("Name") or "skill")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{folder}/{SKILL_FILE_NAME}", render_skill_md(record))
        for entry in record.get("Files") or []:
            archive.writestr(f"{folder}/{entry['Path']}", str(entry.get("Content") or ""))
    return buffer.getvalue()


# ── the library ─────────────────────────────────────────────────────────────


def export_skill(record: Mapping[str, Any]) -> Dict[str, Any]:
    """One stored skill as a library entry."""
    metadata = record.get("Metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    return {
        "skillId": record.get("SkillId"),
        "name": record.get("Name"),
        "displayName": record.get("DisplayName") or "",
        "description": record.get("Description"),
        "instructions": record.get("Instructions") or "",
        "files": [
            {"path": f.get("Path"), "content": f.get("Content") or ""}
            for f in record.get("Files") or []
        ],
        "metadata": dict(metadata),
    }


def export_library(
    records: Sequence[Mapping[str, Any]], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The library document of ``records``, the stored skills, sorted by name."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "kind": LIBRARY_KIND,
        "version": LIBRARY_VERSION,
        "exportedAt": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skills": [
            export_skill(r) for r in sorted(records, key=lambda r: str(r.get("Name") or ""))
        ],
    }


def parse_library(document: Any) -> List[Any]:
    """The skill entries of a library document, or SkillError naming what is wrong."""
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError as exc:
            raise SkillError(f"The file is not valid JSON: {exc}") from None
    if not isinstance(document, Mapping) or document.get("kind") != LIBRARY_KIND:
        raise SkillError(f"The file is not a skill library (kind {LIBRARY_KIND!r}).")
    if document.get("version") != LIBRARY_VERSION:
        raise SkillError(
            f"The library is format version {document.get('version')!r}; "
            f"this release reads version {LIBRARY_VERSION}."
        )
    skills = document.get("skills")
    if not isinstance(skills, list):
        raise SkillError("The library has no list of skills.")
    if len(skills) > MAX_SKILLS:
        raise SkillError(f"The library lists {len(skills)} skills; at most {MAX_SKILLS} are read.")
    return skills


def library_entry(entry: Any) -> Tuple[Skill, List[Skipped], str]:
    """A library entry as a skill, the files left out of it, and the id it asks for."""
    if not isinstance(entry, Mapping):
        raise SkillError("an entry is not an object")
    files = entry.get("files") or []
    if not isinstance(files, list):
        raise SkillError("the files are not a list")
    kept, skipped = [], []
    for item in files:
        path = item.get("path") if isinstance(item, Mapping) else None
        content = item.get("content") if isinstance(item, Mapping) else None
        reason = executable_reason(str(path or ""), content if isinstance(content, str) else None)
        if reason:
            skipped.append(Skipped(str(path or ""), reason))
            continue
        kept.append({"path": path, "content": content})
    skill = check_skill(
        entry.get("name"),
        entry.get("description"),
        entry.get("instructions"),
        display_name=entry.get("displayName"),
        files=kept,
        metadata=entry.get("metadata"),
        source_format=SOURCE_LIBRARY,
    )
    requested = entry.get("skillId")
    return skill, skipped, requested if isinstance(requested, str) else ""


def new_skill_id(name: str, used: set, requested: str = "") -> str:
    """The id a new skill is stored under: the requested one or its name, numbered if taken."""
    if requested and len(requested) <= MAX_SKILL_ID and SKILL_ID_PATTERN.fullmatch(requested):
        if requested not in used:
            return requested
    if name not in used:
        return name
    number = 2
    while f"{name}-{number}" in used:
        number += 1
    return f"{name}-{number}"


@dataclass
class ImportStep:
    """What a library import does with one entry."""

    name: str
    action: str  # "create", "update" or "skip"
    skill_id: str = ""
    skill: Optional[Skill] = None
    skipped: List[Skipped] = field(default_factory=list)
    message: str = ""


def plan_import(document: Any, stored: Sequence[Mapping[str, Any]]) -> List[ImportStep]:
    """What importing ``document`` over the ``stored`` skills does, entry by entry.

    Matched by name: an entry whose name a stored skill has updates that skill
    and keeps its id, so a configuration profile that names it still does; any
    other entry creates a skill, under the entry's id when it is free, up to
    MAX_SKILLS in all. An entry that cannot be read is skipped with the reason,
    and the others are still imported. Reference files that are not text are
    left out of their skill and listed.

    Raises:
        SkillError: the document itself is not a skill library this release reads.
    """
    entries = parse_library(document)
    by_name = {str(s.get("Name")): s for s in stored if s.get("Name")}
    used = {str(s.get("SkillId")) for s in stored if s.get("SkillId")}
    count = len(stored)
    seen: set = set()
    steps: List[ImportStep] = []
    for entry in entries:
        label = str((entry.get("name") if isinstance(entry, Mapping) else None) or "(unnamed)")
        try:
            skill, skipped, requested = library_entry(entry)
        except SkillError as exc:
            steps.append(ImportStep(name=label, action="skip", message=str(exc)))
            continue
        if skill.name in seen:
            steps.append(
                ImportStep(name=skill.name, action="skip", message="listed more than once")
            )
            continue
        seen.add(skill.name)
        existing = by_name.get(skill.name)
        if existing is not None:
            steps.append(
                ImportStep(
                    name=skill.name,
                    action="update",
                    skill_id=str(existing["SkillId"]),
                    skill=skill,
                    skipped=skipped,
                )
            )
            continue
        if count >= MAX_SKILLS:
            steps.append(
                ImportStep(
                    name=skill.name,
                    action="skip",
                    message=f"at most {MAX_SKILLS} skills can be stored",
                )
            )
            continue
        skill_id = new_skill_id(skill.name, used, requested)
        used.add(skill_id)
        count += 1
        steps.append(
            ImportStep(
                name=skill.name, action="create", skill_id=skill_id, skill=skill, skipped=skipped
            )
        )
    return steps
