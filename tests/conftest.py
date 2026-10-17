"""The `store` fixture: every store test runs on InMemoryStore, and also on IrisStore when
HLIDAC_IRIS_TESTS=1 (WP10). The IRIS runs need the team's IRIS and both accounts in .env; a
teammate runs them, never CI. They use their own schema, HlidacTest, created with the app's
privileges and dropped at the end, so the real Hlidac tables are never touched. No patient data.
"""

import pytest
from iris_support import IRIS_TESTS, TEST_SCHEMA

from hlidac.store import InMemoryStore, Store


@pytest.fixture(scope="session")
def iris_admin():
    """The administrator's connection: creates, empties and drops the test schema."""
    if not IRIS_TESTS:
        pytest.skip("IRIS tests run only with HLIDAC_IRIS_TESTS=1")
    from hlidac.config import Settings
    from hlidac.iris import setup

    admin = setup.admin_database(Settings())
    setup.create_tables(admin, TEST_SCHEMA)
    setup.grant_app_role(admin, TEST_SCHEMA)
    yield admin
    setup.drop_tables(admin, TEST_SCHEMA)
    admin.close()


@pytest.fixture(scope="session")
def iris_app_db(iris_admin):
    """The app's own least-privilege connection, as in production."""
    from hlidac.config import Settings
    from hlidac.iris.db import Database

    db = Database.from_settings(Settings())
    yield db
    db.close()


@pytest.fixture(params=["memory", "iris"] if IRIS_TESTS else ["memory"])
def store(request) -> Store:
    if request.param == "memory":
        return InMemoryStore()
    from hlidac.iris import setup
    from hlidac.iris.store import IrisStore

    setup.clear_tables(request.getfixturevalue("iris_admin"), TEST_SCHEMA)
    return IrisStore(request.getfixturevalue("iris_app_db"), schema=TEST_SCHEMA)
