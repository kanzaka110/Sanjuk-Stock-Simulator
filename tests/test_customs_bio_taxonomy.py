"""공식 HS2022 의약품 taxonomy와 회사 exposure fail-closed 계약."""

from datetime import datetime, timezone
import hashlib
import json

import pytest


UTC = timezone.utc
REFERENCE_URI = "https://comtradeapi.un.org/files/v1/app/reference/H6.json"
FIRST_SEEN = datetime(2026, 7, 17, 13, 0, tzinfo=UTC)


def _reference_bytes(*, rows=None, class_code="H6", class_name="HS2022"):
    if rows is None:
        rows = [
            {
                "id": "30",
                "text": "30 - Pharmaceutical products",
                "parent": "TOTAL",
                "isLeaf": "0",
                "aggrlevel": 2,
                "standardUnitAbbr": "n/a",
            }
        ]
    return json.dumps(
        {
            "more": False,
            "minimumInputLength": 2,
            "classCode": class_code,
            "className": class_name,
            "results": rows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def test_parse_official_hs2022_chapter30_contract():
    from core.customs_bio_taxonomy import parse_hs2022_chapter30_reference

    raw = _reference_bytes()
    taxonomy = parse_hs2022_chapter30_reference(
        raw,
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )

    assert taxonomy["factor_name"] == "pharmaceutical_products_hs30"
    assert taxonomy["classification_system"] == "HS"
    assert taxonomy["classification_reference_code"] == "H6"
    assert taxonomy["classification_version"] == "HS2022"
    assert taxonomy["taxonomy_effective_from_period"] == "202201"
    assert taxonomy["taxonomy_effective_to_period"] is None
    assert taxonomy["classification_codes"] == ["30"]
    assert taxonomy["official_label"] == "Pharmaceutical products"
    assert taxonomy["taxonomy_evidence_uri"] == REFERENCE_URI
    assert taxonomy["taxonomy_evidence_sha256"] == hashlib.sha256(raw).hexdigest()
    assert taxonomy["taxonomy_evidence_available_at_utc"] == (
        "2026-07-17T13:00:00.000000Z"
    )
    assert taxonomy["source_published_at_utc"] is None
    assert taxonomy["shadow_only"] is True
    assert taxonomy["eligible_for_production_score"] is False
    assert taxonomy["is_broad_biotechnology"] is False


@pytest.mark.parametrize(
    "precision_alias",
    (
        {"source_field": "precision_instruments"},
        {"source_item_code": "KCS:PRECISION"},
    ),
    ids=("source_field", "source_item_code"),
)
def test_precision_instruments_can_never_be_a_bio_proxy(precision_alias):
    from core.customs_bio_taxonomy import evaluate_company_exposure

    result = evaluate_company_exposure(
        {
            **precision_alias,
            "symbol": "207940.KS",
            "market": "KR",
            "exposure_weight": 1.0,
        },
        taxonomy=None,
        decision_at_utc=FIRST_SEEN,
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] == "precision_instruments_not_bio"
    assert result["factor_name"] == "pharmaceutical_products_hs30"
    assert result["shadow_only"] is True
    assert result["eligible_for_production_score"] is False


def _verified_proposal(taxonomy):
    return {
        "taxonomy_id": taxonomy["taxonomy_id"],
        "source_item_code": "30",
        "source_field": "pharmaceutical_products_hs30",
        "classification_system": "HS",
        "classification_codes": ["30"],
        "classification_version": "HS2022",
        "taxonomy_effective_from_period": taxonomy[
            "taxonomy_effective_from_period"
        ],
        "taxonomy_effective_to_period": taxonomy["taxonomy_effective_to_period"],
        "taxonomy_evidence_uri": taxonomy["taxonomy_evidence_uri"],
        "taxonomy_evidence_sha256": taxonomy["taxonomy_evidence_sha256"],
        "taxonomy_evidence_available_at_utc": taxonomy[
            "taxonomy_evidence_available_at_utc"
        ],
        "symbol": "207940.KS",
        "market": "KR",
        "company_exposure_evidence_uri": (
            "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260717000001"
        ),
        "company_exposure_evidence_sha256": "b" * 64,
        "company_exposure_available_at_utc": "2026-07-17T13:00:00.000000Z",
        "weight_basis": "reviewed_revenue_exposure",
        "mapping_version": "hs30-company-exposure-v1",
        "valid_from_utc": "2026-07-17T13:00:00.000000Z",
        "exposure_weight": 0.35,
    }


def test_complete_point_in_time_company_mapping_allows_shadow_weight_only():
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    result = evaluate_company_exposure(
        _verified_proposal(taxonomy),
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0.35
    assert result["mapping_status"] == "verified_shadow"
    assert result["symbol"] == "207940.KS"
    assert result["classification_codes"] == ["30"]
    assert result["shadow_only"] is True
    assert result["eligible_for_production_score"] is False


def test_expired_company_mapping_returns_exact_zero():
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    proposal = _verified_proposal(taxonomy)
    proposal["valid_to_utc"] = "2026-07-17T13:30:00.000000Z"
    result = evaluate_company_exposure(
        proposal,
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] == "mapping_expired"
    assert result["eligible_for_production_score"] is False


@pytest.mark.parametrize(
    "invalid_weight",
    (True, "0.35", 0, -0.1, 1.1, float("nan"), 1 << 100_000),
    ids=("bool", "string", "zero", "negative", "above_one", "nan", "huge_int"),
)
def test_invalid_weights_fail_closed_without_exception(invalid_weight):
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    proposal = _verified_proposal(taxonomy)
    proposal["exposure_weight"] = invalid_weight
    result = evaluate_company_exposure(
        proposal,
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] == "company_evidence_invalid"
    assert result["eligible_for_production_score"] is False


_REQUIRED_MAPPING_FIELDS = (
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
)


@pytest.mark.parametrize("missing_field", _REQUIRED_MAPPING_FIELDS)
def test_each_missing_mapping_field_forces_exact_zero(missing_field):
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    proposal = _verified_proposal(taxonomy)
    del proposal[missing_field]
    result = evaluate_company_exposure(
        proposal,
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] != "verified_shadow"
    assert result["eligible_for_production_score"] is False


def test_all_zero_exposures_aggregate_to_null_not_neutral_score():
    from core.customs_bio_taxonomy import aggregate_company_exposures

    aggregate = aggregate_company_exposures(
        [
            {
                "factor_name": "pharmaceutical_products_hs30",
                "symbol": "207940.KS",
                "market": "KR",
                "exposure_weight": 0,
                "mapping_status": "company_evidence_invalid",
                "shadow_only": True,
                "eligible_for_production_score": False,
            },
            {
                "factor_name": "pharmaceutical_products_hs30",
                "symbol": "068270.KS",
                "market": "KR",
                "exposure_weight": 0,
                "mapping_status": "mapping_contract_invalid",
                "shadow_only": True,
                "eligible_for_production_score": False,
            },
        ]
    )

    assert aggregate["aggregate_exposure_weight"] is None
    assert aggregate["score_contribution"] is None
    assert aggregate["mapping_status"] == "all_exposures_zero"
    assert aggregate["eligible_for_production_score"] is False


def test_empty_exposure_set_is_not_evidence_of_zero_exposure():
    from core.customs_bio_taxonomy import aggregate_company_exposures

    aggregate = aggregate_company_exposures([])

    assert aggregate["aggregate_exposure_weight"] is None
    assert aggregate["score_contribution"] is None
    assert aggregate["mapping_status"] == "empty_exposure_set"
    assert aggregate["eligible_for_production_score"] is False


def test_future_company_evidence_forces_exact_zero():
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    proposal = _verified_proposal(taxonomy)
    proposal["company_exposure_available_at_utc"] = "2026-07-17T15:00:00.000000Z"
    result = evaluate_company_exposure(
        proposal,
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] == "evidence_not_available"
    assert result["eligible_for_production_score"] is False


@pytest.mark.parametrize(
    "invalid_uri",
    (
        "https://dart.fss.or.kr:99999/dsaf001/main.do?rcpNo=20260717000001",
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo",
    ),
    ids=("invalid_port", "malformed_query"),
)
def test_malformed_company_evidence_uri_fails_closed_without_exception(invalid_uri):
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    proposal = _verified_proposal(taxonomy)
    proposal["company_exposure_evidence_uri"] = invalid_uri
    result = evaluate_company_exposure(
        proposal,
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] == "company_evidence_invalid"
    assert result["eligible_for_production_score"] is False


def test_taxonomy_rejects_h7_duplicate_chapter_and_label_mutation():
    from core.customs_bio_taxonomy import parse_hs2022_chapter30_reference

    with pytest.raises(ValueError, match="customs_taxonomy_reference_contract_invalid"):
        parse_hs2022_chapter30_reference(
            _reference_bytes(class_code="H7"),
            source_uri=REFERENCE_URI,
            first_seen_at_utc=FIRST_SEEN,
        )

    chapter = {
        "id": "30",
        "text": "30 - Pharmaceutical products",
        "parent": "TOTAL",
        "isLeaf": "0",
        "aggrlevel": 2,
        "standardUnitAbbr": "n/a",
    }
    with pytest.raises(ValueError, match="customs_taxonomy_chapter30_ambiguous"):
        parse_hs2022_chapter30_reference(
            _reference_bytes(rows=[chapter, dict(chapter)]),
            source_uri=REFERENCE_URI,
            first_seen_at_utc=FIRST_SEEN,
        )

    changed = dict(chapter)
    changed["text"] = "30 - Biotechnology"
    with pytest.raises(ValueError, match="customs_taxonomy_chapter30_contract_invalid"):
        parse_hs2022_chapter30_reference(
            _reference_bytes(rows=[changed]),
            source_uri=REFERENCE_URI,
            first_seen_at_utc=FIRST_SEEN,
        )


class _TaxonomyShaLike:
    def __str__(self):
        return "a" * 64


class _TaxonomyExplodingSha:
    def __str__(self):
        raise RuntimeError("must-not-escape")


class _TaxonomyExplodingEq:
    def __eq__(self, _other):
        raise RuntimeError("must-not-escape")

    def __ne__(self, _other):
        raise RuntimeError("must-not-escape")


@pytest.mark.parametrize(
    "mutation",
    ("missing_taxonomy_id", "sha_like", "sha_explodes"),
)
def test_company_exposure_rejects_bypassable_taxonomy_provenance(mutation):
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    proposal = _verified_proposal(taxonomy)
    if mutation == "missing_taxonomy_id":
        del taxonomy["taxonomy_id"]
    elif mutation == "sha_like":
        taxonomy["taxonomy_evidence_sha256"] = _TaxonomyShaLike()
    else:
        taxonomy["taxonomy_evidence_sha256"] = _TaxonomyExplodingSha()

    result = evaluate_company_exposure(
        proposal,
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] == "taxonomy_evidence_invalid"
    assert result["eligible_for_production_score"] is False


def test_company_exposure_taxonomy_cannot_trigger_custom_equality():
    from core.customs_bio_taxonomy import (
        evaluate_company_exposure,
        parse_hs2022_chapter30_reference,
    )

    taxonomy = parse_hs2022_chapter30_reference(
        _reference_bytes(),
        source_uri=REFERENCE_URI,
        first_seen_at_utc=FIRST_SEEN,
    )
    proposal = _verified_proposal(taxonomy)
    taxonomy["factor_name"] = _TaxonomyExplodingEq()

    result = evaluate_company_exposure(
        proposal,
        taxonomy=taxonomy,
        decision_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert result["exposure_weight"] == 0
    assert result["mapping_status"] == "taxonomy_evidence_invalid"


def test_deep_json_recursion_is_normalized_to_taxonomy_value_error():
    from core.customs_bio_taxonomy import parse_hs2022_chapter30_reference

    deeply_nested = b"[" * 2_000 + b"0" + b"]" * 2_000
    with pytest.raises(ValueError, match="customs_taxonomy_reference_malformed"):
        parse_hs2022_chapter30_reference(
            deeply_nested,
            source_uri=REFERENCE_URI,
            first_seen_at_utc=FIRST_SEEN,
        )
