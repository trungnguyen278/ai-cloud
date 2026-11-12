# SPDX-License-Identifier: MIT
"""The devices' rules, without I/O: where a registration's device stands, and what a
registration must be (platform/protocols/setup/README.md, The registration)."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from device import Device, b64url_json
from forge import hs256, public_pem

from ai_cloud.devices.domain import link
from ai_cloud.devices.domain.errors import RegistrationInvalid
from ai_cloud.devices.domain.model import (
    Binding,
    binding,
    challenge_hash,
    named_again,
    new_challenge,
    renamed,
)
from ai_cloud.devices.domain.model import Device as Registered
from ai_cloud.devices.domain.registration import challenge_of, verify
from ai_cloud.shared.jose import b64url
from ai_cloud.shared.protocols import platform_dir, protocol_schema

SERVER = "https://cloud.example"
HOME = uuid.uuid7()
REGISTERED = datetime(2026, 11, 5, 9, 0, 3, tzinfo=UTC)


def example_registrations() -> list[tuple[str, str]]:
    """The registrations of the app API's example, with the challenge each answers."""
    example = json.loads(
        (platform_dir() / "app" / "examples" / "devices.json").read_text(encoding="utf-8")
    )
    found = []
    challenge = ""
    for message in example["messages"]:
        body = message.get("message", {})
        challenge = body.get("challenge", challenge)
        if "registration" in body:
            found.append((body["registration"], challenge))
    return found


def held(
    device_id: uuid.UUID, home_id: uuid.UUID = HOME, name: str | None = "Robot", version: int = 1
) -> Registered:
    return Registered(
        id=device_id,
        home_id=home_id,
        create_time=REGISTERED,
        description=None,
        name=name,
        version=version,
    )


def test_a_challenge_is_32_random_bytes_kept_as_its_hash() -> None:
    challenge = new_challenge()
    assert protocol_schema("app", "challenge").problem(challenge) is None
    assert new_challenge() != challenge
    assert challenge_hash(challenge) == hashlib.sha256(challenge.encode()).digest()


def test_where_a_registration_s_device_stands() -> None:
    device, other = uuid.uuid7(), uuid.uuid7()
    assert binding(HOME, None, None) is Binding.NEW
    # Already in the home, with its identifier and its key: it lost its state.
    assert binding(HOME, held(device), device) is Binding.SAME
    # In another home.
    assert binding(HOME, held(device, uuid.uuid7()), device) is Binding.TAKEN
    # Its identifier with another key, or none the cloud holds.
    assert binding(HOME, held(device), other) is Binding.TAKEN
    assert binding(HOME, held(device), None) is Binding.TAKEN
    # Its key, another device's.
    assert binding(HOME, None, other) is Binding.TAKEN


def test_a_device_s_name_and_its_etag() -> None:
    device = held(uuid.uuid7(), version=4)
    assert device.etag == 'W/"4"'
    # Renaming changes its version, and its ETag with it, whatever the name was.
    assert renamed(device, "Bi") == held(device.id, name="Bi", version=5)
    assert renamed(held(device.id, name=None), "Bi").name == "Bi"
    # Registered again into its home, it keeps the name it has, and takes the registration's
    # only if it has none: an app of 1.4 sends none.
    assert named_again(device, "Robot 2") is None
    assert named_again(device, None) is None
    assert named_again(held(device.id, name=None), None) is None
    taken = named_again(held(device.id, name=None, version=4), "Robot")
    assert taken == held(device.id, name="Robot", version=5)


def test_the_example_s_registrations_verify() -> None:
    registrations = example_registrations()
    assert len(registrations) == 6
    for registration, challenge in registrations:
        assert challenge_of(registration) == challenge
        verified = verify(registration, audience=SERVER)
        assert verified.device_id == uuid.UUID("4a4ce4bd-2853-43d8-98dc-3dc06daec6e5")
        # As the example says, its key's thumbprint.
        assert verified.key.thumbprint == "A9J9ACDRqGsoYjAJJK-upx23ibV7l4Bofp0C6uidqC8"
        assert set(verified.key.jwk) == {"kty", "crv", "x", "y"}


def test_a_registration_names_the_device_and_its_key() -> None:
    device = Device()
    verified = verify(device.registration(new_challenge(), SERVER), audience=SERVER)
    assert verified.device_id == device.id
    assert verified.key.thumbprint == device.thumbprint
    assert verified.key.jwk == device.jwk


def test_identifiers_compare_as_uuids_whatever_their_case() -> None:
    device = Device()
    claims = {"iss": str(device.id).upper(), "sub": str(device.id)}
    registration = device.registration(new_challenge(), SERVER, claims=claims)
    assert verify(registration, audience=SERVER).device_id == device.id


def test_a_registration_s_times_do_not_matter() -> None:
    # Before it first connects, the device's clock may not be set.
    device = Device()
    claims = {"exp": 1, "iat": 4_000_000_000, "nbf": 4_000_000_000}
    registration = device.registration(new_challenge(), SERVER, claims=claims)
    assert verify(registration, audience=SERVER).device_id == device.id


