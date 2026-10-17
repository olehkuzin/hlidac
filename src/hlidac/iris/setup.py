"""Create our IRIS SQL tables and the app's least-privilege account (WP10).

    uv run python -m hlidac.iris.setup                       # tables, role, app user
    uv run python -m hlidac.iris.setup --seed-dev-accounts   # also the invented dev_seed accounts

Idempotent: run it again whenever the server restarts (the team's Azure instance loses its data
on restart) or the app password in .env changes. It runs as the IRIS administrator
(IRIS_ADMIN_USER / IRIS_ADMIN_PASSWORD in .env, which the app never reads) and creates the app's
account from IRIS_USER / IRIS_PASSWORD. It touches only our own schema, role and user, never
hospital data, and prints names and counts only.

The app's role may SELECT, INSERT and UPDATE our tables, only SELECT and INSERT the append-only
ones, never DELETE, and nothing outside our schema; without %Native_* resources it can't reach
globals or run code either (tests/test_iris_store.py proves it).

It also locks the FHIR REST API (S1, D35): IRIS's `/fhir/r4` web application let any IRIS account
read and write the hospital FHIR data, and allowed unauthenticated requests. Now it requires the
resource FHIR_RESOURCE, which only %All accounts and the role FHIR_ROLE hold; FHIR_ROLE goes to
the accounts in FHIR_USERS, so they keep the access they had. The app's account doesn't get it.
"""

import argparse
import re
from importlib import resources
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from hlidac.config import Settings
from hlidac.iris.context import READ_ONLY_FHIR_TABLES
from hlidac.iris.db import Database
from hlidac.iris.store import SCHEMA

APP_ROLE = "HlidacApp"
APPEND_ONLY = frozenset({"assessment", "grade_review", "audit_log"})

FHIR_RESOURCE = "FhirApi"
FHIR_ROLE = "FhirApiAccess"
FHIR_SEARCH_ROLE = "HlidacFhirSearchRead"
FHIR_SEARCH_DB_RESOURCE = "%DB_FHIRSERVERX0001R"
# Accounts without %All that used the FHIR API before the lock (the server's Admin account).
FHIR_USERS = ("Admin",)
_UNAUTHENTICATED = 64  # Security.Applications.AutheEnabled bit

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_PASSWORD = re.compile(r"^[A-Za-z0-9._-]{16,32}$")  # interpolated into DDL, so no quotes


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="IRIS_ADMIN_", extra="ignore")

    user: str | None = None
    password: SecretStr | None = None


def admin_database(config: Settings, admin: AdminSettings | None = None) -> Database:
    admin = admin or AdminSettings()
    if admin.user is None or admin.password is None:
        raise RuntimeError("set IRIS_ADMIN_USER and IRIS_ADMIN_PASSWORD in .env")
    return Database.connect_to(
        config.iris_host,
        config.iris_port,
        config.iris_namespace,
        admin.user,
        admin.password.get_secret_value(),
    )


def statements(schema: str = SCHEMA) -> list[str]:
    """The statements of schema.sql, for the given schema."""
    sql = resources.files("hlidac.iris").joinpath("schema.sql").read_text()
    lines = [line.split("--", 1)[0] for line in sql.splitlines()]
    found = [s.strip() for s in "\n".join(lines).split(";")]
    return [s.replace("Hlidac.", f"{_name(schema)}.") for s in found if s]


def table_names(schema: str = SCHEMA) -> list[str]:
    return [m.group(1) for s in statements(schema) if (m := re.match(r"CREATE TABLE \w+\.(\w+)", s))]


def existing_tables(db: Database, schema: str) -> set[str]:
    rows = db.query(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ?", [schema]
    )
    return {row[0] for row in rows}


def create_tables(db: Database, schema: str = SCHEMA) -> list[str]:
    """Creates the tables (with their indexes) that don't exist yet; returns their names."""
    existing = existing_tables(db, schema)
    created = []
    for statement in statements(schema):
        table = re.search(r"(?:CREATE TABLE|ON) \w+\.(\w+)", statement).group(1)
        if table in existing:
            continue
        db.execute(statement)
        if statement.startswith("CREATE TABLE"):
            created.append(table)
    return created


