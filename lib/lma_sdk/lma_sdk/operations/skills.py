# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""The meeting assistant's skills: create, read, change, delete, import and export them.

Available as ``client.skills``::

    client.skills.create(
        name="launch-review",
        description="Review a launch plan against the checklist.",
        instructions=Path("SKILL-body.md").read_text(),
        files={"reference/checklist.md": Path("checklist.md").read_text()},
    )
    client.skills.import_file("launch-review.zip")          # or a SKILL.md / Quick .md
    client.skills.export("launch-review", "skill.zip", file_format="zip")

    library = client.skills.export_library()                 # every skill, as a dict
    other_client.skills.import_library(library)              # matched by name

A skill holds instructions and text reference files only: an import leaves out
scripts and other files that are not text, and says which. Every call is signed
with the caller's AWS credentials (IAM); the stack also accepts the writes from
its Admin group.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Union

from lma_sdk.exceptions import LMAAppSyncError

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

#: The library document's kind and the format version this SDK writes and reads.
LIBRARY_KIND = "lma-skill-library"
LIBRARY_VERSION = 1
#: The largest library document sent in one import request; a larger library is
#: sent in parts, which an import by name makes equivalent to sending it whole.
MAX_IMPORT_BYTES = 512 * 1024

SKILL_FIELDS = """
    SkillId Name DisplayName Description Instructions
    Files { Path Content Size }
    Metadata SourceFormat CreatedAt CreatedBy UpdatedAt UpdatedBy
"""
SUMMARY_FIELDS = """
    SkillId Name DisplayName Description Files { Path Size }
    Metadata SourceFormat CreatedAt CreatedBy UpdatedAt UpdatedBy
"""

LIST_QUERY = f"query ListSkills {{ listSkills {{ {SUMMARY_FIELDS} }} }}"
GET_QUERY = f"query GetSkill($skillId: ID!) {{ getSkill(skillId: $skillId) {{ {SKILL_FIELDS} }} }}"
EXPORT_QUERY = (
    "query ExportSkill($skillId: ID!, $format: String) "
    "{ exportSkill(skillId: $skillId, format: $format) }"
)
EXPORT_LIBRARY_QUERY = "query ExportSkillLibrary { exportSkillLibrary }"
OUTPUT = f"SkillId Success Message Skill {{ {SKILL_FIELDS} }}"
CREATE_MUTATION = (
    f"mutation CreateSkill($input: SkillInput!) {{ createSkill(input: $input) {{ {OUTPUT} }} }}"
)
UPDATE_MUTATION = (
    "mutation UpdateSkill($skillId: ID!, $input: SkillInput!) "
    f"{{ updateSkill(skillId: $skillId, input: $input) {{ {OUTPUT} }} }}"
)
DELETE_MUTATION = (
    "mutation DeleteSkill($skillId: ID!) { deleteSkill(skillId: $skillId) { SkillId Success Message } }"
)
IMPORT_MUTATION = """
mutation ImportSkill($fileName: String!, $content: String!, $encoding: String, $overwrite: Boolean) {
  importSkill(fileName: $fileName, content: $content, encoding: $encoding, overwrite: $overwrite) {
    Success Message SkillId Name Action Skipped { Path Reason } Notes
  }
}
"""
IMPORT_LIBRARY_MUTATION = """
mutation ImportSkillLibrary($library: AWSJSON!) {
  importSkillLibrary(library: $library) {
    Success Message Skills { Name SkillId Action Message }
  }
}
"""

FilesArg = Union[Mapping[str, str], List[Mapping[str, str]], None]


def _files(files: FilesArg) -> List[Dict[str, str]]:
    if files is None:
        return []
    if isinstance(files, Mapping):
        return [{"Path": path, "Content": content} for path, content in files.items()]
    return [
        {"Path": f.get("Path", f.get("path")), "Content": f.get("Content", f.get("content"))}
        for f in files
    ]


