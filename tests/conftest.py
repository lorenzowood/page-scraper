from __future__ import annotations

from pathlib import Path

import pytest

from pagecapture.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    yield database
    await database.close()
