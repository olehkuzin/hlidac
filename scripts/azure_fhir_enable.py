"""Enable the /fhir/r4 instance on the Azure IRIS. InstallInstance left it with
isEnabled = False, so every FHIR request returned 503 MetadataNotConfigured."""

import os
import pathlib

import iris

HOST = os.environ["AZ_IRIS_HOST"]  # the IRIS server address, never in the repo
PORT = 1972
USER = "_SYSTEM"
NAMESPACE = "FHIRSERVER"
ENDPOINT = "/fhir/r4"


def main() -> None:
    password = os.environ.get("AZ_IRIS_PASSWORD") or (
        pathlib.Path.home().joinpath(".azure/iris-password").read_text().strip()
    )
    conn = iris.createConnection(HOST, PORT, NAMESPACE, USER, password)
    try:
        irispy = iris.createIRIS(conn)
        inst = irispy.classMethodObject("HS.FHIRServer.ServiceInstance", "FindByUrl", ENDPOINT)
        print("isEnabled before:", inst.get("isEnabled"))
        inst.set("isEnabled", 1)
        print("save status:", inst.invoke("%Save"))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