def _json(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


def _with_metadata(skill: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if skill and isinstance(skill.get("Metadata"), str):
        skill = {**skill, "Metadata": json.loads(skill["Metadata"] or "{}")}
    return skill


def check_library(library: Union[Dict[str, Any], str]) -> Dict[str, Any]:
    """``library`` as a document, or ValueError when it is not a skill library of this format."""
    document = json.loads(library) if isinstance(library, str) else library
    if not isinstance(document, dict) or document.get("kind") != LIBRARY_KIND:
        raise ValueError(f"not a skill library (kind {LIBRARY_KIND!r})")
    if document.get("version") != LIBRARY_VERSION:
        raise ValueError(
            f"the library is format version {document.get('version')!r}; "
            f"this SDK reads version {LIBRARY_VERSION}"
        )
    if not isinstance(document.get("skills"), list):
        raise ValueError("the library has no list of skills")
    return document


def library_parts(document: Dict[str, Any], max_bytes: Optional[int] = None) -> List[Dict[str, Any]]:
    """``document`` split into libraries of whole skills, each at most ``max_bytes`` of JSON.

    ``max_bytes`` defaults to MAX_IMPORT_BYTES. A single skill larger than that
    is sent on its own.
    """
    max_bytes = MAX_IMPORT_BYTES if max_bytes is None else max_bytes
    header = {k: v for k, v in document.items() if k != "skills"}
    parts: List[Dict[str, Any]] = []
    current: List[Any] = []
    size = len(json.dumps({**header, "skills": []}))
    for skill in document["skills"]:
        entry = len(json.dumps(skill)) + 2
        if current and size + entry > max_bytes:
            parts.append({**header, "skills": current})
            current, size = [], len(json.dumps({**header, "skills": []}))
        current.append(skill)
        size += entry
    parts.append({**header, "skills": current})
    return parts


class SkillOperations:
    """The meeting assistant's skills, as ``client.skills``."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client

    def _call(self, query: str, operation: str, **variables: Any) -> Dict[str, Any]:
        return self._client.appsync.graphql(
            query, variables=variables or None, operation_name=operation
        )

    @staticmethod
    def _result(data: Dict[str, Any], field: str) -> Dict[str, Any]:
        result = data.get(field) or {}
        if not result.get("Success"):
            raise LMAAppSyncError(result.get("Message") or f"{field} failed")
        if "Skill" in result:
            result = {**result, "Skill": _with_metadata(result.get("Skill"))}
        return result

    # ── reads ────────────────────────────────────────────────────

    def list(self) -> List[Dict[str, Any]]:
        """Every skill, without its instructions or file contents, sorted by name."""
        data = self._call(LIST_QUERY, "ListSkills")
        return [_with_metadata(s) for s in data.get("listSkills") or []]

    def get(self, skill_id: str) -> Optional[Dict[str, Any]]:
        """One skill with its instructions and reference files, or None."""
        data = self._call(GET_QUERY, "GetSkill", skillId=skill_id)
        return _with_metadata(data.get("getSkill"))

    def export(
        self, skill_id: str, path: Union[str, Path, None] = None, file_format: str = "skill-md"
    ) -> Dict[str, Any]:
        """One skill as a ``SKILL.md`` (``skill-md``) or a zip of its folder (``zip``).

        Returns the stack's answer: ``fileName``, ``encoding`` and ``content``
        (base64 for a zip). With ``path``, the file is also written there.
        """
        data = self._call(EXPORT_QUERY, "ExportSkill", skillId=skill_id, format=file_format)
        answer = _json(data.get("exportSkill"))
        if not isinstance(answer, dict) or "content" not in answer:
            raise LMAAppSyncError("exportSkill did not return a skill file")
        if path is not None:
            target = Path(path)
            if answer.get("encoding") == "base64":
                target.write_bytes(base64.b64decode(answer["content"]))
            else:
                target.write_text(answer["content"], encoding="utf-8")
        return answer

    def export_library(self) -> Dict[str, Any]:
        """Every skill as a library document."""
        data = self._call(EXPORT_LIBRARY_QUERY, "ExportSkillLibrary")
        document = _json(data.get("exportSkillLibrary"))
        if not isinstance(document, dict) or document.get("kind") != LIBRARY_KIND:
            raise LMAAppSyncError("exportSkillLibrary did not return a skill library")
        return document

    # ── writes ───────────────────────────────────────────────────

    @staticmethod
    def _input(
        name: str,
        description: str,
        instructions: str,
        display_name: Optional[str],
        files: FilesArg,
        metadata: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "Name": name,
            "DisplayName": display_name,
            "Description": description,
            "Instructions": instructions,
            "Files": _files(files),
            "Metadata": json.dumps(dict(metadata or {})),
        }

    def create(
        self,
        name: str,
        description: str,
        instructions: str,
        *,
        display_name: Optional[str] = None,
        files: FilesArg = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a skill. ``files`` maps each reference file's path to its text.

        Raises:
            LMAAppSyncError: the stack refused it (the message says why).
        """
        data = self._call(
            CREATE_MUTATION,
            "CreateSkill",
            input=self._input(name, description, instructions, display_name, files, metadata),
        )
        return self._result(data, "createSkill")

    def update(
        self,
        skill_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        instructions: Optional[str] = None,
        display_name: Optional[str] = None,
        files: FilesArg = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Change a skill. A part not given keeps its stored value."""
        current = self.get(skill_id)
        if current is None:
            raise LMAAppSyncError(f"no skill {skill_id!r}")
        data = self._call(
            UPDATE_MUTATION,
            "UpdateSkill",
            skillId=skill_id,
            input=self._input(
                name if name is not None else current["Name"],
                description if description is not None else current["Description"],
                instructions if instructions is not None else current.get("Instructions") or "",
                display_name if display_name is not None else current.get("DisplayName"),
                files if files is not None else current.get("Files") or [],
                metadata if metadata is not None else current.get("Metadata") or {},
            ),
        )
        return self._result(data, "updateSkill")

    def delete(self, skill_id: str) -> Dict[str, Any]:
        """Delete a skill."""
        return self._result(self._call(DELETE_MUTATION, "DeleteSkill", skillId=skill_id), "deleteSkill")

    def import_file(
        self,
        path: Union[str, Path],
        *,
        overwrite: bool = False,
        content: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Import one skill from a ``SKILL.md``, an Amazon Quick skill ``.md`` or a ``.zip`` folder.

        ``overwrite`` replaces a skill of the same name. ``content`` sends those
        bytes under ``path``'s name instead of reading the file. Returns the
        stack's answer, with ``Skipped`` listing the files left out and why.

        Raises:
            LMAAppSyncError: the stack refused the file (the message says why).
        """
        name = Path(path).name
        data = content if content is not None else Path(path).read_bytes()
        is_zip = name.lower().endswith(".zip") or data[:4] == b"PK\x03\x04"
        result = self._call(
            IMPORT_MUTATION,
            "ImportSkill",
            fileName=name,
            content=base64.b64encode(data).decode("ascii") if is_zip else data.decode("utf-8"),
            encoding="base64" if is_zip else "utf-8",
            overwrite=overwrite,
        )
        return self._result(result, "importSkill")

    def import_library(self, library: Union[Dict[str, Any], str]) -> Dict[str, Any]:
        """Create or update the skills ``library`` lists, matched by name.

        A large library is sent in parts of whole skills; the answer is the
        parts' answers together.

        Raises:
            ValueError: ``library`` is not a skill library of this format.
        """
        document = check_library(library)
        combined: Dict[str, Any] = {"Success": True, "Skills": []}
        for part in library_parts(document):
            data = self._call(
                IMPORT_LIBRARY_MUTATION, "ImportSkillLibrary", library=json.dumps(part)
            )
            answer = data.get("importSkillLibrary") or {}
            combined["Success"] = combined["Success"] and bool(answer.get("Success"))
            combined["Skills"].extend(answer.get("Skills") or [])
            if not answer.get("Success") and not answer.get("Skills"):
                combined["Message"] = answer.get("Message") or "The import failed"
                return combined
        processed = sum(1 for s in combined["Skills"] if s.get("Action") != "failed")
        combined["Message"] = f"{processed} of {len(combined['Skills'])} skills processed"
        return combined
