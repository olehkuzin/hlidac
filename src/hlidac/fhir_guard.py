"""The write guard for FHIR transaction bundles. Standard library only.

The app runs it when it enqueues a bundle (`schemas/outbox.py`), and the IRIS production runs the
same file again just before posting, so a row changed in the database can't widen what we write.
We write only our own resource types, always tagged, and only by our own identifiers or ids, so a
write can never match a hospital resource and a retry never duplicates one.
"""

import json
import math
import re
from typing import Any
from urllib.parse import parse_qs

TAG_SYSTEM = "urn:hlidac:tag"
TAG_CODE = "hlidac"
ID_PREFIX = "hlidac-"  # our resource ids, for types without a searchable identifier (Provenance)
WRITABLE_TYPES = frozenset(
    {"Questionnaire", "QuestionnaireResponse", "Observation", "Provenance", "Task"}
)
_OUR_SYSTEM = re.compile(r"^urn:hlidac:[a-z-]+$")
_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_OUR_ID = re.compile(r"^hlidac-[A-Za-z0-9.-]{1,57}$")


def check_bundle(bundle: Any) -> None:
    """Raise ValueError unless every entry is a safe write of ours."""
    if not isinstance(bundle, dict):
        raise ValueError("the job is a FHIR transaction Bundle")  # noqa: TRY004 (see check_json)
    if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
        raise ValueError("the job is a FHIR transaction Bundle")
    entries = bundle.get("entry")
    if not isinstance(entries, list) or not entries:
        raise ValueError("the bundle has entries")
    for entry in entries:
        _check_entry(entry)


def check_json(bundle_json: str) -> dict[str, Any]:
    """Parse and check. Only ValueError is raised, which Pydantic turns into a ValidationError.
    Ambiguous JSON fails closed: a duplicate key (parsers disagree on which value wins) and a
    non-finite number (NaN, Infinity, or one too large for a float) are refused."""
    if not isinstance(bundle_json, str):
        raise ValueError("the job is a FHIR transaction Bundle as a JSON string")  # noqa: TRY004
    try:
        bundle = json.loads(
            bundle_json,
            object_pairs_hook=_unique_keys,
            parse_constant=_no_constant,
            parse_float=_finite_float,
        )
    except RecursionError:
        raise ValueError("the bundle is nested too deeply") from None
    check_bundle(bundle)
    return bundle


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("a JSON object repeats a key")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("a JSON number is too large")
    return value


def _check_entry(entry: Any) -> None:
    resource = entry.get("resource") if isinstance(entry, dict) else None
    request = entry.get("request") if isinstance(entry, dict) else None
    if not isinstance(resource, dict) or not isinstance(request, dict):
        raise ValueError("every entry has a resource and a request")  # noqa: TRY004
    resource_type = resource.get("resourceType")
    if not isinstance(resource_type, str) or resource_type not in WRITABLE_TYPES:
        raise ValueError(f"we never write {resource_type!r} resources")
    meta = resource.get("meta")
    tags = meta.get("tag") if isinstance(meta, dict) else None
    if not isinstance(tags, list) or not any(
        isinstance(t, dict) and t.get("system") == TAG_SYSTEM and t.get("code") == TAG_CODE
        for t in tags
    ):
        raise ValueError("every resource we write carries the hlidac tag")
    resource_id = resource.get("id")
    if resource_id is not None and not (
        isinstance(resource_id, str) and _OUR_ID.match(resource_id)
    ):
        raise ValueError("a resource we write has no id, or one of ours")

    method, url = request.get("method"), request.get("url")
    if not isinstance(url, str):
        raise ValueError("every request has a URL")  # noqa: TRY004
    path, _, query = url.partition("?")
    if method == "PUT" and query:
        # Conditional update by our own identifier.
        if path != resource_type:
            raise ValueError("the request URL names the resource's own type")
        _check_search(query, resource)
    elif method == "PUT":
        # Update by our own id (Provenance has no identifier to search on).
        if resource_id is None or path != f"{resource_type}/{resource_id}":
            raise ValueError("a PUT without a search targets the resource's own hlidac- id")
    elif method == "POST":
        if path != resource_type:
            raise ValueError("the request URL names the resource's own type")
        if resource_id is not None:
            raise ValueError("a create carries no id")
        _check_search(str(request.get("ifNoneExist", "")), resource)
    else:
        raise ValueError("only PUT and conditional POST; nothing is ever deleted or patched")


def _check_search(query: str, resource: dict[str, Any]) -> None:
    """Exactly one `identifier=<our system>|<value>`, and it is the resource's own identifier."""
    try:
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("the search is exactly one identifier") from None
    if list(params) != ["identifier"] or len(params["identifier"]) != 1:
        raise ValueError("the search is exactly one identifier")
    system, separator, value = params["identifier"][0].partition("|")
    if not separator or not _OUR_SYSTEM.match(system) or not _VALUE.match(value):
        raise ValueError("the search uses one of our identifier systems and a plain value")
    own = resource.get("identifier") or []
    own = own if isinstance(own, list) else [own]  # QuestionnaireResponse has a single identifier
    if not any(
        isinstance(i, dict) and (i.get("system"), i.get("value")) == (system, value) for i in own
    ):
        raise ValueError("the search identifier is the resource's own identifier")
