"""WP10 on the team's IRIS (HLIDAC_IRIS_TESTS=1 only): the app's account has exactly the
privileges setup.py grants and can't write hospital resources. Every probe is harmless even if a
privilege were wrongly granted: SQL probes use WHERE 1=0, and the FHIR probes read the server's
metadata or delete a resource that doesn't exist. Hospital tables are looked up by name only
(schema metadata), never read.
"""

import re

import httpx
import pytest
from iris_support import IRIS_TESTS, TEST_SCHEMA

from hlidac.config import Settings
from hlidac.iris import setup

pytestmark = pytest.mark.skipif(not IRIS_TESTS, reason="IRIS tests run only with HLIDAC_IRIS_TESTS=1")

PRIVILEGE_VIOLATION = r"<-99>"


def denied(db, statement: str) -> None:
    with pytest.raises(Exception, match=PRIVILEGE_VIOLATION):
        db.execute(statement)


def test_append_only_tables_refuse_updates(iris_app_db):
    for table in setup.APPEND_ONLY:
        denied(iris_app_db, f"UPDATE {TEST_SCHEMA}.{table} SET record_json = '' WHERE 1=0")


def test_no_rows_can_be_deleted_and_no_tables_changed(iris_app_db):
    for table in setup.table_names(TEST_SCHEMA):
        denied(iris_app_db, f"DELETE FROM {TEST_SCHEMA}.{table} WHERE 1=0")
    denied(iris_app_db, f"DROP TABLE {TEST_SCHEMA}.account")
    denied(iris_app_db, f"CREATE TABLE {TEST_SCHEMA}.extra (k INTEGER)")


def test_hospital_tables_are_out_of_reach(iris_admin, iris_app_db):
    # The repository's resource, version and main search tables (HSFHIR_X0001_R, _V, _S), which
    # hold every hospital resource; the ~1,600 per-type search tables would take minutes to probe.
    names = [
        (schema, table)
        for schema, table in iris_admin.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
            " WHERE TABLE_SCHEMA %STARTSWITH 'HSFHIR'"
        )
        if re.fullmatch(r"HSFHIR_X\d+_[RSV]", schema)
    ]
    assert names, "expected the FHIR repository's tables in this namespace"
    for schema, table in names:
        target = f'"{schema}"."{table}"'
        denied(iris_app_db, f"DELETE FROM {target} WHERE 1=0")
        denied(iris_app_db, f"INSERT INTO {target} SELECT * FROM {target} WHERE 1=0")


def test_no_global_access_or_code_execution():
    import iris

    config = Settings()
    conn = iris.connect(
        config.iris_host,
        config.iris_port,
        config.iris_namespace,
        config.iris_user,
        config.iris_password.get_secret_value(),
    )
    try:
        native = iris.createIRIS(conn)
        with pytest.raises(Exception, match="%Native_GlobalAccess"):
            native.set("probe", "^HlidacProbe")
        with pytest.raises(Exception, match="%Native_ClassExecution"):
            native.classMethodValue("%SYSTEM.Version", "GetNumber")
    finally:
        conn.close()


def test_the_app_account_cannot_use_fhir():
    """setup.lock_fhir_api: only accounts with the FhirApi resource reach the FHIR API. IRIS
    refuses everyone else with 404, so the metadata (no patient data) tells access apart, with an
    administrator as the control; a delete of a missing resource returns 204 when writes work."""
    config, admin = Settings(), setup.AdminSettings()
    app_login = (config.iris_user, config.iris_password.get_secret_value())
    metadata = f"{config.fhir_base_url}/metadata"
    control = httpx.get(metadata, auth=(admin.user, admin.password.get_secret_value()), timeout=60)
    assert control.status_code == 200
    assert httpx.get(metadata, auth=app_login, timeout=60).status_code != 200
    assert httpx.get(metadata, timeout=60).status_code != 200  # no unauthenticated access
    deleted = httpx.delete(
        f"{config.fhir_base_url}/Basic/hlidac-probe-does-not-exist", auth=app_login, timeout=30
    )
    assert deleted.status_code not in {200, 204}
