#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PYPI_API_TOKEN:-}" ]]; then
  echo "ERROR: PYPI_API_TOKEN is not set."
  echo "Run: export PYPI_API_TOKEN=\"<your-token>\""
  exit 1
fi

python -m pip install --upgrade build twine

for pkg in lma-sdk lma-cli lma-load-simulator; do
  echo "Uploading placeholder package: $pkg"
  pushd "$pkg" >/dev/null
  rm -rf dist build *.egg-info
  python -m build
  python -m twine upload dist/* -u __token__ -p "$PYPI_API_TOKEN"
  popd >/dev/null
  echo "Uploaded $pkg"
  echo
done

echo "All placeholder packages uploaded successfully."
