"""Bounded secret detection for free-text and encoded persistence boundaries."""

from __future__ import annotations

import base64
import binascii
import html
import re
from urllib import parse

MAX_SENSITIVE_SCAN_BYTES = 1_100_000
_MAX_VARIANTS = 96
_MAX_BASE64_TOKENS = 32
_BASE64_TOKEN = re.compile(
    r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{12,}={0,2}(?![A-Za-z0-9+/_=-])"
)
_SENSITIVE_ALIASES = frozenset(
    {
        "accountno",
        "accountnumber",
        "apikey",
        "appkey",
        "appsecret",
        "authorization",
        "clientsecret",
        "crtfckey",
        "password",
        "privatekey",
        "secret",
        "servicekey",
        "token",
    }
)
_SENSITIVE_FIELD = re.compile(
    r"(?:account[\s_.-]*(?:no|number)|api[\s_.-]*key|"
    r"app[\s_.-]*(?:key|secret)|authorization|"
    r"client[\s_.-]*secret|crtfc[\s_.-]*key|"
    r"data[\s_.-]*go(?:[\s_.-]*(?:kr|api|service))*[\s_.-]*key|"
    r"password|private[\s_.-]*key|service[\s_.-]*key|secret|token)"
    r"(?=[\s\"'<>\[\]{}=:;&?])",
    re.IGNORECASE,
)
_KNOWN_SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----)",
    re.IGNORECASE,
)


def _collapsed(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def sensitive_key_name(value: str) -> bool:
    collapsed = _collapsed(value)
    return any(
        collapsed == alias or collapsed.endswith(alias)
        for alias in _SENSITIVE_ALIASES
    )


def _decode_bytes(value: bytes) -> set[str]:
    decoded: set[str] = set()
    for encoding in ("utf-8", "latin-1"):
        try:
            decoded.add(value.decode(encoding))
        except UnicodeDecodeError:
            pass
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            decoded.add(value.decode("utf-16"))
        except UnicodeDecodeError:
            pass
    elif b"\x00" in value[:128]:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                decoded.add(value.decode(encoding))
            except UnicodeDecodeError:
                pass
    return decoded


def _base64_texts(token: str) -> set[str]:
    if (
        token.isdigit()
        or re.search(r"[A-Z]", token) is None
        or re.search(r"[a-z]", token) is None
    ):
        return set()
    if len(token) > ((MAX_SENSITIVE_SCAN_BYTES + 2) // 3) * 4:
        return set()
    padded = token + "=" * (-len(token) % 4)
    decoded: set[str] = set()
    for altchars in (None, b"-_"):
        try:
            raw = base64.b64decode(
                padded.encode("ascii"),
                altchars=altchars,
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError):
            continue
        if len(raw) > MAX_SENSITIVE_SCAN_BYTES:
            continue
        text_candidates: set[str] = set()
        try:
            text_candidates.add(raw.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                text_candidates.add(raw.decode("utf-16"))
            except UnicodeDecodeError:
                pass
        decoded.update(
            text
            for text in text_candidates
            if text and all(character.isprintable() or character in "\t\r\n" for character in text)
        )
        latin_text = raw.decode("latin-1")
        if (
            _SENSITIVE_FIELD.search(latin_text) is not None
            or _KNOWN_SECRET.search(latin_text) is not None
        ):
            decoded.add(latin_text)
    return decoded


def decoded_text_variants(value: str | bytes) -> set[str]:
    if type(value) is str:
        if len(value.encode("utf-8")) > MAX_SENSITIVE_SCAN_BYTES:
            raise ValueError("sensitive_scan_too_large")
        initial = {value}
    elif type(value) is bytes:
        if len(value) > MAX_SENSITIVE_SCAN_BYTES:
            raise ValueError("sensitive_scan_too_large")
        initial = _decode_bytes(value)
    else:
        raise TypeError("sensitive_scan_type_invalid")

    variants = set(initial)
    frontier = set(initial)
    decoded_tokens = 0
    for _ in range(3):
        next_frontier: set[str] = set()
        for text in frontier:
            transformed_values = (
                html.unescape(text),
                parse.unquote(text),
                parse.unquote_plus(text),
            )
            for transformed in transformed_values:
                if transformed not in variants:
                    variants.add(transformed)
                    next_frontier.add(transformed)
                    if len(variants) >= _MAX_VARIANTS:
                        raise ValueError("sensitive_scan_complexity")
            for match in _BASE64_TOKEN.finditer(text):
                decoded_values = _base64_texts(match.group(0))
                if not decoded_values:
                    continue
                if decoded_tokens >= _MAX_BASE64_TOKENS:
                    raise ValueError("sensitive_scan_complexity")
                decoded_tokens += 1
                for decoded in decoded_values:
                    if decoded not in variants:
                        variants.add(decoded)
                        next_frontier.add(decoded)
                        if len(variants) >= _MAX_VARIANTS:
                            raise ValueError("sensitive_scan_complexity")
        if not next_frontier:
            break
        frontier = next_frontier
    return variants


def _secret_forms(secret: str) -> set[str]:
    encoded = secret.encode("utf-8")
    standard = base64.b64encode(encoded).decode("ascii")
    urlsafe = base64.urlsafe_b64encode(encoded).decode("ascii")
    return {
        secret,
        html.escape(secret, quote=True),
        parse.quote(secret, safe=""),
        parse.quote_plus(secret, safe=""),
        standard,
        standard.rstrip("="),
        urlsafe,
        urlsafe.rstrip("="),
    }


def sensitive_text_kind(
    value: str | bytes,
    *,
    known_secrets: tuple[str, ...] = (),
) -> str | None:
    secret_forms = {
        form
        for secret in known_secrets
        if type(secret) is str and secret
        for form in _secret_forms(secret)
    }
    if type(value) is str:
        direct_values = {value}
    elif type(value) is bytes:
        if len(value) > MAX_SENSITIVE_SCAN_BYTES:
            raise ValueError("sensitive_scan_too_large")
        direct_values = _decode_bytes(value)
    else:
        raise TypeError("sensitive_scan_type_invalid")
    if secret_forms and any(
        form in direct_value
        for direct_value in direct_values
        for form in secret_forms
    ):
        return "known"
    try:
        variants = decoded_text_variants(value)
    except ValueError as exc:
        if str(exc) == "sensitive_scan_complexity":
            return "generic"
        raise
    if secret_forms and any(
        form in variant for variant in variants for form in secret_forms
    ):
        return "known"
    if any(
        _KNOWN_SECRET.search(variant) is not None
        or _SENSITIVE_FIELD.search(variant) is not None
        for variant in variants
    ):
        return "generic"
    return None


def contains_sensitive_text(
    value: str | bytes,
    *,
    known_secrets: tuple[str, ...] = (),
) -> bool:
    return sensitive_text_kind(value, known_secrets=known_secrets) is not None
