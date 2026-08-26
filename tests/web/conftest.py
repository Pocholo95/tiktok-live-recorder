import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db.database import init_db  # noqa: E402
from web.app import create_app  # noqa: E402


class _StubSupervisor:
    """Minimal stand-in for ChannelSupervisor: the web layer only ever
    reads status from it and stops it on shutdown."""

    def get_status(self, channel_id):
        return None

    def get_all_statuses(self):
        return {}

    def stop(self, wait=True):
        pass


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return create_app(db_path, _StubSupervisor(), output_dir)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client
