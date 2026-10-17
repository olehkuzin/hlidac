"""WP7: the model gateway (D5, guard G6). Requests are captured at the HTTP layer, so the tests
check what would really leave the app. No live model is called; every name and number is invented.
"""

import json

import httpx2 as httpx  # the HTTP client the openai SDK is built on
import pytest
from factories import T0

from hlidac.clock import DemoClock
from hlidac.config import Settings
from hlidac.models import (
    NAME_MASK,
    SEED,
    EndpointNotAllowed,
    ModelCallFailed,
    ModelGateway,
    PatientDataInInstructions,
    build_client,
    check_audio_destination,
    check_endpoint,
    pseudonymise,
)
from hlidac.store import InMemoryStore

AZURE = "https://vika-demo.openai.azure.com"
NAME = "Jana Nováková"
BIRTH_NUMBER = "765432/1234"
PHONE = "+420 777 123 456"
EMAIL = "jana.novakova@example.cz"
STREET = "Vinohradská 1234/12"
MESSAGE = (
    f"Dobrý den, tady {NAME}, RČ {BIRTH_NUMBER}. Od včera mám teplotu 38,5 a zimnici, "
    f"Janě je pořád zle. Volejte na {PHONE} nebo pište na {EMAIL}, "
    f"bydlím {STREET}, 120 00 Praha 2."
)
SECRETS = [
    "Jana",
    "Janě",
    "Nováková",
    BIRTH_NUMBER,
    "765432",
    PHONE,
    "777 123 456",
    EMAIL,
    "Vinohradská",
    "1234/12",
    "120 00",
]
SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
    "additionalProperties": False,
}
REPLY = '{"findings": []}'


def settings(**overrides) -> Settings:
    values = {
        "extraction_backend": "azure",
        "azure_openai_endpoint": AZURE,
        "azure_openai_api_key": "test-key",
        "azure_openai_deployment": "extract-pinned",
        "azure_openai_api_version": "2024-10-21",
        "local_base_url": "http://localhost:11434/v1",
        "local_model": "qwen2.5:7b-instruct",
    }
    return Settings(_env_file=None, **(values | overrides))


class Wire:
    """An HTTP transport that records every request and answers like a chat completion."""

    def __init__(
        self,
        status: int = 200,
        content: str | None = REPLY,
        finish_reason: str = "stop",
        refusal: str | None = None,
        error_code: str = "boom",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.status, self.content, self.finish_reason = status, content, finish_reason
        self.refusal, self.error_code, self.headers = refusal, error_code, headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            error = {"error": {"code": self.error_code, "message": "echo: Jana"}}
            return httpx.Response(self.status, json=error, headers=self.headers)
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-2024-08-06",
                "system_fingerprint": "fp_test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": self.content,
                            "refusal": self.refusal,
                        },
                        "finish_reason": self.finish_reason,
                    }
                ],
            },
        )

    def body(self) -> dict:
        [request] = self.requests
        return json.loads(request.content)


def gateway(wire: Wire, **overrides):
    config = settings(**overrides)
    store = InMemoryStore()
    client = build_client(config, transport=httpx.MockTransport(wire))
    return ModelGateway(config, store, DemoClock(T0), client), store


def extract(gw: ModelGateway, text: str = MESSAGE):
    return gw.extract_json(
        instructions="Vyplň schéma podle zprávy.",
        patient_text=text,
        schema=SCHEMA,
        schema_name="extraction",
        target="message:m1",
        known_names=[NAME],
    )


# --- the endpoint allow-list ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "backend"),
    [
        ("https://api.openai.com/v1", "azure"),
        ("https://api.openai.com/v1", "local"),
        ("http://vika-demo.openai.azure.com", "azure"),  # not HTTPS
        ("https://openai.azure.com.evil.example", "azure"),
        ("https://vika-demo.openai.azure.com", "local"),  # Azure is not local
        ("http://10.0.0.5:11434/v1", "local"),  # another machine is not local
        ("http://ollama.example:11434/v1", "local"),
        ("http://localhost:11434/v1", "azure"),
    ],
)
def test_patient_text_to_a_non_allow_listed_endpoint_raises(url, backend):
    with pytest.raises(EndpointNotAllowed):
        check_endpoint(url, backend, "PATIENT")


