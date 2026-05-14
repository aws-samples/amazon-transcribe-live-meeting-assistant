# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Persist synthetic-user tokens to disk so long-running scenarios can
survive restarts / replays without re-minting Cognito users."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(os.path.expanduser("~/.lma-load"))


class TokenCache:
    """JSON file cache keyed by ``run_id``.

    File layout::

        ~/.lma-load/<runId>/users.json
    """

    def __init__(self, run_id: str, cache_dir: Path | None = None) -> None:
        self.run_id = run_id
        self.cache_dir = (cache_dir or DEFAULT_CACHE_DIR) / run_id
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.cache_dir / "users.json"

    def save(self, users: list[Any]) -> None:
        """Serialize a list of CognitoUser-like objects to the cache file."""
        data = [
            {
                "username": u.username,
                "email": u.email,
                "password": u.password,
                "is_admin": u.is_admin,
                "idx": u.idx,
                "run_id": u.run_id,
                "id_token": u.id_token,
                "access_token": u.access_token,
                "refresh_token": u.refresh_token,
                "token_expiry": u.token_expiry,
            }
            for u in users
        ]
        # 0600 perms so passwords aren't world-readable.
        self.file.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass
        logger.info("Saved %d users to %s", len(users), self.file)

    def load(self) -> list[dict[str, Any]]:
        if not self.file.exists():
            return []
        return json.loads(self.file.read_text())

    def purge(self) -> None:
        if self.file.exists():
            self.file.unlink()
        try:
            self.cache_dir.rmdir()
        except OSError:
            pass