def expected_columns(schema: str = SCHEMA) -> dict[str, set[str]]:
    columns = {}
    for statement in statements(schema):
        if m := re.match(r"CREATE TABLE \w+\.(\w+)", statement):
            definitions = re.finditer(
                r"^\s+(\w+)\s+(?:VARCHAR|TIMESTAMP|INTEGER)", statement, re.MULTILINE
            )
            columns[m.group(1)] = {c.group(1).lower() for c in definitions}
    return columns


def schema_drift(db: Database, schema: str = SCHEMA) -> list[str]:
    """Existing tables whose columns differ from schema.sql (create_tables skips existing ones)."""
    rows = db.query(
        "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ?",
        [schema],
    )
    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column.lower())
    return [
        table
        for table, wanted in expected_columns(schema).items()
        if table in actual and actual[table] - {"id"} != wanted
    ]


def grant_app_role(db: Database, schema: str = SCHEMA, role: str = APP_ROLE) -> None:
    """Exactly the privileges in the module docstring, whatever the role had before."""
    _ignore_existing(db, f"CREATE ROLE {_name(role)}")
    for table in table_names(schema):
        target = f"{_name(schema)}.{table}"
        db.execute(f"REVOKE ALL PRIVILEGES ON {target} FROM {role}")
        privileges = "SELECT, INSERT" if table in APPEND_ONLY else "SELECT, INSERT, UPDATE"
        db.execute(f"GRANT {privileges} ON {target} TO {role}")
    if schema == SCHEMA:
        for fhir_schema, fhir_table in READ_ONLY_FHIR_TABLES:
            if fhir_table in existing_tables(db, fhir_schema):
                target = f'"{_name(fhir_schema)}"."{_name(fhir_table)}"'
                db.execute(f"REVOKE ALL PRIVILEGES ON {target} FROM {role}")
                db.execute(f"GRANT SELECT ON {target} TO {role}")


def ensure_app_user(db: Database, user: str, password: str, role: str = APP_ROLE) -> str:
    """Creates the app's account, or resets its password to the one in .env."""
    if not _PASSWORD.fullmatch(password):
        raise ValueError("IRIS_PASSWORD: 16-32 characters of A-Z, a-z, 0-9, '.', '_' or '-'")
    if user.casefold() in {"_system", "superuser", "admin", "_ensemble"}:
        raise ValueError("IRIS_USER is the app's own account, never an administrator")
    created = _ignore_existing(db, f'CREATE USER {_name(user)} IDENTIFIED BY "{password}"')
    if not created:
        db.execute(f'ALTER USER {user} IDENTIFIED BY "{password}"')
    db.execute(f"GRANT {_name(role)} TO {user}")
    return "created" if created else "password reset"


def lock_fhir_api(config: Settings, admin: AdminSettings | None = None) -> str:
    """Makes the FHIR web application require FHIR_RESOURCE and a login (idempotent)."""
    import iris

    admin = admin or AdminSettings()
    app_name = urlsplit(config.fhir_base_url).path.rstrip("/")
    conn = iris.connect(
        config.iris_host, config.iris_port, "%SYS", admin.user, admin.password.get_secret_value()
    )
    try:
        native = iris.createIRIS(conn)

        def ok(status: object, what: str) -> None:
            if not native.classMethodValue("%SYSTEM.Status", "IsOK", status):
                text = native.classMethodValue("%SYSTEM.Status", "GetErrorText", status)
                raise RuntimeError(f"{what}: {text}")

        if not native.classMethodValue("Security.Resources", "Exists", FHIR_RESOURCE):
            ok(
                native.classMethodValue(
                    "Security.Resources", "Create", FHIR_RESOURCE, "Use of the FHIR REST API", ""
                ),
                "create resource",
            )
        if not native.classMethodValue("Security.Roles", "Exists", FHIR_ROLE):
            ok(
                native.classMethodValue(
                    "Security.Roles", "Create", FHIR_ROLE, "May use the FHIR REST API",
                    f"{FHIR_RESOURCE}:U",
                ),
                "create role",
            )
        for user in FHIR_USERS:
            if native.classMethodValue("Security.Users", "Exists", user):
                sql = conn.cursor()
                try:
                    sql.execute(f"GRANT {_name(FHIR_ROLE)} TO {_name(user)}")
                finally:
                    sql.close()

        if not native.classMethodValue("Security.Roles", "Exists", FHIR_SEARCH_ROLE):
            ok(
                native.classMethodValue(
                    "Security.Roles",
                    "Create",
                    FHIR_SEARCH_ROLE,
                    "Read-only database access for granted FHIR search tables",
                    f"{FHIR_SEARCH_DB_RESOURCE}:R",
                ),
                "create search-read role",
            )
        if config.iris_user and native.classMethodValue("Security.Users", "Exists", config.iris_user):
            sql = conn.cursor()
            try:
                sql.execute(f"GRANT {_name(FHIR_SEARCH_ROLE)} TO {_name(config.iris_user)}")
            finally:
                sql.close()

        application = native.classMethodObject("Security.Applications", "%OpenId", app_name)
        if application is None:
            raise RuntimeError(f"no web application {app_name}")
        before = (application.get("Resource"), application.get("AutheEnabled"))
        application.set("Resource", FHIR_RESOURCE)
        application.set("AutheEnabled", before[1] & ~_UNAUTHENTICATED)
        ok(application.invoke("%Save"), f"save {app_name}")
        changed = before != (FHIR_RESOURCE, before[1] & ~_UNAUTHENTICATED)
        return f"{app_name} {'locked' if changed else 'was locked already'}"
    finally:
        conn.close()


