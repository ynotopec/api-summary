import pytest

from api_summary.config import Settings
from api_summary.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        app_env="development",
        auth_enabled=False,
        data_dir=tmp_path,
        public_base_url="http://testserver",
        max_chunk_bytes=1024,
        max_file_bytes=1024**2,
        max_owner_bytes=2 * 1024**2,
        max_storage_bytes=4 * 1024**2,
    )


@pytest.fixture
def store(settings):
    return Store(settings)
