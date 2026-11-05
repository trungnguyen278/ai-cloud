# SPDX-License-Identifier: MIT
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ai_cloud.accounts.domain.model import (
    DISPLAY_NAME_MAX,
    Invitation,
    Member,
    Role,
    SignIn,
    display_name_for,
    has_control_characters,
    invitation_usable,
    may_change,
    may_manage,
    new_invitation_code,
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


def test_an_invitation_code_is_what_the_app_api_says() -> None:
    codes = {new_invitation_code() for _ in range(100)}
    assert len(codes) == 100
    schema = protocol_schema("app", "invitation_code")
    assert all(schema.problem(code) is None for code in codes)


def member(role: Role | None) -> Member:
    account = None if role is None else uuid.uuid7()
    return Member(uuid.uuid7(), uuid.uuid7(), account, "Linh", role, datetime.now(UTC), 1)


@pytest.mark.parametrize(
    ("role", "allowed"),
    [(Role.OWNER, True), (Role.ADMIN, True), (Role.MEMBER, False), (None, False)],
)
def test_who_manages_a_home(role: Role | None, allowed: bool) -> None:
    assert may_manage(member(role)) is allowed


@pytest.mark.parametrize(
    ("actor", "target", "allowed"),
    [
        (Role.OWNER, Role.ADMIN, True),
        (Role.OWNER, Role.MEMBER, True),
        (Role.OWNER, None, True),
        (Role.ADMIN, Role.OWNER, False),
        (Role.ADMIN, Role.ADMIN, False),
        (Role.ADMIN, Role.MEMBER, True),
        (Role.ADMIN, None, True),
        (Role.MEMBER, Role.OWNER, False),
        (Role.MEMBER, Role.ADMIN, False),
        (Role.MEMBER, Role.MEMBER, False),
        (Role.MEMBER, None, False),
    ],
)
def test_who_changes_whom(actor: Role, target: Role | None, allowed: bool) -> None:
    assert may_change(member(actor), member(target)) is allowed


def invitation(expire_time: datetime, used_by: uuid.UUID | None = None) -> Invitation:
    return Invitation(
        uuid.uuid7(),
        uuid.uuid7(),
        uuid.uuid7(),
        "Linh",
        expire_time - timedelta(days=3),
        expire_time,
        used_by,
    )


def test_a_code_expires_at_its_expire_time() -> None:
    moment = datetime.now(UTC)
    assert invitation_usable(invitation(moment + timedelta(seconds=1)), None, moment)
    assert not invitation_usable(invitation(moment), None, moment)


def test_a_used_code_works_only_for_the_person_it_brought_in() -> None:
    moment = datetime.now(UTC)
    asker = member(Role.MEMBER)
    later = moment + timedelta(days=1)
    assert invitation_usable(invitation(later, asker.id), asker, moment)
    assert not invitation_usable(invitation(later, uuid.uuid7()), asker, moment)
    assert not invitation_usable(invitation(later, asker.id), None, moment)
    assert not invitation_usable(invitation(moment, asker.id), asker, moment)
