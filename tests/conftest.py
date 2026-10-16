"""Suite-wide guards."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

PRIVATE_DIR = Path(__file__).resolve().parents[1] / "data" / "private"


@pytest.fixture(autouse=True, scope="session")
def _tests_never_touch_real_private_data() -> Iterator[None]:
    """Fail the run if any test writes into the real ``data/private`` folder."""
    before = {path for path in PRIVATE_DIR.rglob("*")} if PRIVATE_DIR.exists() else set()
    yield
    after = {path for path in PRIVATE_DIR.rglob("*")} if PRIVATE_DIR.exists() else set()
    created = sorted(str(path.relative_to(PRIVATE_DIR)) for path in after - before)
    assert not created, f"tests wrote into data/private: {created}"
