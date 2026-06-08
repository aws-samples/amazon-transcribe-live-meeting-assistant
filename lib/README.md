# LMA internal Python packages

This directory contains Python packages that ship **inside this repository
only**. They are **not published to public PyPI**:

| Package | Path | Distribution name |
|---------|------|-------------------|
| LMA SDK | `lib/lma_sdk` | `lma-sdk` |
| LMA CLI | `lib/lma_cli_pkg` | `lma-cli` |

> ⚠️ **Security — dependency confusion.** Because these names are not registered
> on public PyPI, a bare `pip install lma-cli` / `pip install lma-sdk` could
> resolve a **third-party** package of the same name from PyPI. The LMA CLI runs
> in environments holding Bedrock and administrator credentials, so always
> install from the local path, never from PyPI.

## Installation

Install the SDK **first**, then the CLI, both from the local path:

```bash
# From the repository root
pip install -e lib/lma_sdk
pip install -e lib/lma_cli_pkg

# Or, equivalently:
make setup-cli
```

For dev/test extras use `make setup-cli-dev` or
`pip install -e "lib/lma_sdk[dev]"`.

The related load simulator (`utilities/load-simulator`, dist name
`lma-load-simulator`) is also internal-only and depends on `lma-sdk`; install it
the same way:

```bash
pip install -e utilities/load-simulator
```

The `lma-cli`, `lma-load-simulator`, and `lma-sdk` `pyproject.toml` files use a
`[tool.uv.sources]` override so that `uv`-based resolvers pin `lma-sdk` to the
in-repo path rather than public PyPI.
