# Solution Profile contract (vendored)

The files under [`v1/`](v1/) are **vendored copies** of version 1 of the Solution
Profile contract. LMA's profile in [`solution-profile/`](../) is written against
them. They are kept byte for byte as published and must not be edited here, not even
to reformat them. A change to the contract is made by its owners and then copied in
again whole.

## Provenance

| | |
|---|---|
| What | The Solution Profile contract, version 1: its README, JSON Schema and reference validator |
| Vendored | 2026-11-24, unchanged, from the copy another solution that publishes a profile under the same contract vendored on 2026-11-20 |
| Maintained | Outside this repository, by the contract's owners. Only they change it; this repository takes a new version by copying the files in again |
| Upstream | None published as of 2026-11-24. The digests below pin that these copies are unchanged here, not that they match a public source. The schema's `$id` is a placeholder domain and does not resolve |
| Integrity | The sha256 of each file is pinned below and checked by `scripts/solution_profile/test_solution_profile.py` |

## Files

| File | What it is |
|---|---|
| [`v1/README.md`](v1/README.md) | The contract: the files, their fields and the gap taxonomy |
| [`v1/solution-profile.schema.json`](v1/solution-profile.schema.json) | The exact rules, as JSON Schema 2020-12 |
| [`v1/validate_profile.py`](v1/validate_profile.py) | The reference validator |

The repository's Python lint does not read `v1/validate_profile.py`: `make
lint-python-check` covers named paths only, and this directory is not one of them.
What keeps the copy honest is the hash pin below.

<!-- vendored-sha256:start -->
```text
efb5561fd72e1edbf7c3b2bb3bfabe2c1dd08e9877f60bb01b04510976fee327  v1/README.md
ea6286ebfe79261811c3ec8203bf3d08da6c87f8b8471efc02586895c971b158  v1/solution-profile.schema.json
fb2d437d50e2c6df675797a5fdb7d20babcc2700deee2f40c94b79815ecd08d4  v1/validate_profile.py
```
<!-- vendored-sha256:end -->

To update the copies after the contract changes, copy the three files over these, run
`sha256sum v1/README.md v1/solution-profile.schema.json v1/validate_profile.py` from
this directory, paste the result between the markers above, and run
`make solution-profile-check`.

## Points to raise with the contract's owners

These are recorded here rather than worked around by editing the copy.

1. **Development versions.** The contract's version pattern is `X.Y.Z` with an
   optional `-suffix` or `+suffix`. LMA's `VERSION` holds a PEP 440 version, which
   on `develop` is a development build such as `0.4.1.dev1`, and the pattern
   rejects that form, so `--version-file VERSION` cannot be passed as it stands on
   a development build. `scripts/solution_profile/check.py` gives the validator a
   temporary file holding the contract form (`0.4.1-dev1`), and the manifest
   carries the same form. A final release is already `X.Y.Z` and is passed as
   `VERSION` itself.
2. **Anchors.** The validator checks that an evidence path exists, not that its
   `#anchor` names a heading in that file. LMA's check adds that test for Markdown
   evidence.
3. **How the validator is run.** Its shebang is `uv run --script`, with its two
   dependencies declared inline. LMA's CI has no `uv`; the check loads the
   validator's own `main` in process, with PyYAML and jsonschema from the
   repository's Python environment.

The tarball published with each release does not contain this directory; it holds
the profile files, its skill and `build.json`. See
[docs/solution-profile.md](../../docs/solution-profile.md).
