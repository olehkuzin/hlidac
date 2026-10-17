"""WP30: the login-code rules, without HTTP."""

import pytest
from factories import T0, patient_account

from hlidac.security.codes import (
    CODE_TTL,
    RateLimited,
    hash_code,
    issue_code,
    verify_code,
)
from hlidac.store import InMemoryStore

SECRET = b"k" * 32


@pytest.fixture
def store() -> InMemoryStore:
    store = InMemoryStore()
    store.save_account(patient_account())
    return store


def test_code_works_once(store):
    code = issue_code(store, "acc-p1", T0, SECRET)
    assert len(code) == 6 and code.isdigit()
    assert verify_code(store, "acc-p1", code, T0, SECRET) == "ok"
    assert verify_code(store, "acc-p1", code, T0, SECRET) == "used"


def test_code_expires(store):
    code = issue_code(store, "acc-p1", T0, SECRET)
    assert verify_code(store, "acc-p1", code, T0 + CODE_TTL, SECRET) == "expired"


def test_pasted_code_with_spaces_works(store):
    code = issue_code(store, "acc-p1", T0, SECRET)
    assert verify_code(store, "acc-p1", f" {code[:3]} {code[3:]} ", T0, SECRET) == "ok"


def test_no_code_issued_means_invalid(store):
    assert verify_code(store, "acc-p1", "123456", T0, SECRET) == "invalid"


def test_hash_depends_on_the_server_key():
    assert hash_code(SECRET, "c1", "123456") != hash_code(b"other" * 8, "c1", "123456")
    assert hash_code(SECRET, "c1", "123456") != hash_code(SECRET, "c2", "123456")


def test_rate_limit_raises(store):
    for _ in range(3):
        issue_code(store, "acc-p1", T0, SECRET)
    with pytest.raises(RateLimited):
        issue_code(store, "acc-p1", T0, SECRET)
