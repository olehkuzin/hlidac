"""One shared IRIS SQL connection for the app (WP10).

Community Edition allows 8 connections and FHIR REST calls count too, so the app opens one, lazily
(importing the app never touches the network), and serialises every statement through it. Every
cursor is closed. The connection is autocommit, like the driver's default.

Azure drops TCP connections that are idle for about 4 minutes without telling either side, and the
next statement then hangs until the driver times out. So a connection idle for MAX_IDLE_SECONDS
is replaced before it is used: a statement is never sent on a connection that may be dead, and
nothing has to be retried.
"""

import contextlib
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from hlidac.config import Settings

Row = tuple[Any, ...]

MAX_IDLE_SECONDS = 120


class Database:
    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect
        self._conn: Any = None
        self._last_used = 0.0  # time.monotonic(): connection housekeeping, not the demo clock
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, config: Settings) -> "Database":
        missing = [
            name
            for name in ("iris_host", "iris_port", "iris_namespace", "iris_user", "iris_password")
            if getattr(config, name) is None
        ]
        if missing:
            raise RuntimeError(f"STORE_BACKEND=iris needs {', '.join(missing).upper()} in .env")
        password = config.iris_password.get_secret_value()
        return cls.connect_to(
            config.iris_host, config.iris_port, config.iris_namespace, config.iris_user, password
        )

    @classmethod
    def connect_to(
        cls, host: str, port: int, namespace: str, user: str, password: str
    ) -> "Database":
        import iris  # the intersystems-irispython driver; only needed with STORE_BACKEND=iris

        return cls(lambda: iris.connect(host, port, namespace, user, password))

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a statement; returns the number of rows it changed."""
        return self._run(sql, params, fetch=False)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return self._run(sql, params, fetch=True)

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _run(self, sql: str, params: Sequence[Any], fetch: bool) -> Any:
        with self._lock:
            if self._conn is not None and time.monotonic() - self._last_used > MAX_IDLE_SECONDS:
                self._drop()
            if self._conn is None:
                self._conn = self._connect()
            self._last_used = time.monotonic()
            cursor = self._conn.cursor()
            try:
                cursor.execute(sql, list(params))
                return [tuple(row) for row in cursor.fetchall()] if fetch else cursor.rowcount
            except Exception as error:
                # An SQL error (constraint, privilege, syntax) leaves the connection usable;
                # anything else (network, server restart) gets a fresh connection next time.
                if "SQLCODE" not in str(error):
                    self._drop()
                raise
            finally:
                with contextlib.suppress(Exception):  # the connection may be gone already
                    cursor.close()

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):  # closing a broken connection may fail
                conn.close()