@pytest.mark.parametrize(
    ("url", "backend"),
    [
        (AZURE, "azure"),
        ("https://vika.cognitiveservices.azure.com/", "azure"),
        ("http://localhost:11434/v1", "local"),
        ("http://127.0.0.1:11434/v1", "local"),
        ("http://[::1]:11434/v1", "local"),
    ],
)
def test_allow_listed_endpoints_pass(url, backend):
    check_endpoint(url, backend, "PATIENT")


def test_a_misconfigured_endpoint_refuses_to_build_a_client():
    with pytest.raises(EndpointNotAllowed):
        build_client(settings(azure_openai_endpoint="https://api.openai.com"))
    with pytest.raises(EndpointNotAllowed):
        build_client(
            settings(extraction_backend="local", local_base_url="https://api.openai.com/v1")
        )


def test_a_client_swapped_to_another_endpoint_is_refused_before_sending():
    wire = Wire()
    gw, store = gateway(wire)
    gw._client = build_client(settings(extraction_backend="local")).with_options(
        base_url="https://api.openai.com/v1"
    )
    with pytest.raises(EndpointNotAllowed):
        extract(gw)
    assert wire.requests == []
    [entry] = store.list_audit()  # the refusal is audited, without the text
    assert entry.action == "model_call_refused"
    assert entry.details == {"host": "api.openai.com", "backend": "azure"}


def test_another_azure_resource_is_refused_even_on_an_azure_domain():
    wire = Wire()
    gw, _ = gateway(wire)
    gw._client = gw._client.with_options(base_url="https://other-tenant.openai.azure.com/openai/")
    with pytest.raises(EndpointNotAllowed):
        extract(gw)
    assert wire.requests == []


def test_a_redirect_to_another_host_is_not_followed():
    wire = Wire(status=307, headers={"location": "https://collector.example/steal"})
    gw, store = gateway(wire)
    with pytest.raises(ModelCallFailed):
        extract(gw)
    [request] = wire.requests  # no second request to the redirect target
    assert request.url.host == "vika-demo.openai.azure.com"
    assert not store.list_audit()[0].model_call.ok


def test_every_request_the_client_sends_is_checked():
    wire = Wire()
    client = build_client(settings(), transport=httpx.MockTransport(wire))
    with pytest.raises(EndpointNotAllowed):
        client._client.get("https://collector.example/")  # e.g. a redirect hop
    assert wire.requests == []


def test_proxy_settings_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    for backend in ("azure", "local"):
        http = build_client(settings(extraction_backend=backend))._client
        assert http.trust_env is False and http.follow_redirects is False
        assert not http._mounts  # no proxy transports


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"azure_openai_api_version": "2024-06-01"}, "api-version"),
        ({"azure_openai_endpoint": AZURE + "/openai/deployments/x"}, "resource root"),
        ({"azure_openai_deployment": None}, "deployment"),
        ({"extraction_backend": "local", "local_model": None}, "LOCAL_MODEL"),
    ],
)
def test_a_broken_configuration_fails_at_start(overrides, message):
    with pytest.raises(ValueError, match=message):
        build_client(settings(**overrides))


def test_the_api_key_is_never_in_the_settings_repr():
    assert "test-key" not in repr(settings())


# --- what is sent --------------------------------------------------------------------------------


def test_identifiers_are_absent_from_the_request_sent_to_azure():
    wire = Wire()
    gw, _ = gateway(wire)
    extract(gw)
    [request] = wire.requests
    assert request.url.host == "vika-demo.openai.azure.com"
    raw = request.content.decode()
    sent = json.loads(raw)["messages"][1]["content"]
    for secret in SECRETS:
        assert secret not in raw
        assert secret not in sent
    assert "teplotu 38,5 a zimnici" in sent  # the clinical content survives


def test_the_request_is_deterministic_schema_bound_and_pinned():
    wire = Wire()
    gw, _ = gateway(wire)
    extract(gw)
    body = wire.body()
    assert "/deployments/extract-pinned/" in wire.requests[0].url.path
    assert body["temperature"] == 0
    assert body["seed"] == SEED
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "extraction", "schema": SCHEMA, "strict": True},
    }
    # Only the approved instructions and the current message: no records, ids or dates.
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert set(body) == {"model", "messages", "temperature", "seed", "response_format"}


