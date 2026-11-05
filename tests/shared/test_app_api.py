# SPDX-License-Identifier: MIT
"""The app API's conventions, which every module that serves it shares."""

import uuid

import pytest
from fastapi import Request

from ai_cloud.shared.app_api import (
    CHALLENGE,
    bearer_token,
    over_the_rate,
    page_after,
    page_of,
    problem_of,
)
from ai_cloud.shared.problems import Problem


class Kept(Exception):
    pass


class Gone(Exception):
    pass


class Unknown(Exception):
    pass


def request(headers: dict[str, str]) -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def test_pages() -> None:
    items = [uuid.uuid7() for _ in range(3)]
    first = page_of(items, 2, lambda item: item)
    assert first.items == tuple(items[:2])
    assert page_after(first.next_page_token) == items[1]
    last = page_of(items[2:], 2, lambda item: item)
    assert last.items == (items[2],)
    assert last.next_page_token == ""
    assert page_after(None) is None
    assert page_after("") is None


@pytest.mark.parametrize("token", ["*", "abc", "AAAA", "é"])
def test_a_page_token_no_list_gave_is_refused(token: str) -> None:
    with pytest.raises(Problem) as refused:
        page_after(token)
    assert refused.value.status == 400


@pytest.mark.parametrize(
    "authorization", ["", "Bearer", "Bearer   ", "Basic dXNlcjpwYXNz", "Token abc"]
)
def test_a_request_without_a_bearer_token_is_refused(authorization: str) -> None:
    with pytest.raises(Problem) as refused:
        bearer_token(request({"Authorization": authorization} if authorization else {}))
    assert refused.value.status == 401
    assert refused.value.headers == CHALLENGE


def test_a_bearer_token_is_read_whatever_the_scheme_s_case() -> None:
    assert bearer_token(request({"Authorization": "Bearer abc"})) == "abc"
    assert bearer_token(request({"Authorization": "bearer  abc "})) == "abc"


def test_an_error_becomes_its_problem() -> None:
    typed = {Kept: ("kept_here", 409, "Kept here")}
    statuses = {Gone: 404, Exception: 401}
    kept = problem_of(Kept("it is kept"), typed, statuses)
    assert kept.body() == {
        "type": "/app/v1/problems/kept_here",
        "title": "Kept here",
        "status": 409,
        "detail": "it is kept",
    }
    gone = problem_of(Gone("no such thing"), typed, statuses)
    assert (gone.type, gone.status, gone.headers) == ("about:blank", 404, {})
    # A request whose access token failed is told how to authenticate again.
    assert problem_of(Unknown("who?"), typed, statuses).headers == CHALLENGE
    with pytest.raises(Unknown):
        problem_of(Unknown("who?"), typed, {Gone: 404})


def test_a_request_over_the_rate_is_told_when_to_try_again() -> None:
    problem = over_the_rate(1.2, "over the rate")
    assert (problem.status, problem.headers) == (429, {"Retry-After": "2"})
