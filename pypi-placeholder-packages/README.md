# PyPI placeholder packages

These packages are reserved on public PyPI to prevent dependency confusion for internal-only LMA names.

## Reserved names

- `lma-sdk`
- `lma-cli`
- `lma-load-simulator`

## Build and upload

Install build tools and upload each package with the PyPI API token you created:

```bash
python -m pip install --upgrade build twine
cd pypi-placeholder-packages/lma-sdk
python -m build
python -m twine upload dist/* -u __token__ -p "$PYPI_API_TOKEN"

cd ../lma-cli
python -m build
python -m twine upload dist/* -u __token__ -p "$PYPI_API_TOKEN"

cd ../lma-load-simulator
python -m build
python -m twine upload dist/* -u __token__ -p "$PYPI_API_TOKEN"
```

## Environment

Set the token in your shell before uploading:

```bash
export PYPI_API_TOKEN="<your-pypi-api-token>"
```

Do not commit the token to source control.
