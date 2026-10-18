"""Load the Rakathon dataset into the Azure FHIR server as transaction bundles:
all 1830 patients by default, or a subset via --source (see
select_diverse_subset.py).

Target-diagnosis patients (C61, C64) go first so the useful data lands early.
Large bundles are split into ordered transaction chunks so a single request
stays well under gateway timeouts on the 2 vCPU instance.

Resumable: a patient whose final resource already exists on the server is
skipped, so re-runs do not write new versions of everything (the Community
licence caps total data at ~10 GB). Bundles are PUTs with fixed ids, so a
retried chunk is idempotent.

Counts-only logging (D23: no patient values in logs).

    uv run python scripts/azure_load_bundles.py --dry-run   # plan only, no network
    uv run python scripts/azure_load_bundles.py --source <dataset>/fhir/subset-diverse.ndjson
"""

import argparse
import base64
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HOST = os.environ["AZ_IRIS_HOST"]  # the IRIS server address, never in the repo
FHIR_BASE = f"http://{HOST}:52773/fhir/r4"
USER = "_SYSTEM"

# Unpacked Rakathon dataset (outside the repo); --source overrides the default file
DATASET = pathlib.Path(os.environ.get("RAKATHON_DATASET_DIR", ".")) / "fhir"
SOURCE = DATASET / "bundles.ndjson"
PRIORITY_FILES = [DATASET / "bundles-C61.ndjson", DATASET / "bundles-C64.ndjson"]

WORKERS = 3  # each in-flight request holds one of the 8 Community licence units
CHUNK_SIZE = 500
RETRIES = 3
TIMEOUT = 600
LOG_EVERY = 10


def load_password() -> str:
    return os.environ.get("AZ_IRIS_PASSWORD") or (
        pathlib.Path.home().joinpath(".azure/iris-password").read_text().strip()
    )


