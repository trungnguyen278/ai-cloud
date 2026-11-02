# SPDX-License-Identifier: MIT
import re
from datetime import UTC, datetime

import pytest

from ai_cloud.accounts.domain.model import (
    DISPLAY_NAME_MAX,
    SignIn,
    display_name_for,
    has_control_characters,
    new_nonce,
    new_refresh_token,
    token_hash,
)
from ai_cloud.shared.protocols import protocol_schema


def sign_in(name: str | None, email: str | None) -> SignIn:
    return SignIn("google", "1", email, name, datetime.now(UTC), "n")


@pytest.mark.parametrize(
    ("name", "email", "display_name"),
    [
        ("Linh", "linh@example.com", "Linh"),
        ("  Linh Nguyễn \n", None, "Linh Nguyễn"),
        ("Li\u0000nh", None, "Linh"),
        (None, "minh.anh@example.com", "minh.anh"),
        ("\u0007 ", "minh.anh@example.com", "minh.anh"),
        ("x" * 150, None, "x" * DISPLAY_NAME_MAX),
        (None, None, None),
        ("", "@example.com", None),
    ],
)
def test_the_name_an_account_starts_with(
    name: str | None, email: str | None, display_name: str | None
) -> None:
    assert display_name_for(sign_in(name, email)) == display_name


def test_control_characters() -> None:
    assert has_control_characters("a\u0000b")
    assert has_control_characters("a\nb")
    assert not has_control_characters("Linh Nguyễn 🙂")


def test_a_nonce_is_what_the_app_api_says() -> None:
    nonces = {new_nonce() for _ in range(100)}
    assert len(nonces) == 100
    schema = protocol_schema("app", "nonce")
    assert all(schema.problem(nonce) is None for nonce in nonces)


def test_a_refresh_token_is_kept_as_its_sha256() -> None:
    token = new_refresh_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert token_hash(token) == token_hash(token)
    assert len(token_hash(token)) == 32
    assert token_hash(token) != token_hash(new_refresh_token())
