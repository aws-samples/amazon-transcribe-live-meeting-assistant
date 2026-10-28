# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Every log destination in every deployed template, checked statically (no AWS).

A Lambda function with no `LoggingConfig` still logs: Lambda creates
`/aws/lambda/<function-name>` on the first invocation, with no retention and the
AWS-owned key, and CloudFormation neither owns nor deletes it. A MicroVM image
whose `Logging.CloudWatch.LogGroup` names no declared group, and a CodeBuild
project with no `LogsConfig.CloudWatchLogs.GroupName`, are the same story with a
different service. So is a declared log group that nothing points at —
that one sits empty while the logs it was declared for go elsewhere. All three
are invisible until somebody goes looking for a log, or reads a bill, so this
file asserts the wiring instead. The image's setting covers only its build: a
running MicroVM logs to the group its RunMicrovm call names, so the last section
checks that each launcher function is handed the image's group by name.

`EMITTER_LOG_GROUP_PATHS` is the set of resource types that name their own log
group, and where each names it. The five rules, each as its own test so a
failure names which one broke:

1. every emitter names a log group declared in the same template;
2. every `AWS::Logs::LogGroup` sets a non-null `RetentionInDays`;
3. no `LogGroupName` is derived from the resource that logs to it, in any
   intrinsic form. `!Sub "/aws/lambda/${SomeFunction}"` resolves the function's
   generated physical name, which makes the group depend on the function.
   CloudFormation then creates the function first, and anything that invokes it
   in that window lets Lambda create the group — after which the stack's own
   group fails to create, because the name is taken;
4. a log group's `Condition` matches the `Condition` of the resource that names
   it, so a conditional emitter cannot end up pointing at a group that was not
   created (or vice versa);
5. every declared log group is referenced by something.

Scoped to the templates `lma-main.yaml` actually instantiates, read from the
`CFN_TEMPLATES` list in the root `Makefile` so the two cannot drift. There is no
per-resource exemption list: a new emitter that does not comply fails rule 1.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO_ROOT / "lma-main.yaml"
MAKEFILE = REPO_ROOT / "Makefile"

LOG_GROUP_TYPE = "AWS::Logs::LogGroup"

# Resource types that emit to a log group they name, and the property path each
# one names it under. A MicroVM image is in here for the same reason a function
# is: `Logging.CloudWatch.LogGroup` is a destination name, so without a declared
# group the service creates one, with no expiry and the AWS-owned key — and the
# MicroVM groups carry the highest-volume output in the solution.
EMITTER_LOG_GROUP_PATHS = {
    "AWS::Lambda::Function": ("LoggingConfig", "LogGroup"),
    "AWS::Serverless::Function": ("LoggingConfig", "LogGroup"),
    "AWS::Lambda::MicrovmImage": ("Logging", "CloudWatch", "LogGroup"),
    # Without LogsConfig a project writes to /aws/codebuild/<project>, which
    # CodeBuild creates with no expiry and the AWS-owned key.
    "AWS::CodeBuild::Project": ("LogsConfig", "CloudWatchLogs", "GroupName"),
}
EMITTER_TYPES = tuple(EMITTER_LOG_GROUP_PATHS)

# `CFN_TEMPLATES` is written with make variables for the longer directories.
MAKE_VARIABLES = {
    "$(AI_STACK_DIR)": "lma-ai-stack",
    "$(ASR_DIR)": "lma-asr-microvm-stack",
    "$(VP_DIR)": "lma-virtual-participant-stack",
    "$(WEBSOCKET_DIR)": "lma-websocket-transcriber-stack",
}

# Linted, but instantiated by no template, so nothing in them reaches a
# deployment. They are exempt from the rules below as whole templates rather
# than per-resource, and `test_the_exempt_templates_are_still_not_deployed`
# keeps that honest: wiring either one in anywhere fails that test, which is the
# point at which its log destinations have to comply. The value of each entry is
# the string whose absence from every other template is the evidence.
TEMPLATES_NOT_DEPLOYED = {
    "lma-ai-stack/deployment/virtual-participant-enhancements.yaml": (
        "virtual-participant-enhancements"
    ),
    "lma-bedrockagent-stack/template.yaml": "lma-bedrockagent-stack",
}


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _cfn_templates() -> list[str]:
    """The `CFN_TEMPLATES` list from the root Makefile, as repo-relative paths."""
    body = MAKEFILE.read_text().split("CFN_TEMPLATES := \\", 1)[1]
    paths = []
    for line in body.splitlines():
        entry = line.strip().rstrip("\\").strip()
        if not entry:
            if paths:  # the list ends at the first blank line after it
                break
            continue  # the remainder of the `CFN_TEMPLATES := \` line itself
        for variable, directory in MAKE_VARIABLES.items():
            entry = entry.replace(variable, directory)
        paths.append(entry)
    return paths