# --- the organisers' gateway: Azure API Management, OpenAI v1 surface (D39) ---------------------

APIM = "https://apim-test.azure-api.net/openai/v1"


def apim_settings(**overrides):
    return settings(azure_openai_endpoint=APIM, azure_openai_api_version=None, **overrides)


def test_the_v1_gateway_needs_no_api_version_and_sends_the_api_key_header():
    wire = Wire()
    config = apim_settings()
    client = build_client(config, transport=httpx.MockTransport(wire))
    gw = ModelGateway(config, InMemoryStore(), DemoClock(T0), client)
    extract(gw)
    [request] = wire.requests
    assert request.url.host == "apim-test.azure-api.net"
    assert request.url.path == "/openai/v1/chat/completions"
    assert "api-version" not in str(request.url)
    assert request.headers["api-key"] == "test-key"
    body = wire.body()
    assert body["model"] == "extract-pinned" and body["temperature"] == 0
    raw = request.content.decode()
    for secret in SECRETS:  # masked before it leaves, as on every backend
        assert secret not in raw


def test_a_reasoning_model_gets_its_effort_instead_of_temperature_and_the_audit_says_so():
    wire = Wire()
    config = apim_settings(azure_openai_reasoning_effort="low")
    store = InMemoryStore()
    client = build_client(config, transport=httpx.MockTransport(wire))
    extract(ModelGateway(config, store, DemoClock(T0), client))
    body = wire.body()
    assert "temperature" not in body
    assert body["reasoning_effort"] == "low" and body["seed"] == SEED
    assert body["response_format"]["json_schema"]["strict"] is True
    [entry] = store.list_audit()
    assert entry.details["reasoning_effort"] == "low"


def test_the_local_backend_never_gets_a_reasoning_effort():
    wire = Wire()
    config = settings(extraction_backend="local", azure_openai_reasoning_effort="low")
    client = build_client(config, transport=httpx.MockTransport(wire))
    extract(ModelGateway(config, InMemoryStore(), DemoClock(T0), client))
    assert wire.body()["temperature"] == 0 and "reasoning_effort" not in wire.body()


@pytest.mark.parametrize(
    "url",
    [
        "https://other-team.azure-api.net/openai/v1",  # another API Management instance
        "https://apim-test.azure-api.net.evil.example/openai/v1",
        "http://apim-test.azure-api.net/openai/v1",  # not HTTPS
    ],
)
def test_only_the_configured_gateway_host_passes(url):
    wire = Wire()
    config = apim_settings()
    gw = ModelGateway(
        config, InMemoryStore(), DemoClock(T0),
        build_client(config, transport=httpx.MockTransport(wire)),
    )  # fmt: skip
    gw._client = gw._client.with_options(base_url=url)
    with pytest.raises(EndpointNotAllowed):
        extract(gw)
    assert wire.requests == []


def test_a_gateway_path_other_than_v1_is_refused_at_start():
    with pytest.raises(ValueError, match="resource root"):
        build_client(settings(azure_openai_endpoint="https://apim-test.azure-api.net/other"))


def test_the_local_backend_stays_on_this_machine_and_is_masked_too():
    wire = Wire()
    gw, _ = gateway(wire, extraction_backend="local")
    reply = extract(gw)
    [request] = wire.requests
    assert request.url.host == "localhost"
    assert wire.body()["model"] == "qwen2.5:7b-instruct"
    for secret in SECRETS:
        assert secret not in request.content.decode()
    assert reply.text == REPLY


# --- the audit -----------------------------------------------------------------------------------


