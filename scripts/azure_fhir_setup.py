"""Create the FHIR R4 server on the Azure ACI instance (adapted from the kit's
fhir_server_setup_native.py; connects to HSLIB first, as the kit does)."""

import os
import pathlib

import iris

HOST = os.environ["AZ_IRIS_HOST"]  # the IRIS server address, never in the repo
PORT = 1972
USER = "_SYSTEM"
# Never hardcode secrets in the repo: read from env or the local password file.
PASSWORD = os.environ.get(
    "AZ_IRIS_PASSWORD",
    pathlib.Path.home().joinpath(".azure/iris-password").read_text().strip(),
)

NAMESPACE = "FHIRSERVER"
ENDPOINT = "/fhir/r4"


def connect(namespace: str):
    conn = iris.createConnection(HOST, PORT, namespace, USER, PASSWORD)
    return conn, iris.createIRIS(conn)


def main() -> None:
    # Step 1: create the FHIRSERVER namespace via the HSLIB installer.
    conn, irispy = connect("HSLIB")
    try:
        ns_exists = irispy.classMethodBoolean("%SYS.Namespace", "Exists", NAMESPACE)
        print(f"namespace {NAMESPACE} exists: {ns_exists}", flush=True)
        if not ns_exists:
            print("running HS.Util.Installer.Foundation.Install ...", flush=True)
            irispy.classMethodVoid("HS.Util.Installer.Foundation", "Install", NAMESPACE)
        print(
            "namespace now exists:",
            irispy.classMethodBoolean("%SYS.Namespace", "Exists", NAMESPACE),
            flush=True,
        )
    finally:
        conn.close()

    # Step 2: install the FHIR namespace structures + the /fhir/r4 instance.
    conn, irispy = connect(NAMESPACE)
    try:
        exists = irispy.classMethodBoolean(
            "HS.FHIRServer.ServiceInstance", "ExistsByUrl", ENDPOINT
        )
        print(f"FHIR instance {ENDPOINT} exists: {exists}", flush=True)
        if exists:
            print("already installed, nothing to do", flush=True)
            return
        strategy = "HS.FHIRServer.Storage.JsonAdvSQL.InteractionsStrategy"
        metadata = "hl7.fhir.r4.core@4.0.1"
        print("InstallNamespace ...", flush=True)
        irispy.classMethodVoid("HS.FHIRServer.Installer", "InstallNamespace")
        print("InstallInstance (this compiles the R4 schema, may take a while) ...", flush=True)
        irispy.classMethodVoid(
            "HS.FHIRServer.Installer", "InstallInstance", ENDPOINT, strategy, metadata
        )
        print(
            "installed:",
            irispy.classMethodBoolean("HS.FHIRServer.ServiceInstance", "ExistsByUrl", ENDPOINT),
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
