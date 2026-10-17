"""The Azure Container Instances spec for a release (WP32), printed as JSON for
`az container create --file`. Run by azure-pipelines.yml; reads everything from the environment.

The container group has two containers on one network: `app` (the backend image, port 8000, never
public) and `https` (Caddy, ports 80 and 443), which gets a certificate for the group's
azurecontainer.io name and forwards to the app. HTTPS is required: the login cookie is Secure.
Secrets go only into `secureValue`, which Azure doesn't show back; the spec file holds them, so
the pipeline deletes it after use.
"""

import json
import os
import sys

# App settings passed through when set (config.py reads them). STORE_BACKEND, SESSION_HTTPS_ONLY,
# OUTBOX_RUNNER, APP_BASE_URL and DEMO_SKIP_LOGIN are fixed below. Not passed: WHISPER_MODEL (mlx
# needs a Mac; use VOICE_BACKEND=elevenlabs or server) and the IRIS admin account (setup only).
PLAIN = [
    "AS_OF",
    "IRIS_HOST",
    "IRIS_PORT",
    "IRIS_NAMESPACE",
    "IRIS_USER",
    "FHIR_BASE_URL",
    "EXTRACTION_BACKEND",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_REASONING_EFFORT",
    "LOCAL_BASE_URL",
    "LOCAL_MODEL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "MAIL_FROM",
    "SMTP_ALLOWED_RECIPIENTS",
    "DEMO_PANEL",
    "DEMO_MAILBOX_URL",
    "CHECKIN_INVITE_MINUTES",
    "CHECKIN_INVITE_TEAM",
    "VOICE_BACKEND",
    "WHISPER_SERVER_URL",
    "WHISPER_SERVER_MODEL",
    "ELEVENLABS_STT_MODEL",
]
SECRET = [
    "SECRET_KEY",
    "IRIS_PASSWORD",
    "AZURE_OPENAI_API_KEY",
    "SMTP_PASSWORD",
    "WHISPER_SERVER_API_KEY",
    "ELEVENLABS_API_KEY",
]
REQUIRED = [
    "ACI_NAME",
    "LOCATION",
    "DNS_LABEL",
    "IMAGE",
    "PROXY_IMAGE",
    "ACR_LOGIN_SERVER",
    "ACR_USERNAME",
    "ACR_PASSWORD",
    "SECRET_KEY",  # without it every restart would log everyone out
    "IRIS_PASSWORD",
]


def spec(env: dict[str, str]) -> dict:
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise SystemExit(f"missing pipeline variables: {', '.join(missing)}")
    fqdn = f"{env['DNS_LABEL']}.{env['LOCATION']}.azurecontainer.io"
    settings = [
        {"name": "STORE_BACKEND", "value": "iris"},
        {"name": "SESSION_HTTPS_ONLY", "value": "true"},
        {"name": "OUTBOX_RUNNER", "value": "app"},
        {"name": "APP_BASE_URL", "value": f"https://{fqdn}"},
        # Behind the proxy every request comes from localhost, which the skip-login button trusts.
        {"name": "DEMO_SKIP_LOGIN", "value": "false"},
    ]
    settings += [{"name": n, "value": env[n]} for n in PLAIN if env.get(n)]
    settings += [{"name": n, "secureValue": env[n]} for n in SECRET if env.get(n)]
    return {
        "apiVersion": "2023-05-01",
        "type": "Microsoft.ContainerInstance/containerGroups",
        "name": env["ACI_NAME"],
        "location": env["LOCATION"],
        "properties": {
            "osType": "Linux",
            "restartPolicy": "Always",
            "imageRegistryCredentials": [
                {
                    "server": env["ACR_LOGIN_SERVER"],
                    "username": env["ACR_USERNAME"],
                    "password": env["ACR_PASSWORD"],
                }
            ],
            "ipAddress": {
                "type": "Public",
                "dnsNameLabel": env["DNS_LABEL"],
                "ports": [{"protocol": "TCP", "port": 80}, {"protocol": "TCP", "port": 443}],
            },
            "containers": [
                {
                    "name": "app",
                    "properties": {
                        "image": env["IMAGE"],
                        "ports": [{"port": 8000}],
                        "environmentVariables": settings,
                        "resources": {"requests": {"cpu": 1.0, "memoryInGB": 1.5}},
                        # No livenessProbe: ACI's httpGet probe counted 200 answers as failures
                        # and killed the app; restartPolicy Always restarts a crashed app anyway.
                    },
                },
                {
                    "name": "https",
                    "properties": {
                        "image": env["PROXY_IMAGE"],
                        "command": [
                            "caddy", "reverse-proxy", "--from", fqdn, "--to", "localhost:8000"
                        ],
                        "ports": [{"port": 80}, {"port": 443}],
                        "resources": {"requests": {"cpu": 0.5, "memoryInGB": 0.5}},
                    },
                },
            ],
        },
    }


def pipeline_env(environ: dict[str, str]) -> dict[str, str]:
    """Azure DevOps passes a variable missing from the group as the literal text `$(NAME)`; that
    means unset, never a value."""
    return {k: v for k, v in environ.items() if v.strip() and v.strip() != f"$({k})"}


if __name__ == "__main__":
    json.dump(spec(pipeline_env(dict(os.environ))), sys.stdout)
