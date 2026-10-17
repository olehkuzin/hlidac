"""Deploy the outbox production `Hlidac.Production` to IRIS (WP11, D36).

    uv run python -m hlidac.iris.deploy           # load, compile, (re)start, auto-start
    uv run python -m hlidac.iris.deploy --stop    # stop it

No IoP and nothing installed in the container: the classes in `iris/cls/` (ObjectScript, plus one
method in Python) are loaded and compiled over the admin connection, and `fhir_guard.py`, the
same file the app uses, is stored in ^Hlidac.Code for the production to run. Idempotent. Run it
after `python -m hlidac.iris.setup`, after every change to `iris/cls/` or `fhir_guard.py`, and
after the Azure instance restarts. Prints names and statuses only.
"""

import argparse
import hashlib
import re
from importlib import resources

from hlidac.config import Settings
from hlidac.iris.setup import AdminSettings

PRODUCTION = "Hlidac.Production"
GUARD_GLOBAL = "^Hlidac.Code"


def class_sources() -> dict[str, str]:
    """Class name -> UDL text, from iris/cls/<name>.cls."""
    folder = resources.files("hlidac.iris").joinpath("cls")
    sources = {}
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".cls"):
            text = entry.read_text(encoding="utf-8")
            name = re.search(r"^Class (\S+)", text, re.MULTILINE).group(1)
            if f"{name}.cls" != entry.name:
                raise ValueError(f"{entry.name} defines {name}")
            sources[name] = text
    return sources


def guard_source() -> str:
    return resources.files("hlidac").joinpath("fhir_guard.py").read_text(encoding="utf-8")


class Deployer:
    def __init__(self, config: Settings, admin: AdminSettings) -> None:
        import iris

        self._conn = iris.connect(
            config.iris_host,
            config.iris_port,
            config.iris_namespace,
            admin.user,
            admin.password.get_secret_value(),
        )
        self._iris = iris
        self._native = iris.createIRIS(self._conn)
        self._namespace = config.iris_namespace

    def close(self) -> None:
        self._conn.close()

    def _ok(self, status: object, what: str) -> None:
        if not self._native.classMethodValue("%SYSTEM.Status", "IsOK", status):
            text = self._native.classMethodValue("%SYSTEM.Status", "GetErrorText", status)
            raise RuntimeError(f"{what}: {text}")

    def store_guard(self) -> str:
        source = guard_source()
        version = hashlib.sha256(source.encode()).hexdigest()[:16]
        self._native.set(source, GUARD_GLOBAL, "fhir_guard")
        self._native.set(version, GUARD_GLOBAL, "fhir_guard", "sha256")
        return version

    def load_classes(self) -> list[str]:
        sources = class_sources()
        for name, text in sources.items():
            status = self._native.classMethodValue(  # the files use \n; IRIS defaults to CR LF
                "%Compiler.UDL.TextServices", "SetTextFromString", self._namespace, name, text, "\n"
            )
            self._ok(status, f"load {name}")
        errors = self._iris.IRISReference(None)
        status = self._native.classMethodValue(
            "%SYSTEM.OBJ", "Compile", ",".join(sources), "ck-d", errors
        )
        self._ok(status, "compile")
        return list(sources)

    def running(self) -> tuple[str, int]:
        name, state = self._iris.IRISReference(""), self._iris.IRISReference(0)
        self._ok(
            self._native.classMethodValue("Ens.Director", "GetProductionStatus", name, state),
            "production status",
        )
        return name.getValue() or "", int(state.getValue() or 0)

    def stop(self) -> bool:
        name, state = self.running()
        if name != PRODUCTION or state != 1:
            return False
        self._ok(self._native.classMethodValue("Ens.Director", "StopProduction", 30, 1), "stop")
        return True

    def start(self) -> None:
        name, state = self.running()
        if name and name != PRODUCTION and state == 1:
            raise RuntimeError(f"another production runs in {self._namespace}: {name}")
        self.stop()
        self._ok(
            self._native.classMethodValue("Ens.Director", "StartProduction", PRODUCTION), "start"
        )
        self._ok(
            self._native.classMethodValue("Ens.Director", "SetAutoStart", PRODUCTION), "auto-start"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stop", action="store_true", help="stop the production")
    args = parser.parse_args()

    deployer = Deployer(Settings(), AdminSettings())
    try:
        if args.stop:
            print(f"{PRODUCTION}: {'stopped' if deployer.stop() else 'was not running'}")
            return
        print(f"fhir_guard stored in {GUARD_GLOBAL}, version {deployer.store_guard()}")
        print(f"classes compiled: {', '.join(deployer.load_classes())}")
        deployer.start()
        name, state = deployer.running()
        print(f"{PRODUCTION}: {'running' if (name, state) == (PRODUCTION, 1) else f'state {state}'}")
    finally:
        deployer.close()


if __name__ == "__main__":
    main()
