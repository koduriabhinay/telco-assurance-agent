import os

import pytest

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture(autouse=True)
def fresh_store():
    """Every test starts from the seeded mock network with all alarms active."""
    from app.db import reset_store

    yield reset_store()