def test_a_registration_s_challenge_is_read_before_it_is_verified() -> None:
    device = Device()
    challenge = new_challenge()
    assert challenge_of(device.registration(challenge, SERVER, signer=Device().key)) == challenge


@pytest.mark.parametrize(
    "registration",
    [
        "not.a.jwt",
        "e30.e30.",  # no nonce
        "e30." + b64url_json({"nonce": 42}) + ".",
        "e30." + b64url_json({"nonce": "short"}) + ".",
        "e30." + b64url_json({"nonce": "\ud800" * 43}) + ".",
        "e30." + b64url_json({"nonce": "a/b+" * 11}) + ".",
    ],
    ids=["malformed", "none", "number", "short", "surrogates", "not-base64url"],
)
def test_a_registration_without_a_challenge_names_none(registration: str) -> None:
    assert challenge_of(registration) is None


def forged(**changes: Any) -> str:
    """A registration as a broken device, or someone else, would make it."""
    return Device().registration(new_challenge(), SERVER, **changes)


def jwk_with(**members: str) -> dict[str, str]:
    return {**Device().jwk, **members}


def off_curve() -> dict[str, str]:
    jwk = Device().jwk
    y = bytearray(jwk["y"].encode())
    y[0] = ord("A") if y[0] != ord("A") else ord("B")
    return {**jwk, "y": y.decode()}


@pytest.mark.parametrize(
    ("registration", "why"),
    [
        ("a.b.c", "malformed"),
        (forged(header={"typ": "JWT"}), "not a device's registration"),
        (forged(without=("typ",)), "not a device's registration"),
        (forged(without=("jwk",)), "its key"),
        (forged(header={"jwk": jwk_with(d=b64url(bytes(32)))}), "a private key"),
        (forged(header={"jwk": jwk_with(crv="P-384")}), "not a P-256 key"),
        (forged(header={"jwk": jwk_with(kty="RSA")}), "not a P-256 key"),
        (forged(header={"jwk": jwk_with(x="AA")}), "wrong size"),
        (forged(header={"jwk": jwk_with(x="")}), "wrong size"),
        (forged(header={"jwk": {k: v for k, v in Device().jwk.items() if k != "y"}}), "no coord"),
        (forged(header={"jwk": off_curve()}), "not a point of the curve"),
        (forged(header={"jwk": "a key"}), "not a P-256 key"),
        (forged(signer=Device().key), "bad signature"),
        (forged(claims={"aud": "https://elsewhere.example"}), "wrong audience"),
        (forged(claims={"aud": [SERVER]}), "wrong audience"),
        (forged(without=("aud",)), "no aud claim"),
        (forged(without=("iss",)), "no iss claim"),
        (forged(without=("sub",)), "no sub claim"),
        (forged(without=("nonce",)), "no nonce claim"),
        (forged(claims={"iss": str(uuid.uuid7())}), "iss and sub differ"),
        (forged(claims={"iss": "robot", "sub": "robot"}), "not identifiers"),
        (forged(claims={"iss": 7}), "not identifiers"),
        (
            forged(claims={"iss": "4a4ce4bd285343d898dc3dc06daec6e5"}),
            "not identifiers",
        ),
    ],
)
def test_what_a_registration_must_be(registration: str, why: str) -> None:
    with pytest.raises(RegistrationInvalid, match=why):
        verify(registration, audience=SERVER)


def test_no_other_algorithm_passes() -> None:
    device = Device()
    claims = {"iss": str(device.id), "sub": str(device.id), "aud": SERVER, "nonce": "x" * 43}
    header = {"typ": "device-registration+jwt", "jwk": device.jwk}
    # The algorithm confusion of RFC 8725, 2.1: the public key as an HMAC secret.
    confused = hs256(claims, public_pem(device.key), **header)
    with pytest.raises(RegistrationInvalid, match="wrong algorithm"):
        verify(confused, audience=SERVER)
    unsigned = f"{b64url_json({'alg': 'none', **header})}.{b64url_json(claims)}."
    with pytest.raises(RegistrationInvalid, match="wrong algorithm"):
        verify(unsigned, audience=SERVER)


def test_a_refusal_says_nothing_the_registration_holds() -> None:
    device = Device()
    challenge = new_challenge()
    registration = device.registration(challenge, SERVER, claims={"aud": "https://x.example"})
    with pytest.raises(RegistrationInvalid) as refused:
        verify(registration, audience=SERVER)
    for secret in (challenge, str(device.id), "x.example", device.jwk["x"]):
        assert secret not in str(refused.value)


def test_an_error_s_message_is_as_long_as_the_cloud_link_allows() -> None:
    error = link.error(7, link.Code.INTERNAL, "x" * 2000)
    assert error == {"type": "error", "reply_to": 7, "code": "internal", "message": "x" * 1024}
    assert protocol_schema("cloud", "error").problem(error) is None