def patient_id(bundle: dict) -> str:
    return next(
        e["resource"]["id"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Patient"
    )


def priority_ids() -> set[str]:
    ids: set[str] = set()
    for path in PRIORITY_FILES:
        with path.open("rb") as fh:
            ids.update(patient_id(json.loads(line)) for line in fh if line.strip())
    return ids


def collect_bundles(source: pathlib.Path) -> list[bytes]:
    """All bundles in source, target-diagnosis patients first. Pseudonyms are
    used only for ordering and are never printed."""
    first = priority_ids()
    head: list[bytes] = []
    tail: list[bytes] = []
    with source.open("rb") as fh:
        for line in fh:
            if line.strip():
                (head if patient_id(json.loads(line)) in first else tail).append(line)
    return head + tail


def refs(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "reference" and isinstance(v, str):
                yield v
            else:
                yield from refs(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from refs(x)


def key(entry: dict) -> str:
    r = entry["resource"]
    return f"{r['resourceType']}/{r['id']}"


def plan_chunks(raw: bytes) -> tuple[list[bytes], str, int]:
    """Return (request payloads in order, key of the last resource sent, resource count).

    Small bundles go as-is (one atomic transaction). Large ones are reordered so
    every reference points to a resource sent earlier (source order lists each
    DiagnosticReport before its Observations), then split into chunks.
    """
    bundle = json.loads(raw)
    entries = bundle["entry"]
    if len(entries) <= CHUNK_SIZE:
        return [raw], key(entries[-1]), len(entries)

    rank = {"Patient": 0, "DiagnosticReport": 2}
    ordered = sorted(entries, key=lambda e: rank.get(e["resource"]["resourceType"], 1))
    sent: set[str] = set()
    for e in ordered:
        if any(ref not in sent for ref in refs(e["resource"])):
            raise ValueError("bundle has a reference that cannot be ordered backwards")
        sent.add(key(e))

    payloads = [
        json.dumps(
            {"resourceType": "Bundle", "type": "transaction", "entry": ordered[i : i + CHUNK_SIZE]},
            ensure_ascii=False,
        ).encode("utf-8")
        for i in range(0, len(ordered), CHUNK_SIZE)
    ]
    return payloads, key(ordered[-1]), len(entries)


class Client:
    def __init__(self, password: str):
        token = base64.b64encode(f"{USER}:{password}".encode()).decode()
        self.headers = {"Accept": "application/fhir+json", "Authorization": f"Basic {token}"}

    def get(self, path: str, timeout: int = 60):
        req = urllib.request.Request(f"{FHIR_BASE}/{path}", headers=self.headers)
        return urllib.request.urlopen(req, timeout=timeout)

    def exists(self, resource_key: str) -> bool:
        try:
            with self.get(resource_key) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return False
            raise

    def post_transaction(self, payload: bytes) -> tuple[int, str]:
        last_err = ""
        for attempt in range(1, RETRIES + 1):
            req = urllib.request.Request(
                FHIR_BASE,
                data=payload,
                method="POST",
                headers={**self.headers, "Content-Type": "application/fhir+json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    return resp.status, ""
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code < 500:
                    return e.code, last_err
            except Exception as e:  # noqa: BLE001 (network errors: retry)
                last_err = type(e).__name__
            if attempt < RETRIES:
                time.sleep(5 * attempt)
        return 0, last_err


def load_patient(client: Client, idx: int, raw: bytes) -> tuple[int, str, int, str]:
    """Returns (idx, outcome, resources sent, error). Outcome: loaded/skipped/failed."""
    try:
        payloads, last_key, n = plan_chunks(raw)
        if client.exists(last_key):
            return idx, "skipped", 0, ""
        for payload in payloads:
            status, err = client.post_transaction(payload)
            if status != 200:
                return idx, "failed", 0, err
        return idx, "loaded", n, ""
    except Exception as e:  # noqa: BLE001 (one bad patient file must not stop the load)
        return idx, "failed", 0, type(e).__name__


def check_endpoint(client: Client) -> None:
    """Fail fast unless the FHIR endpoint is up and auth works (counts only)."""
    with client.get("metadata") as resp:
        print(f"metadata: HTTP {resp.status}")
    with client.get("Patient?_summary=count") as resp:
        print(f"auth check: HTTP {resp.status}, Patient count before load: {json.load(resp).get('total')}")


def verify(client: Client) -> None:
    for rtype in ["Patient", "Condition", "Observation", "DiagnosticReport", "DocumentReference",
                  "ImagingStudy", "MedicationRequest", "MedicationAdministration", "Procedure"]:
        try:
            with client.get(f"{rtype}?_summary=count", timeout=300) as resp:
                print(f"{rtype}: {json.load(resp).get('total')}")
        except urllib.error.HTTPError as e:
            print(f"{rtype}: HTTP {e.code}")


def dry_run(bundles: list[bytes]) -> None:
    requests = resources = chunked = 0
    for raw in bundles:
        payloads, _, n = plan_chunks(raw)
        requests += len(payloads)
        resources += n
        chunked += len(payloads) > 1
    print(f"bundles {len(bundles)}, chunked {chunked}, requests {requests}, resources {resources}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="plan chunks only, no network")
    parser.add_argument("--source", type=pathlib.Path, default=SOURCE,
                        help="ndjson of bundles, e.g. subset-diverse.ndjson")
    args = parser.parse_args()

    bundles = collect_bundles(args.source)
    print(f"bundles to load: {len(bundles)}")
    if args.dry_run:
        dry_run(bundles)
        return

    client = Client(load_password())
    check_endpoint(client)
    counts = {"loaded": 0, "skipped": 0, "failed": 0}
    failed: list[tuple[int, str]] = []
    resources = 0
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(load_patient, client, i, raw) for i, raw in enumerate(bundles, 1)]
        for done, future in enumerate(as_completed(futures), 1):
            idx, outcome, n, err = future.result()
            counts[outcome] += 1
            resources += n
            if outcome == "failed":
                failed.append((idx, err))
            if done % LOG_EVERY == 0 or done == len(bundles):
                rate = resources / max(time.monotonic() - start, 1)
                print(f"progress: {done}/{len(bundles)} {counts}, {resources} resources, "
                      f"{rate:.0f}/s", flush=True)
    print(f"done in {(time.monotonic() - start) / 60:.1f} min: {counts}, {resources} resources")
    if failed:
        print("failed indices (re-run to retry):", failed[:20])
        sys.exit(1)
    verify(client)


if __name__ == "__main__":
    main()
