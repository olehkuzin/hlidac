"""When the IRIS store tests run (WP10): only with HLIDAC_IRIS_TESTS=1, on their own schema."""

import os

IRIS_TESTS = os.environ.get("HLIDAC_IRIS_TESTS") == "1"
TEST_SCHEMA = "HlidacTest"
