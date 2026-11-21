"""The auth probe (/diag/auth-probe.json): off unless diag.auth_probe is on; shows
what a request carried (names, schemes, unverified JWT claims) and never a secret;
``?challenge=<tenant>`` answers a request without a token with Power Query's 401.
"""

import base64
import json
import logging
from typing import Any

import pytest
from flask.testing import FlaskClient

from tests.conftest import Strata

URL = "/diag/auth-probe.json"
ANON = {"X-Anon": "1"}
SIGNATURE = "SIGsecretSIGNATUREpart_42"
LOGGER = "strata.blueprints.diag"


def _segment(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _jwt(claims: dict[str, Any]) -> str:
    header = {"alg": "RS256", "kid": "key-1", "typ": "JWT"}
    return f"{_segment(header)}.{_segment(claims)}.{SIGNATURE}"


CLAIMS = {
    "aud": "https://reportsau.demant.com",
    "iss": "https://sts.windows.net/1b2c3d4e-0000-4000-8000-00000000abcd/",
    "tid": "1b2c3d4e-0000-4000-8000-00000000abcd",
    "upn": "jane@demant.com",
    "appid": "a672d62c-fc7b-4e81-a576-e60dc46e951d",
    "exp": 1795000000,
    "secret_claim": "hush-hush",
}


@pytest.fixture
def probe_on(strata: Strata) -> FlaskClient:
    strata.set_config({"diag.auth_probe": True})
    return strata.client


def _probe_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.getMessage().startswith("auth probe: ")
    ]


