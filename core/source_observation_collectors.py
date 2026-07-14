"""Pure adapters that persist normalized collector output as source observations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from core.source_observations import SourceObservationStore

_SEC_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_DART_RECEIPT_RE = re.compile(r"^\d{14}$")
_KR_STOCK_CODE_RE = re.compile(r"^\d{6}$")
_KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class CollectorResult:
    seen: int
    inserted: int
    duplicates: int
    skipped: int
    invalid: int


def record_dart_disclosure_observations(
    items: Iterable[dict[str, Any]],
    *,
    store: SourceObservationStore,
    ingested_at: datetime,
) -> CollectorResult:
    """Persist listed-company OpenDART disclosures without guessing KS/KQ venue."""
    seen = inserted = duplicates = skipped = invalid = 0
    for raw in items:
        seen += 1
        if not isinstance(raw, dict):
            invalid += 1
            continue
        receipt_no = str(raw.get("rcept_no") or "").strip()
        receipt_date = str(raw.get("rcept_dt") or "").strip()
        stock_code = str(raw.get("stock_code") or "").strip()
        if not _DART_RECEIPT_RE.fullmatch(receipt_no):
            invalid += 1
            continue
        if not stock_code:
            skipped += 1
            continue
        if not _KR_STOCK_CODE_RE.fullmatch(stock_code):
            invalid += 1
            continue
        try:
            source_as_of = datetime.strptime(receipt_date, "%Y%m%d").replace(
                tzinfo=_KST
            )
        except ValueError:
            invalid += 1
            continue

        payload = {
            "rcept_no": receipt_no,
            "rcept_dt": receipt_date,
            "stock_code": stock_code,
            "corp_name": str(raw.get("corp_name") or "").strip(),
            "report_nm": str(raw.get("report_nm") or "").strip(),
        }
        try:
            result = store.append(
                source="opendart_disclosures",
                source_record_id=receipt_no,
                symbol=f"KRX:{stock_code}",
                market="KR",
                currency="KRW",
                source_as_of=source_as_of,
                ingested_at=ingested_at,
                schema_version=1,
                fallback_used=False,
                payload=payload,
            )
        except (TypeError, ValueError):
            invalid += 1
            continue
        if result.inserted:
            inserted += 1
        else:
            duplicates += 1

    return CollectorResult(
        seen=seen,
        inserted=inserted,
        duplicates=duplicates,
        skipped=skipped,
        invalid=invalid,
    )


def record_sec_filing_observations(
    hits: Iterable[dict[str, Any]],
    *,
    store: SourceObservationStore,
    ingested_at: datetime,
) -> CollectorResult:
    """Persist normalized SEC submissions hits without changing monitor behavior."""
    seen = inserted = duplicates = skipped = invalid = 0
    rows = list(hits)
    for raw in reversed(rows):
        seen += 1
        if not isinstance(raw, dict):
            invalid += 1
            continue
        ticker = str(raw.get("ticker") or "").strip().upper()
        accession = str(raw.get("accession") or "").strip()
        filing_date = str(raw.get("filing_date") or "").strip()
        if not _US_TICKER_RE.fullmatch(ticker):
            invalid += 1
            continue
        if not _SEC_ACCESSION_RE.fullmatch(accession):
            invalid += 1
            continue
        try:
            source_as_of = datetime.strptime(filing_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            invalid += 1
            continue

        payload = {
            "accession": accession,
            "filing_date": filing_date,
            "form": str(raw.get("form") or "").strip(),
            "severity": str(raw.get("severity") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
            "items": [str(item) for item in (raw.get("items") or [])],
            "url": str(raw.get("url") or "").strip(),
        }
        try:
            result = store.append(
                source="sec_submissions",
                source_record_id=accession,
                symbol=ticker,
                market="US",
                currency="USD",
                source_as_of=source_as_of,
                ingested_at=ingested_at,
                schema_version=1,
                fallback_used=False,
                payload=payload,
            )
        except (TypeError, ValueError):
            invalid += 1
            continue
        if result.inserted:
            inserted += 1
        else:
            duplicates += 1

    return CollectorResult(
        seen=seen,
        inserted=inserted,
        duplicates=duplicates,
        skipped=skipped,
        invalid=invalid,
    )
