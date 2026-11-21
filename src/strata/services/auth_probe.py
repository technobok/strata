"""The auth probe (``/diag/auth-probe.json``): what a request carried when it reached Strata.

It exists to find out how Excel gets past the Entra application proxy in front
of Strata (ROADMAP XL), so it reports names and shapes, never secrets:

- header and cookie names (values only for the X-Forwarded-* / Via headers);
- the ``Authorization`` scheme, and for a bearer JWT its **unverified** header
  fields and a fixed list of claims -- never the token or its signature;
- Basic, Negotiate and NTLM credentials: the scheme only;
- identity-looking headers (``X-MS-*``, ``*-User``, ...): a JWT is summarised the
  same way, an email or GUID is shown, anything else only by its length.

``?challenge=<tenant>`` makes a request with no token (or Power Query's empty
``Authorization: Bearer``) get the 401 that starts Excel's "Organizational
account" sign-in, naming that tenant's Entra authority.
"""

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

from flask import Request, g

# The JWT fields that say who the token is for (aud), from whom (iss, tid), about
# whom (oid, upn, ...), through which client (appid, azp) and until when.
JWT_HEADER = ("alg", "kid", "typ")
JWT_CLAIMS = (
    "aud",
    "iss",
    "tid",
    "oid",
    "upn",
    "preferred_username",
    "unique_name",
    "email",
    "appid",
    "azp",
    "scp",
    "ver",
    "iat",
    "nbf",
    "exp",
)
MAX_TEXT = 200  # longest string echoed from a header or a claim

# A tenant id (GUID) or a domain: Entra accepts either in the authority's path.
TENANT = re.compile(r"[A-Za-z0-9.-]{1,64}")
AUTHORITY = "https://login.microsoftonline.com"

FORWARDED = (
    "X-Forwarded-For",
    "X-Forwarded-Proto",
    "X-Forwarded-Host",
    "X-Forwarded-Prefix",
    "Via",
)
_IDENTITY_WORDS = ("user", "principal", "email", "upn", "identity", "token")
_NOT_IDENTITY = frozenset({"authorization", "proxy-authorization", "cookie", "user-agent"})
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,30}")
_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def describe(req: Request, tenant: str | None) -> dict[str, Any]:
    """What ``req`` carried; ``tenant`` is the request's valid ``?challenge=`` or None."""
    headers = req.headers
    user = g.get("user")
    auth = authorization(headers.get("Authorization"))
    return {
        "reached_strata": True,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": req.method,
        "path": req.path,
        "query_keys": sorted(req.args.keys()),
        "host": req.host,
        "remote_addr": req.remote_addr,
        "forwarded": {name: _clip(headers[name]) for name in FORWARDED if name in headers},
        "user_agent": _clip(headers.get("User-Agent")),
        "headers": sorted({name for name, _value in headers.items()}),
        "cookies": sorted(req.cookies.keys()),
        "gatekeeper_user": getattr(user, "username", None) if user is not None else None,
        "authorization": auth,
        "proxy_authorization": _scheme_only(headers.get("Proxy-Authorization")),
        "identity_headers": {
            name: _identity_value(value)
            for name, value in sorted(headers.items())
            if _is_identity_header(name)
        },
        "challenge": tenant,
        "challenged": wants_challenge(auth, tenant),
    }


def wants_challenge(auth: dict[str, Any] | None, tenant: str | None) -> bool:
    """Answer with the sign-in challenge: a tenant was asked for and no token came
    (no Authorization at all, or Power Query's empty ``Bearer``)."""
    return tenant is not None and (auth is None or auth.get("empty") is True)


def challenge_header(tenant: str) -> str:
    """The ``WWW-Authenticate`` value Power Query's Organizational account needs."""
    return f"Bearer authorization_uri={AUTHORITY}/{tenant}/oauth2/authorize"


def authorization(value: str | None) -> dict[str, Any] | None:
    """The shape of an Authorization header: the scheme, and for Bearer whether a
    token came, its length and (if it is a JWT) its unverified summary."""
    if value is None:
        return None
    scheme, _, credentials = value.strip().partition(" ")
    if not scheme:
        return {"scheme": "", "empty": True}
    if not _SCHEME.fullmatch(scheme):  # no scheme: the value may be a bare token
        return {"scheme": None, "length": len(value)}
    out: dict[str, Any] = {"scheme": scheme}
    if scheme.lower() == "bearer":
        token = credentials.strip()
        out.update(empty=not token, length=len(token))
        if token:
            out["jwt"] = jwt_summary(token)
    return out


def jwt_summary(token: str) -> dict[str, Any] | None:
    """A JWT's header fields and listed claims, decoded WITHOUT verifying it (this
    is a diagnostic, nothing trusts it). None when ``token`` is not a JWT."""
    if not _JWT.fullmatch(token):
        return None
    head_part, body_part, _signature = token.split(".")
    header, claims = _b64_json(head_part), _b64_json(body_part)
    if header is None or claims is None:
        return None
    summary: dict[str, Any] = {
        "header": {k: _clip(header[k]) for k in JWT_HEADER if k in header},
        "claims": {k: _clip(claims[k]) for k in JWT_CLAIMS if k in claims},
    }
    exp = claims.get("exp")
    if isinstance(exp, int | float) and not isinstance(exp, bool):
        try:
            summary["exp_utc"] = datetime.fromtimestamp(exp, UTC).isoformat()
        except OverflowError, OSError, ValueError:
            pass
    return summary


def _b64_json(part: str) -> dict[str, Any] | None:
    try:
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except ValueError:  # bad base64 (binascii.Error), bad UTF-8 or bad JSON
        return None
    return value if isinstance(value, dict) else None


def _scheme_only(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    scheme = value.strip().partition(" ")[0]
    return {"scheme": scheme if _SCHEME.fullmatch(scheme) else None}


def _is_identity_header(name: str) -> bool:
    lower = name.lower()
    if lower in _NOT_IDENTITY:
        return False
    return lower.startswith("x-ms-") or any(word in lower for word in _IDENTITY_WORDS)


def _identity_value(value: str) -> Any:
    text = value.strip()
    jwt = jwt_summary(text)
    if jwt is not None:
        return {"jwt": jwt}
    if len(text) <= 254 and (_EMAIL.fullmatch(text) or _GUID.fullmatch(text)):
        return text
    return {"length": len(text)}


def _clip(value: Any) -> Any:
    """A header or claim value, safe to echo: strings capped, lists shortened."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        return [_clip(v) for v in value[:20]]
    return str(value)[:MAX_TEXT]
