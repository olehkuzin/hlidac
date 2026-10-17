#!/usr/bin/env bash
# Reset our demo writes in IRIS (WP14, D43): removes only our SQL rows and hlidac-tagged FHIR
# resources, and checks that hospital resource counts are identical before and after.
set -euo pipefail

cd "$(dirname "$0")/.."
exec uv run python -m hlidac.iris.reset "$@"
