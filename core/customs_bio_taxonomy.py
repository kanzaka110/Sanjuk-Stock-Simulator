"""UN Comtrade 공식 HS2022 Chapter 30 shadow taxonomy 계약."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit


_REFERENCE_URI = "https://comtradeapi.un.org/files/v1/app/reference/H6.json"
_MAX_REFERENCE_BYTES = 4_000_000
_TOP_LEVEL_KEYS = frozenset(
    {"more", "minimumInputLength", "classCode", "className", "results"}
)
_CHAPTER_KEYS = frozenset(
    {"id", "text", "parent", "isLeaf", "aggrlevel", "standardUnitAbbr"}
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[0-9]{6}\.(?:KS|KQ)")
_VERSION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_REQUIRED_PROPOSAL_FIELDS = frozenset(
    {
        "taxonomy_id",
        "source_item_code",
        "source_field",
        "classification_system",
        "classification_codes",
        "classification_version",
        "taxonomy_effective_from_period",
        "taxonomy_effective_to_period",
        "taxonomy_evidence_uri",
        "taxonomy_evidence_sha256",
        "taxonomy_evidence_available_at_utc",
        "symbol",
        "market",
        "company_exposure_evidence_uri",
        "company_exposure_evidence_sha256",
        "company_exposure_available_at_utc",
        "weight_basis",
        "mapping_version",
        "valid_from_utc",
        "exposure_weight",
    }
)


def _exact_text(value: Any, expected: str) -> bool:
    return type(value) is str and value == expected


def _exact_chapter30(value: Any) -> bool:
    return (
        type(value) is list
        and len(value) == 1
        and type(value[0]) is str
        and value[0] == "30"
    )


def _utc_text(value: Any) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("customs_taxonomy_first_seen_invalid")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("customs_taxonomy_json_duplicate_key")
        result[key] = value
    return result


def _validate_reference_uri(source_uri: Any) -> str:
    if type(source_uri) is not str or source_uri != _REFERENCE_URI:
        raise ValueError("customs_taxonomy_source_uri_invalid")
    parsed = urlsplit(source_uri)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "comtradeapi.un.org"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("customs_taxonomy_source_uri_invalid")
    return source_uri


def parse_hs2022_chapter30_reference(
    raw: bytes,
    *,
    source_uri: str,
    first_seen_at_utc: datetime,
) -> dict[str, Any]:
    """공식 H6 reference에서 HS Chapter 30 정의 하나만 추출한다."""

    if type(raw) is not bytes or not raw or len(raw) > _MAX_REFERENCE_BYTES:
        raise ValueError("customs_taxonomy_reference_size_invalid")
    canonical_uri = _validate_reference_uri(source_uri)
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("customs_taxonomy_reference_malformed") from exc
    if type(document) is not dict or frozenset(document) != _TOP_LEVEL_KEYS:
        raise ValueError("customs_taxonomy_reference_contract_invalid")
    if (
        document["more"] is not False
        or type(document["minimumInputLength"]) is not int
        or document["minimumInputLength"] != 2
        or document["classCode"] != "H6"
        or document["className"] != "HS2022"
        or type(document["results"]) is not list
        or not document["results"]
        or len(document["results"]) > 10_000
    ):
        raise ValueError("customs_taxonomy_reference_contract_invalid")
    matches = [
        row
        for row in document["results"]
        if type(row) is dict and row.get("id") == "30"
    ]
    if len(matches) != 1:
        raise ValueError("customs_taxonomy_chapter30_ambiguous")
    chapter = matches[0]
    if (
        frozenset(chapter) != _CHAPTER_KEYS
        or chapter["text"] != "30 - Pharmaceutical products"
        or chapter["parent"] != "TOTAL"
        or chapter["isLeaf"] != "0"
        or type(chapter["aggrlevel"]) is not int
        or chapter["aggrlevel"] != 2
        or chapter["standardUnitAbbr"] != "n/a"
    ):
        raise ValueError("customs_taxonomy_chapter30_contract_invalid")
    evidence_sha = hashlib.sha256(raw).hexdigest()
    if _SHA256_RE.fullmatch(evidence_sha) is None:
        raise AssertionError("customs_taxonomy_sha256_internal")
    return {
        "taxonomy_id": "un-comtrade-hs2022-chapter30-v1",
        "factor_name": "pharmaceutical_products_hs30",
        "classification_system": "HS",
        "classification_reference_code": "H6",
        "classification_version": "HS2022",
        "taxonomy_effective_from_period": "202201",
        "taxonomy_effective_to_period": None,
        "classification_codes": ["30"],
        "official_label": "Pharmaceutical products",
        "classification_level": 2,
        "source_authority": "United Nations Statistics Division / UN Comtrade",
        "taxonomy_evidence_uri": canonical_uri,
        "taxonomy_evidence_sha256": evidence_sha,
        "taxonomy_evidence_available_at_utc": _utc_text(first_seen_at_utc),
        "source_published_at_utc": None,
        "publication_precision": "unknown",
        "is_broad_biotechnology": False,
        "shadow_only": True,
        "eligible_for_production_score": False,
    }


def _zero_exposure(proposal: Any, status: str) -> dict[str, Any]:
    symbol = proposal.get("symbol") if type(proposal) is dict else None
    market = proposal.get("market") if type(proposal) is dict else None
    return {
        "factor_name": "pharmaceutical_products_hs30",
        "symbol": (
            symbol
            if type(symbol) is str and _SYMBOL_RE.fullmatch(symbol) is not None
            else None
        ),
        "market": market if type(market) is str and market == "KR" else None,
        "exposure_weight": 0,
        "mapping_status": status,
        "shadow_only": True,
        "eligible_for_production_score": False,
    }


def _parse_utc_text(value: Any) -> datetime | None:
    if type(value) is not str or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed


def _valid_company_evidence_uri(value: Any) -> bool:
    if type(value) is not str or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname != "dart.fss.or.kr"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path != "/dsaf001/main.do"
        or parsed.fragment
    ):
        return False
    return (
        frozenset(query) == frozenset({"rcpNo"})
        and len(query["rcpNo"]) == 1
        and re.fullmatch(r"[0-9]{14}", query["rcpNo"][0]) is not None
    )


def evaluate_company_exposure(
    proposal: Any,
    *,
    taxonomy: dict[str, Any] | None,
    decision_at_utc: datetime,
) -> dict[str, Any]:
    """회사 매핑은 완전한 PIT 증거가 없으면 정확히 0으로 유지한다."""

    if type(proposal) is dict and (
        _exact_text(proposal.get("source_field"), "precision_instruments")
        or _exact_text(proposal.get("source_item_code"), "KCS:PRECISION")
    ):
        return _zero_exposure(proposal, "precision_instruments_not_bio")
    if type(proposal) is not dict or not _REQUIRED_PROPOSAL_FIELDS.issubset(proposal):
        return _zero_exposure(proposal, "mapping_contract_invalid")
    if (
        type(decision_at_utc) is not datetime
        or decision_at_utc.tzinfo is None
        or decision_at_utc.utcoffset() is None
    ):
        return _zero_exposure(proposal, "decision_at_invalid")
    decision = decision_at_utc.astimezone(timezone.utc)
    if type(taxonomy) is not dict or (
        not _exact_text(
            taxonomy.get("taxonomy_id"), "un-comtrade-hs2022-chapter30-v1"
        )
        or not _exact_text(
            taxonomy.get("factor_name"), "pharmaceutical_products_hs30"
        )
        or not _exact_text(taxonomy.get("classification_system"), "HS")
        or not _exact_text(taxonomy.get("classification_reference_code"), "H6")
        or not _exact_text(taxonomy.get("classification_version"), "HS2022")
        or not _exact_text(
            taxonomy.get("taxonomy_effective_from_period"), "202201"
        )
        or taxonomy.get("taxonomy_effective_to_period") is not None
        or not _exact_chapter30(taxonomy.get("classification_codes"))
        or not _exact_text(
            taxonomy.get("official_label"), "Pharmaceutical products"
        )
        or type(taxonomy.get("classification_level")) is not int
        or taxonomy["classification_level"] != 2
        or not _exact_text(
            taxonomy.get("source_authority"),
            "United Nations Statistics Division / UN Comtrade",
        )
        or not _exact_text(taxonomy.get("taxonomy_evidence_uri"), _REFERENCE_URI)
        or type(taxonomy.get("taxonomy_evidence_sha256")) is not str
        or _SHA256_RE.fullmatch(taxonomy["taxonomy_evidence_sha256"])
        is None
        or taxonomy.get("source_published_at_utc") is not None
        or not _exact_text(taxonomy.get("publication_precision"), "unknown")
        or taxonomy.get("is_broad_biotechnology") is not False
        or taxonomy.get("shadow_only") is not True
        or taxonomy.get("eligible_for_production_score") is not False
    ):
        return _zero_exposure(proposal, "taxonomy_evidence_invalid")
    taxonomy_available = _parse_utc_text(
        taxonomy.get("taxonomy_evidence_available_at_utc")
    )
    proposal_taxonomy_available = _parse_utc_text(
        proposal.get("taxonomy_evidence_available_at_utc")
    )
    company_available = _parse_utc_text(
        proposal.get("company_exposure_available_at_utc")
    )
    valid_from = _parse_utc_text(proposal.get("valid_from_utc"))
    if None in (
        taxonomy_available,
        proposal_taxonomy_available,
        company_available,
        valid_from,
    ):
        return _zero_exposure(proposal, "evidence_time_invalid")
    assert taxonomy_available is not None
    assert proposal_taxonomy_available is not None
    assert company_available is not None
    assert valid_from is not None
    valid_to = (
        _parse_utc_text(proposal.get("valid_to_utc"))
        if "valid_to_utc" in proposal
        else None
    )
    if "valid_to_utc" in proposal and (
        valid_to is None or valid_to <= valid_from
    ):
        return _zero_exposure(proposal, "evidence_time_invalid")
    if valid_to is not None and decision >= valid_to:
        return _zero_exposure(proposal, "mapping_expired")
    if any(
        timestamp > decision
        for timestamp in (
            taxonomy_available,
            proposal_taxonomy_available,
            company_available,
            valid_from,
        )
    ):
        return _zero_exposure(proposal, "evidence_not_available")
    if (
        type(proposal.get("taxonomy_id")) is not str
        or proposal["taxonomy_id"] != taxonomy["taxonomy_id"]
        or type(proposal.get("source_item_code")) is not str
        or proposal["source_item_code"] != "30"
        or type(proposal.get("source_field")) is not str
        or proposal["source_field"] != "pharmaceutical_products_hs30"
        or type(proposal.get("classification_system")) is not str
        or proposal["classification_system"] != "HS"
        or not _exact_chapter30(proposal.get("classification_codes"))
        or type(proposal.get("classification_version")) is not str
        or proposal["classification_version"] != "HS2022"
        or type(proposal.get("taxonomy_effective_from_period")) is not str
        or proposal["taxonomy_effective_from_period"] != "202201"
        or proposal.get("taxonomy_effective_to_period") is not None
        or type(proposal.get("taxonomy_evidence_uri")) is not str
        or proposal["taxonomy_evidence_uri"]
        != taxonomy["taxonomy_evidence_uri"]
        or type(proposal.get("taxonomy_evidence_sha256")) is not str
        or _SHA256_RE.fullmatch(proposal["taxonomy_evidence_sha256"])
        is None
        or proposal["taxonomy_evidence_sha256"]
        != taxonomy["taxonomy_evidence_sha256"]
        or proposal_taxonomy_available != taxonomy_available
    ):
        return _zero_exposure(proposal, "taxonomy_evidence_mismatch")
    symbol = proposal.get("symbol")
    weight = proposal.get("exposure_weight")
    if type(weight) is int:
        if weight != 1:
            return _zero_exposure(proposal, "company_evidence_invalid")
        numeric_weight = 1.0
    elif type(weight) is float:
        numeric_weight = weight
    else:
        return _zero_exposure(proposal, "company_evidence_invalid")
    if (
        type(symbol) is not str
        or _SYMBOL_RE.fullmatch(symbol) is None
        or proposal.get("market") != "KR"
        or not _valid_company_evidence_uri(
            proposal.get("company_exposure_evidence_uri")
        )
        or type(proposal.get("company_exposure_evidence_sha256")) is not str
        or _SHA256_RE.fullmatch(proposal["company_exposure_evidence_sha256"])
        is None
        or type(proposal.get("weight_basis")) is not str
        or _VERSION_RE.fullmatch(proposal["weight_basis"]) is None
        or type(proposal.get("mapping_version")) is not str
        or _VERSION_RE.fullmatch(proposal["mapping_version"]) is None
        or not math.isfinite(numeric_weight)
        or not 0 < numeric_weight <= 1
    ):
        return _zero_exposure(proposal, "company_evidence_invalid")
    return {
        "taxonomy_id": taxonomy["taxonomy_id"],
        "factor_name": "pharmaceutical_products_hs30",
        "source_item_code": "30",
        "source_field": "pharmaceutical_products_hs30",
        "classification_system": "HS",
        "classification_codes": ["30"],
        "classification_version": "HS2022",
        "taxonomy_effective_from_period": "202201",
        "taxonomy_effective_to_period": None,
        "symbol": symbol,
        "market": "KR",
        "exposure_weight": numeric_weight,
        "weight_basis": proposal["weight_basis"],
        "mapping_version": proposal["mapping_version"],
        "mapping_status": "verified_shadow",
        "shadow_only": True,
        "eligible_for_production_score": False,
    }


def aggregate_company_exposures(rows: Any) -> dict[str, Any]:
    """회사 exposure 집계는 production score가 아닌 shadow 진단만 반환한다."""

    base = {
        "factor_name": "pharmaceutical_products_hs30",
        "aggregate_exposure_weight": None,
        "score_contribution": None,
        "shadow_only": True,
        "eligible_for_production_score": False,
    }
    if type(rows) is not list or not rows:
        return {**base, "mapping_status": "empty_exposure_set"}
    weights: list[float] = []
    for row in rows:
        if (
            type(row) is not dict
            or row.get("factor_name") != "pharmaceutical_products_hs30"
            or row.get("shadow_only") is not True
            or row.get("eligible_for_production_score") is not False
        ):
            return {**base, "mapping_status": "exposure_set_invalid"}
        weight = row.get("exposure_weight")
        if type(weight) is int:
            if weight not in (0, 1):
                return {**base, "mapping_status": "exposure_set_invalid"}
            numeric_weight = float(weight)
        elif type(weight) is float:
            numeric_weight = weight
        else:
            return {**base, "mapping_status": "exposure_set_invalid"}
        if not math.isfinite(numeric_weight) or not 0 <= numeric_weight <= 1:
            return {**base, "mapping_status": "exposure_set_invalid"}
        weights.append(numeric_weight)
    if not any(weight > 0 for weight in weights):
        return {**base, "mapping_status": "all_exposures_zero"}
    return {
        **base,
        "aggregate_exposure_weight": min(sum(weights), 1.0),
        "mapping_status": "verified_shadow_exposures",
    }
