import os, tempfile, pathlib
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="avdlab-test-"))
os.environ.setdefault("AVDLAB_DATA_DIR", str(_tmp))
os.environ.setdefault("AVDLAB_DATABASE_URL", f"sqlite+aiosqlite:///{_tmp}/test.db")
os.environ.setdefault("AVDLAB_ADMIN_PASSWORD", "test")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client():
    from avdlab.main import app
    from avdlab.core.db import init_db
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "test"})
        assert r.status_code == 200, r.text
        yield c