def clear_tables(db: Database, schema: str) -> None:
    """Empties a test schema. Refuses our real one: its rows go only through reset_demo (WP14)."""
    if schema == SCHEMA:
        raise ValueError("clear_tables is for test schemas only")
    # One round trip finds the tables with rows (every statement is one to Azure).
    counts = " UNION ALL ".join(
        f"SELECT '{table}', COUNT(*) FROM {_name(schema)}.{table}" for table in table_names(schema)
    )
    for table, rows in db.query(counts):
        if rows:
            db.execute(f"DELETE FROM {schema}.{_name(table)}")


def drop_tables(db: Database, schema: str) -> None:
    if schema == SCHEMA:
        raise ValueError("drop_tables is for test schemas only")
    for table in table_names(schema):
        if table in existing_tables(db, schema):
            db.execute(f"DROP TABLE {_name(schema)}.{table}")


def _ignore_existing(db: Database, statement: str) -> bool:
    """Runs a CREATE; False if the user or role exists already (SQLCODE -118)."""
    try:
        db.execute(statement)
    except Exception as error:
        if "<-118>" in str(error):
            return False
        raise
    return True


def _name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError(f"not a plain SQL name: {name!r}")
    return name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed-dev-accounts", action="store_true")
    args = parser.parse_args()

    config = Settings()
    if config.iris_user is None or config.iris_password is None:
        raise SystemExit("set IRIS_USER and IRIS_PASSWORD (the app's account) in .env")
    admin = admin_database(config)
    try:
        created = create_tables(admin)
        print(f"tables: {len(created)} created, {len(table_names()) - len(created)} existed")
        for table in schema_drift(admin):
            print(
                f"WARNING: {SCHEMA}.{table} differs from schema.sql. Drop it (our rows only; "
                "the app's data in it is lost) and run the setup again."
            )
        grant_app_role(admin)
        print(f"role {APP_ROLE}: privileges granted on {len(table_names())} tables")
        outcome = ensure_app_user(admin, config.iris_user, config.iris_password.get_secret_value())
        print(f"user {config.iris_user}: {outcome}")
    finally:
        admin.close()
    if config.fhir_base_url is not None:
        print(f"FHIR API: {lock_fhir_api(config)} (needs {FHIR_RESOURCE}; no unauthenticated access)")

    if args.seed_dev_accounts:
        from hlidac import dev_seed
        from hlidac.clock import DemoClock
        from hlidac.iris.store import IrisStore

        app_db = Database.from_settings(config)
        try:
            dev_seed.seed(IrisStore(app_db), DemoClock.from_as_of(config.as_of).now())
        finally:
            app_db.close()
        print(f"dev accounts: {len(dev_seed.ACCOUNTS)} saved (invented, no patient data)")


if __name__ == "__main__":
    main()