def test_every_call_is_audited_exactly_as_sent():
    wire = Wire()
    gw, store = gateway(wire)
    reply = extract(gw)
    [entry] = store.list_audit("message:m1")
    call = entry.model_call
    assert entry.action == "model_call" and entry.actor == "system" and entry.at == T0
    assert call.request_text == wire.body()["messages"][1]["content"]
    assert call.response_text == reply.text == REPLY
    assert (call.backend, call.endpoint_host, call.model) == (
        "azure",
        "vika-demo.openai.azure.com",
        "extract-pinned",
    )
    assert call.model_version == reply.model_version == "gpt-4o-2024-08-06"
    assert call.ok and call.data_class == "PATIENT" and call.purpose == "extraction"
    # Exactly what the model saw besides the message: instructions and schema, verbatim.
    assert entry.details["instructions"] == wire.body()["messages"][0]["content"]
    assert json.loads(entry.details["schema"]) == SCHEMA
    assert entry.details["schema_name"] == "extraction"
    assert entry.details["system_fingerprint"] == "fp_test"


def test_patient_data_in_the_instructions_is_refused():
    wire = Wire()
    gw, store = gateway(wire)
    for instructions in ("Pacientka Jana Nováková hlásí:", "Volejte 777 123 456."):
        with pytest.raises(PatientDataInInstructions):
            gw.extract_json(
                instructions=instructions,
                patient_text="je mi zle",
                schema=SCHEMA,
                schema_name="extraction",
                target="message:m1",
                known_names=[NAME],
            )
    assert wire.requests == []
    assert {e.action for e in store.list_audit()} == {"model_call_refused"}


@pytest.mark.parametrize(
    ("wire", "error"),
    [
        (Wire(status=500), "InternalServerError:boom"),
        (Wire(status=400, error_code="content_filter"), "BadRequestError:content_filter"),
        (Wire(content=None), "empty"),
        (Wire(content=""), "empty"),
        (Wire(content='{"findings": ['), "finish_reason=length"),
        (Wire(content='{"findings": []}', finish_reason="content_filter"), None),
        (Wire(content=None, refusal="Nemohu pomoci."), "refusal"),
    ],
)
def test_a_failed_call_raises_and_stays_in_the_audit(wire, error):
    if error == "finish_reason=length":
        wire.finish_reason = "length"  # truncated JSON is a failure, not a reply
    gw, store = gateway(wire)
    with pytest.raises(ModelCallFailed) as raised:
        extract(gw)
    assert raised.value.__cause__ is None  # the SDK error, which may echo the request, is dropped
    [entry] = store.list_audit()
    call = entry.model_call
    assert not call.ok
    assert call.error == (error or "finish_reason=content_filter")  # fixed labels, no messages
    assert "Jana" not in call.error
    assert call.response_text == ""
    assert "Jana" not in call.request_text


# --- audio ---------------------------------------------------------------------------------------


class Transcriber:
    def __init__(self, endpoint: str | None = None) -> None:
        self.model, self.endpoint, self.heard = "whisper-test", endpoint, []

    def __call__(self, audio: bytes) -> str:
        self.heard.append(audio)
        return "mám horečku"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com/v1/audio",
        "https://vika-demo.openai.azure.com",
        "http://localhost:9000",
    ],
)
def test_audio_never_leaves_the_app_process(endpoint):
    with pytest.raises(EndpointNotAllowed):
        check_audio_destination(endpoint)
    gw, store = gateway(Wire())
    transcriber = Transcriber(endpoint)
    with pytest.raises(EndpointNotAllowed):
        gw.transcribe(b"RIFF....", transcriber, target="message:m2")
    assert transcriber.heard == []
    [entry] = store.list_audit("message:m2")
    assert entry.action == "model_call_refused" and entry.details["backend"] == "audio"


def test_in_process_transcription_is_audited_without_the_audio():
    wire = Wire()
    gw, store = gateway(wire)
    audio = b"RIFF fake audio"
    assert gw.transcribe(audio, Transcriber(), target="message:m2") == "mám horečku"
    assert wire.requests == []  # no HTTP at all
    [entry] = store.list_audit("message:m2")
    call = entry.model_call
    assert (call.purpose, call.backend, call.endpoint_host) == (
        "transcription",
        "local",
        "in-process",
    )
    assert "fake audio" not in call.request_text
    assert call.request_text.startswith(f"[audio {len(audio)} B, sha256 ")
    assert call.response_text == "mám horečku"