def test_off_by_default_for_everyone(strata: Strata, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert strata.client.get(URL, headers=ANON).status_code == 404
    assert strata.client.get(URL).status_code == 404  # an admin's session too
    assert _probe_lines(caplog) == []


def test_turning_it_off_is_404_at_once(strata: Strata, probe_on: FlaskClient) -> None:
    assert probe_on.get(URL, headers=ANON).status_code == 200
    strata.set_config({"diag.auth_probe": False})
    assert probe_on.get(URL, headers=ANON).status_code == 404


def test_an_anonymous_request_shows_what_arrived(probe_on: FlaskClient) -> None:
    probe_on.set_cookie("AzureAppProxyAccessCookie_abc_1.3", "cookie-secret-value")
    ua = "Microsoft.Data.Mashup (https://go.microsoft.com/fwlink/?LinkID=304225)"
    resp = probe_on.get(URL, headers={**ANON, "User-Agent": ua, "X-Forwarded-For": "10.1.2.3"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()
    assert data["reached_strata"] is True
    assert data["user_agent"] == ua
    assert "AzureAppProxyAccessCookie_abc_1.3" in data["cookies"]  # beside Flask's session
    assert "User-Agent" in data["headers"] and "X-Forwarded-For" in data["headers"]
    assert data["forwarded"] == {"X-Forwarded-For": "10.1.2.3"}
    assert data["gatekeeper_user"] is None
    assert data["authorization"] is None
    assert data["challenge"] is None and data["challenged"] is False
    assert "cookie-secret-value" not in resp.get_data(as_text=True)


def test_a_session_shows_who_gatekeeper_sees(probe_on: FlaskClient) -> None:
    assert probe_on.get(URL).get_json()["gatekeeper_user"] == "tester"


def test_a_bearer_jwt_is_summarised_never_echoed(
    probe_on: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    token = _jwt(CLAIMS)
    resp = probe_on.get(URL, headers={**ANON, "Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    auth = resp.get_json()["authorization"]
    assert auth["scheme"] == "Bearer" and auth["empty"] is False
    assert auth["length"] == len(token)
    jwt = auth["jwt"]
    assert jwt["header"] == {"alg": "RS256", "kid": "key-1", "typ": "JWT"}
    assert jwt["claims"] == {k: v for k, v in CLAIMS.items() if k != "secret_claim"}
    assert jwt["exp_utc"].startswith("2026-")
    [line] = _probe_lines(caplog)
    for text in (resp.get_data(as_text=True), line):
        assert token not in text and SIGNATURE not in text
        assert "hush-hush" not in text  # claims outside the list stay out


@pytest.mark.parametrize(
    ("header", "expected", "secret"),
    [
        ("Bearer", {"scheme": "Bearer", "empty": True, "length": 0}, None),
        (
            "Bearer opaque-token-value-123",
            {"scheme": "Bearer", "empty": False, "length": 22, "jwt": None},
            "opaque-token-value-123",
        ),
        # user:hunter2
        ("Basic dXNlcjpodW50ZXIy", {"scheme": "Basic"}, "dXNlcjpodW50ZXIy"),
        ("Negotiate YIIGsecretKerberos", {"scheme": "Negotiate"}, "YIIGsecretKerberos"),
        # no scheme at all: the whole value may be a token
        (_jwt(CLAIMS), {"scheme": None, "length": len(_jwt(CLAIMS))}, SIGNATURE),
    ],
)
def test_authorization_shows_its_shape_only(
    probe_on: FlaskClient, header: str, expected: dict[str, Any], secret: str | None
) -> None:
    resp = probe_on.get(URL, headers={**ANON, "Authorization": header})
    assert resp.get_json()["authorization"] == expected
    if secret is not None:
        assert secret not in resp.get_data(as_text=True)


def test_proxy_authorization_shows_its_scheme_only(probe_on: FlaskClient) -> None:
    resp = probe_on.get(URL, headers={**ANON, "Proxy-Authorization": "Basic dXNlcjpodW50ZXIy"})
    assert resp.get_json()["proxy_authorization"] == {"scheme": "Basic"}
    assert "dXNlcjpodW50ZXIy" not in resp.get_data(as_text=True)


def test_identity_headers_show_names_emails_and_jwt_claims_only(probe_on: FlaskClient) -> None:
    token = _jwt(CLAIMS)
    opaque = "eyJhdXRoX3R5cCI6ImFhZCIsImNsYWltcyI6W119" * 3
    guid = "6f1d2c3b-1111-4222-8333-944455556666"
    resp = probe_on.get(
        URL,
        headers={
            **ANON,
            "X-MS-Client-Principal-Name": "jane@demant.com",
            "X-MS-Token-AAD-Access-Token": token,
            "X-MS-Client-Principal": opaque,
            "X-Remote-User-Id": guid,
        },
    )
    ids = {k.lower(): v for k, v in resp.get_json()["identity_headers"].items()}
    assert ids["x-ms-client-principal-name"] == "jane@demant.com"
    assert ids["x-ms-token-aad-access-token"]["jwt"]["claims"]["upn"] == "jane@demant.com"
    assert ids["x-ms-client-principal"] == {"length": len(opaque)}
    assert ids["x-remote-user-id"] == guid
    assert "user-agent" not in ids and "x-anon" not in ids
    text = resp.get_data(as_text=True)
    assert token not in text and SIGNATURE not in text and opaque not in text


@pytest.mark.parametrize("authorization", [None, "Bearer", "Bearer   "])
@pytest.mark.parametrize("tenant", ["demant.com", "1b2c3d4e-0000-4000-8000-00000000abcd"])
def test_a_challenge_without_a_token_gets_power_querys_401(
    probe_on: FlaskClient, tenant: str, authorization: str | None
) -> None:
    headers = dict(ANON) if authorization is None else {**ANON, "Authorization": authorization}
    resp = probe_on.get(URL, query_string={"challenge": tenant}, headers=headers)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == (
        f"Bearer authorization_uri=https://login.microsoftonline.com/{tenant}/oauth2/authorize"
    )
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()
    assert data["challenge"] == tenant and data["challenged"] is True
    assert data["query_keys"] == ["challenge"]


@pytest.mark.parametrize(
    "authorization", [f"Bearer {_jwt(CLAIMS)}", "Bearer opaque-1", "Basic eA=="]
)
def test_a_challenge_with_credentials_is_answered(
    probe_on: FlaskClient, authorization: str
) -> None:
    resp = probe_on.get(
        URL,
        query_string={"challenge": "demant.com"},
        headers={**ANON, "Authorization": authorization},
    )
    assert resp.status_code == 200 and "WWW-Authenticate" not in resp.headers
    assert resp.get_json()["challenged"] is False


def test_no_challenge_is_never_a_401(probe_on: FlaskClient) -> None:
    resp = probe_on.get(URL, headers={**ANON, "Authorization": "Bearer"})
    assert resp.status_code == 200 and "WWW-Authenticate" not in resp.headers


@pytest.mark.parametrize("tenant", ["evil.com/x", "a b", "", "x" * 65, 'a"b', "demant.com\r\nX: y"])
def test_a_bad_tenant_is_400(
    probe_on: FlaskClient, caplog: pytest.LogCaptureFixture, tenant: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    resp = probe_on.get(URL, query_string={"challenge": tenant}, headers=ANON)
    assert resp.status_code == 400 and "WWW-Authenticate" not in resp.headers
    assert "tenant id or domain" in resp.get_json()["error"]
    assert _probe_lines(caplog) == []


def test_each_hit_logs_one_line_with_the_answer(
    probe_on: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    first = probe_on.get(URL, headers=ANON).get_json()
    second = probe_on.get(URL, query_string={"challenge": "demant.com"}, headers=ANON).get_json()
    lines = _probe_lines(caplog)
    assert [json.loads(line.removeprefix("auth probe: ")) for line in lines] == [first, second]
