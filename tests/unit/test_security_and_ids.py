"""Unit: argon2 verify (constant-time) + opaque-ID генерация.

docs/05-security.md, docs/03-data-model.md.
"""

from __future__ import annotations

import re

from app.core import ids
from app.core.security import hash_api_key, verify_api_key


def test_hash_is_argon2id_and_verifies():
    h = hash_api_key("secret-key")
    assert h.startswith("$argon2id$")
    assert verify_api_key("secret-key", h) is True


def test_verify_rejects_wrong_key():
    h = hash_api_key("secret-key")
    assert verify_api_key("wrong-key", h) is False


def test_verify_rejects_corrupt_hash_without_raising():
    assert verify_api_key("any", "not-a-valid-hash") is False
    assert verify_api_key("any", "") is False


def test_hash_is_salted_unique_per_call():
    assert hash_api_key("same") != hash_api_key("same")


def test_prefixed_ids_have_correct_prefix_and_alphabet():
    cases = {
        ids.new_user_id: "u_",
        ids.new_project_id: "p_",
        ids.new_job_id: "j_",
        ids.new_revision_id: "r_",
        ids.new_deployment_id: "d_",
        ids.new_question_id: "q_",
        ids.new_answer_id: "a_",
    }
    for fn, prefix in cases.items():
        value = fn()
        assert value.startswith(prefix)
        body = value[len(prefix) :]
        assert re.fullmatch(r"[a-z0-9]{24}", body), value


def test_subdomain_is_opaque_16_lower_alnum_no_prefix():
    sub = ids.new_subdomain()
    assert re.fullmatch(r"[a-z0-9]{16}", sub), sub
    assert not sub.startswith(("u_", "p_", "j_", "d_"))


def test_ids_are_unique():
    assert len({ids.new_job_id() for _ in range(50)}) == 50