# --- pseudonymisation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Volal jsem Janě Novákové.", "Volal jsem [JMÉNO] [JMÉNO]."),
        ("pan Novak (bez diakritiky)", "pan [JMÉNO] (bez diakritiky)"),
        ("RČ 7654321234", "RČ [RČ]"),
        ("číslo 777123456", "číslo [RČ]"),  # either way, the number is gone
        ("tel. 00420 777-123-456", "tel. [TELEFON]"),
        ("bydlím Na Příkopě 3", "bydlím [ADRESA]"),
        ("adresa: ulice Dlouhá 5, 110 00 Praha 1", "adresa: [ADRESA]"),
        ("Masarykova 12", "[ADRESA]"),
        # Found by the adversarial pass (verification of WP7):
        ("pište na jana.novakova@example.cz", "pište na [EMAIL]"),
        ("email jana tečka kral zavináč seznam tečka cz", "email [EMAIL]"),
        ("Volejte 777.123.456", "Volejte [TELEFON]"),
        ("číslo 777 12 34 56, ozvěte se", "číslo [TELEFON], ozvěte se"),
        ("+420 602 12 34 56", "[TELEFON]"),
        ("RČ 76 54 32/1234", "RČ [RČ]"),
        ("RČ: 765432-1234", "RČ: [RČ]"),
        ("RČ 455120/123", "RČ [RČ]"),  # born before 1954: 9 digits
        ("bydlím Lipová 5", "bydlím [ADRESA]"),
        ("Zahradní 12, Beroun", "[ADRESA], Beroun"),
        ("Lidicka 3", "[ADRESA]"),  # typed without diacritics
        ("namesti Miru 5", "[ADRESA]"),
        ("Lidicka\u0301 3", "[ADRESA]"),  # decomposed (NFD) input
        ("Jano, je mi zle.", "[JMÉNO], je mi zle."),  # vocative
        ("syn Novák volal", "syn [JMÉNO] volal"),  # the family name from "Nováková"
    ],
)
def test_pseudonymise_masks_identifiers(text, expected):
    assert pseudonymise(text, ["Jana Nováková", "Petr Novák"]) == expected


@pytest.mark.parametrize(
    ("names", "text"),
    [
        (["Eva Malá"], "Evě, Evu, Evy"),
        (["Marek Hájek"], "Marka, Markovi, Hájka"),
        (["Pavel Němec"], "Pavle, Pavlovi, Němcová"),
        (["Jiří Krejčí"], "Jiřího, Jiřímu, Krejčího"),
        (["Petr Novák"], "Novákovým, Petrův, novak76"),
    ],
)
def test_pseudonymise_masks_declined_names(names, text):
    assert pseudonymise(text, names) == ", ".join([NAME_MASK] * 3)


@pytest.mark.parametrize(
    "text",
    [
        "Teplota 38,5 od 12. 10., zvracela jsem 5x za den.",
        "Tlak 140/90, puls 96, váha 64 kg.",
        "Průjem 6 stolic denně, bolest 7 z 10.",
        "Dávka 100 mg, poslední 2. cyklus.",
        "Po chemoterapii 10 dní je mi teplo.",
        "tlak 140 90 Puls 96",
        "tep 110 115 120 bpm, trombocyty 150 000 000 na litr",
        "Poslední 3 dny průjem.",
        "Na ulici mi bylo zle 2 hodiny, na náměstí jsem 2x zvracela.",
        "Na CT 12. 10. mi řekli, že to roste.",
        "V Motole 3 dny zpátky mi dali Zofran.",
        "Každého 3 hodiny beru Novalgin, Na Paralen 500 to nereaguje.",
        "12.10.26 1500 kroků, 12 10 26 1000 mg",
        "Od 7:30 do 22:00 zvracení, krvácení z nosu.",
    ],
)
def test_pseudonymise_keeps_clinical_content(text):
    assert pseudonymise(text, ["Marie Teplá", "Eva Dvořáková"]) == text


def test_a_known_name_that_is_also_a_word_is_masked_everywhere():
    """Privacy first (D31): the surname "Černý" also masks "černou stolici". The red-flag scan
    (WP22) runs on the unmasked text, and unclear red-flag items are asked with buttons (G3)."""
    assert pseudonymise("Mám černou stolici.", ["Josef Černý"]) == "Mám [JMÉNO] stolici."