ALL_TEMPLATES = _cfn_templates()
DEPLOYED_TEMPLATES = [path for path in ALL_TEMPLATES if path not in TEMPLATES_NOT_DEPLOYED]


def _load(path: str) -> dict:
    return yaml.load((REPO_ROOT / path).read_text(), Loader=_CfnLoader)


def _resources(path: str) -> dict:
    return _load(path).get("Resources", {}) or {}


def _of_type(resources: dict, *types: str) -> dict:
    return {k: v for k, v in resources.items() if v.get("Type") in types}


def _references(node, path: str = ""):
    """Yield (target, path) for every Ref / GetAtt / Sub variable in `node`."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                yield (value, path)
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) else str(value).split(".")[0]
                if isinstance(target, str):
                    yield (target, path)
            elif key == "Fn::Sub":
                body = value[0] if isinstance(value, list) else value
                if isinstance(body, str):
                    for variable in re.findall(r"\$\{([A-Za-z0-9:.]+)\}", body):
                        yield (variable.split(".")[0], path)
            yield from _references(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _references(value, f"{path}[{index}]")


def _log_group_ref(emitter: dict) -> object:
    """Whatever the emitter names as its log group, by its own type's path."""
    node = emitter.get("Properties", {}) or {}
    for key in EMITTER_LOG_GROUP_PATHS[emitter["Type"]]:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node


def _as_name(value: object) -> str | None:
    """The name a `LogGroupName` / `LoggingConfig.LogGroup` scalar spells out.

    `${...}` placeholders are left in place: both ends of the comparison are
    written against the same pseudo-parameters, so comparing the unexpanded
    strings is exact. Anything that is not a plain string or a single `Fn::Sub`
    of one has no name to compare and yields None.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Fn::Sub"}:
        body = value["Fn::Sub"]
        if isinstance(body, str):
            return body
    return None


def _destination(emitter: dict, log_groups: dict) -> str | None:
    """The logical id of the log group an emitter logs to, or None if it is not one.

    Two spellings are in use and both are accepted: `!Ref <group>`, and the
    group's name repeated verbatim (the older and still more common form in
    these templates). The second only counts when some declared group really
    carries that name — which is what makes a typo in it a failure here.
    """
    reference = _log_group_ref(emitter)
    if isinstance(reference, dict) and set(reference) == {"Ref"}:
        return reference["Ref"] if reference["Ref"] in log_groups else None
    wanted = _as_name(reference)
    if wanted is None:
        return None
    for logical_id, group in log_groups.items():
        if _as_name(group.get("Properties", {}).get("LogGroupName")) == wanted:
            return logical_id
    return None


# ── the template list itself ────────────────────────────────────────────────


def test_the_template_list_parses_at_the_expected_scale():
    """Guards every parametrized test below against collecting nothing."""
    assert len(ALL_TEMPLATES) >= 16, f"only found {len(ALL_TEMPLATES)} templates in CFN_TEMPLATES"
    missing = [path for path in ALL_TEMPLATES if not (REPO_ROOT / path).is_file()]
    assert not missing, f"CFN_TEMPLATES names templates that do not exist: {missing}"


def test_every_emitter_type_is_actually_collected_somewhere():
    """Each type in `EMITTER_LOG_GROUP_PATHS` must match a resource that exists.

    A type dropped from that mapping, or a resource type renamed, silently
    removes a whole class of log destination from all five rules, which is not
    visible as a failure anywhere else — every remaining test would still pass.
    The MicroVM images are the case this guards: drop `AWS::Lambda::MicrovmImage`
    from the mapping and both of them go ungated, taking the two
    highest-volume log groups in the solution with them, and nothing else here
    would notice.
    """
    found = {emitter_type: 0 for emitter_type in EMITTER_LOG_GROUP_PATHS}
    for path in DEPLOYED_TEMPLATES:
        for resource in _resources(path).values():
            if resource.get("Type") in found:
                found[resource["Type"]] += 1
    uncovered = sorted(name for name, count in found.items() if count == 0)
    assert not uncovered, (
        f"these emitter types match no resource in any deployed template, so "
        f"listing them gates nothing: {uncovered}. Either the resource type was "
        f"renamed or the resources were removed — do not just delete the entry."
    )


def test_cfn_templates_covers_every_nested_stack_lma_main_deploys():
    """A nested stack missing from the list is linted by nothing and tested by nothing.

    Compared by stack directory rather than by file, because the S3 key in a
    `TemplateURL` is the published layout (`lma-cognito-stack/template.yaml`),
    not the repo layout (`lma-cognito-stack/deployment/lma-cognito-stack.yaml`).
    """
    urls = re.findall(
        r"TemplateURL:\s*\S*<ARTIFACT_PREFIX_TOKEN>/([^/\s]+)/", MAIN_TEMPLATE.read_text()
    )
    assert len(urls) >= 13, f"only found {len(urls)} nested stack TemplateURLs — regex stale?"
    listed = {path.split("/", 1)[0] for path in ALL_TEMPLATES}
    missing = sorted(set(urls) - listed)
    assert not missing, (
        f"lma-main.yaml deploys these stacks but CFN_TEMPLATES does not list their "
        f"templates, so they are neither linted nor covered here: {missing}"
    )


def test_the_exempt_templates_are_still_not_deployed():
    """The exemption in `TEMPLATES_NOT_DEPLOYED` holds only while that stays true.

    Every other template is searched, not just lma-main.yaml: nesting is two deep
    here, so an AI-stack sub-template would be instantiated from
    lma-ai-stack.yaml and scanning only the root would never see it.
    """
    now_deployed = []
    for path, evidence in TEMPLATES_NOT_DEPLOYED.items():
        for other in ALL_TEMPLATES:
            if other == path:
                continue  # a template naming itself proves nothing
            if evidence in (REPO_ROOT / other).read_text():
                now_deployed.append(f"{path} (named in {other})")
    assert not now_deployed, (
        f"these templates are exempt from the log-destination rules because no "
        f"template instantiates them, which is no longer true: {sorted(now_deployed)}. "
        f"Remove them from TEMPLATES_NOT_DEPLOYED and wire their log destinations "
        f"to declared log groups."
    )


# ── the five rules ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_every_emitter_names_a_log_group_declared_in_the_same_template(template: str):
    """Rule 1. Without this, the service creates the group and sets its own defaults."""
    resources = _resources(template)
    log_groups = _of_type(resources, LOG_GROUP_TYPE)
    broken = []
    for name, emitter in _of_type(resources, *EMITTER_TYPES).items():
        path = ".".join(EMITTER_LOG_GROUP_PATHS[emitter["Type"]])
        reference = _log_group_ref(emitter)
        if reference is None:
            broken.append(f"{name}: no {path}")
        elif _destination(emitter, log_groups) is None:
            broken.append(f"{name}: {path} ({reference!r}) is not a log group declared here")
    assert not broken, f"{template}: {broken}"


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_every_log_group_sets_a_retention(template: str):
    """Rule 2. A log group with no `RetentionInDays` keeps its events forever."""
    without = [
        name
        for name, group in _of_type(_resources(template), LOG_GROUP_TYPE).items()
        if group.get("Properties", {}).get("RetentionInDays") is None
    ]
    assert not without, f"{template}: log groups with no RetentionInDays: {without}"


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_no_log_group_name_is_derived_from_its_emitter(template: str):
    """Rule 3. See the module docstring for why this ordering matters."""
    resources = _resources(template)
    emitters = _of_type(resources, *EMITTER_TYPES)
    derived = []
    for name, group in _of_type(resources, LOG_GROUP_TYPE).items():
        group_name = group.get("Properties", {}).get("LogGroupName")
        for target, _ in _references(group_name):
            if target in emitters:
                derived.append(f"{name} -> {target}")
    assert not derived, (
        f"{template}: LogGroupName derived from the resource that logs to it, which "
        f"makes the group depend on that resource: {derived}"
    )


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_a_log_groups_condition_matches_its_emitters(template: str):
    """Rule 4. A mismatch creates one of the pair without the other."""
    resources = _resources(template)
    log_groups = _of_type(resources, LOG_GROUP_TYPE)
    mismatched = []
    for name, emitter in _of_type(resources, *EMITTER_TYPES).items():
        destination = _destination(emitter, log_groups)
        if destination is None:
            continue  # rule 1 reports this
        group = log_groups[destination]
        if emitter.get("Condition") != group.get("Condition"):
            mismatched.append(
                f"{name} (Condition: {emitter.get('Condition')}) -> "
                f"{destination} (Condition: {group.get('Condition')})"
            )
    assert not mismatched, (
        f"{template}: condition mismatch between a resource and its log group: {mismatched}"
    )


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_every_declared_log_group_is_referenced(template: str):
    """Rule 5. An unreferenced group stays empty while its logs go elsewhere.

    `DependsOn` deliberately does NOT count as a reference. Every emitter here is
    required to declare `DependsOn: [<its>LogGroup]`, so counting it would make
    the rule unfalsifiable: a function cross-wired to a *different* group's name
    leaves its own group genuinely orphaned, and the surviving `DependsOn` alone
    would satisfy this. Only a real use counts -- a `Ref`/`GetAtt`/`Sub` from
    somewhere other than the group itself (which is how the ECS container groups
    and the Step Functions group are reached), an output, or an emitter whose
    log destination resolves to it.
    """
    loaded = _load(template)
    resources = loaded.get("Resources", {}) or {}
    log_groups = _of_type(resources, LOG_GROUP_TYPE)
    referenced = set()
    for name, resource in resources.items():
        if name in log_groups:
            continue  # a group referring to itself does not make it used
        referenced |= {target for target, _ in _references(resource)}
    referenced |= {target for target, _ in _references(loaded.get("Outputs", {}) or {})}
    # An emitter naming its group by repeating the name counts as using it.
    referenced |= {
        destination
        for emitter in _of_type(resources, *EMITTER_TYPES).values()
        if (destination := _destination(emitter, log_groups)) is not None
    }
    orphans = sorted(set(log_groups) - referenced)
    assert not orphans, (
        f"{template}: log groups nothing points at, so they stay empty while the "
        f"logs they were declared for go to a group this template does not own: {orphans}"
    )


# ── MicroVM runtime output ──────────────────────────────────────────────────

MICROVM_IMAGE_TYPE = "AWS::Lambda::MicrovmImage"


def _image_launchers(resources: dict) -> dict:
    """{function logical id: image logical id} for every function that runs MicroVMs.

    A launcher is recognised by the image ARN it is handed, `MICROVM_IMAGE_ARN:
    !GetAtt <image>.ImageArn`, which is the value it passes to RunMicrovm.
    """
    images = _of_type(resources, MICROVM_IMAGE_TYPE)
    launchers = {}
    for name, function in _of_type(
        resources, "AWS::Lambda::Function", "AWS::Serverless::Function"
    ).items():
        env = function.get("Properties", {}).get("Environment", {}).get("Variables", {}) or {}
        targets = [t for t, _ in _references(env.get("MICROVM_IMAGE_ARN")) if t in images]
        if targets:
            launchers[name] = targets[0]
    return launchers


def test_microvm_launchers_are_found():
    """Guards the test below against passing because it collected nothing."""
    found = {}
    for path in DEPLOYED_TEMPLATES:
        launchers = _image_launchers(_resources(path))
        if launchers:
            found[path] = sorted(launchers)
    assert found == {
        "lma-virtual-participant-stack/template.yaml": ["VPMicrovmLauncherFunction"],
        "lma-asr-microvm-stack/template.yaml": ["AsrLauncherFunction"],
    }


@pytest.mark.parametrize("template", DEPLOYED_TEMPLATES)
def test_every_microvm_launcher_names_its_images_log_group(template: str):
    """A running MicroVM logs to the group its RunMicrovm call names.

    The image's `Logging.CloudWatch.LogGroup` covers only the build, snapshot and
    validate output. A RunMicrovm call that names no group sends the MicroVM's
    own output to a group the service creates after the image, with no expiry
    and the AWS-owned key. The launcher reads the name from
    `MICROVM_LOG_GROUP_NAME`, so that variable has to be a `Ref` to the same
    declared group the image logs to.
    """
    resources = _resources(template)
    log_groups = _of_type(resources, LOG_GROUP_TYPE)
    broken = []
    for function_name, image_name in _image_launchers(resources).items():
        expected = _destination(resources[image_name], log_groups)
        env = resources[function_name]["Properties"]["Environment"]["Variables"]
        actual = env.get("MICROVM_LOG_GROUP_NAME")
        if expected is None or actual != {"Ref": expected}:
            broken.append(
                f"{function_name}: MICROVM_LOG_GROUP_NAME is {actual!r}, expected "
                f"a Ref to {expected} (the group {image_name} logs to)"
            )
    assert not broken, f"{template}: {broken}"
