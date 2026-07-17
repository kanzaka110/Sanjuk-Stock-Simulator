# Customs Export 2B Implementation Plan

> **For Hermes:** Execute each slice with strict RED→GREEN and freeze production score/order behavior.

**Goal:** Add official KCS-reported operating-day lineage and a separately labelled official HS chapter-30 pharmaceutical shadow factor without changing any production score, gate, candidate, or order path.

**Architecture:** Parse KCS press releases mirrored on `korea.kr` for cumulative operating-day counts and persist normalized append-only observations in the existing V2 observation store. Add workday-safe D10/D20/EOM features only when exact KCS lineage is complete. Implement a separate monthly HS-30 provider/contract; keep all company exposure weights zero until versioned IR evidence exists. Existing DB schema remains unchanged because lineage is canonical JSON payload.

**Tech Stack:** Python stdlib HTTP/XML/HTML parsing, Decimal/fixed-point counts, SQLite V2 append-only store, pytest.

---

## Safety invariants

- `calendar_domain` must be exactly `KCS_REPORTED_OPERATING_DAYS`.
- Never substitute KRX trading days, weekday counts, or KASI holiday estimates.
- Date-only publication metadata never becomes synthetic midnight; `available_at_utc = ingested_at_utc`.
- D20/EOM amount and workday segments both use cumulative subtraction.
- Missing/invalid workday lineage yields `workday_feature_ready=false`, null workday values, and a typed flag.
- `precision_instruments` can never alias bio/pharma.
- HS chapter 30 is labelled `pharmaceutical_products_hs30`, not broad biotechnology.
- Taxonomy/company evidence missing or future-dated means exposure weight zero and symbol aggregate null/ineligible.
- No imports or reads from production scorer, candidate, dashboard GET, broker, or order paths.

## Task 1: Workday-safe feature derivation

**Files:**
- Modify: `core/customs_export_features.py`
- Modify: `tests/test_customs_export_features.py`

1. RED: missing workday makes top-level feature not ready while preserving raw/calendar diagnostics.
2. GREEN: split `raw_feature_ready`, `calendar_feature_ready`, `workday_feature_ready`.
3. RED/GREEN: D10 workday YoY from W10 current/prior.
4. RED/GREEN: D20 uses `(W20-W10)` and EOM uses `(WE-W20)`.
5. RED/GREEN: reject wrong calendar domain, future availability, duplicate keys; null+flag non-monotonic/zero workdays.
6. RED/GREEN: hash exact component snapshot IDs into `workday_vector_id`.

## Task 2: Official KCS press-release workday source

**Files:**
- Create: `core/customs_export_workdays.py`
- Create: `tests/test_customs_export_workdays.py`

1. Parse schema-faithful `korea.kr` detail HTML for title, agency, page date, and current/prior workday decimals.
2. Preserve fixed-point tenths, source document ID/URI/SHA, `publication_precision=date_only`, null exact publication timestamp, and first-seen availability.
3. Search the official press-release list using bounded `srchWord/startDate/endDate/pageIndex` parameters and select exact D10/D20/EOM titles only.
4. Fail closed on ambiguous/multiple pages, wrong agency/month/cutoff, missing workday line, malformed count, oversized/non-HTML response, and network failure.
5. Never persist full page URLs containing credentials (this source has none) or exception text.

## Task 3: Append-only workday persistence and collector wiring

**Files:**
- Modify: `core/customs_export_observation_collector.py`
- Modify: `tools/collect_customs_export_observations.py`
- Modify: `tests/test_customs_export_observation_collector.py`
- Modify: `tests/test_customs_export_cli.py`

1. Inject a workday fetcher; fetch outside the DB transaction.
2. Append workday observations before feature derivation inside the existing atomic transaction.
3. Store source `korea_customs`, dataset `ten_day_export_workdays`, unit `UNITLESS`, source URI/hash/method version, and shadow-only flags.
4. Record a separate immutable run ledger; optional workday source failure must not corrupt amount observations but must leave workday features unready and health visibly degraded.
5. Exact retries converge; changed official page bytes append a new revision and new feature vintage.

## Task 4: Official HS-30 pharmaceutical shadow contract

**Files:**
- Create: `core/customs_hs_exports.py`
- Create: `core/customs_bio_taxonomy.py`
- Create: `tests/test_customs_hs_exports.py`
- Create: `tests/test_customs_bio_taxonomy.py`

1. Parse/fetch official KCS item-trade API `GET https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList` with `hsSgn=30` and exact XML contract.
2. Preserve monthly period, exact HS code, official item name, USD export amount, source clocks, classification version/evidence, raw hash, and typed failures.
3. Label factor `pharmaceutical_products_hs30`; prohibit claims that it equals all biotechnology.
4. Validate append-only taxonomy records with exact nonempty code basket, version, evidence hashes/URIs and available times.
5. Missing/future company evidence or `precision_instruments` alias always yields weight zero; all-zero aggregate is null/ineligible.
6. Official classification evidence is UN Comtrade reference `H6 = HS2022`, chapter `30 = Pharmaceutical products`; `H7` is not a valid reference endpoint.
7. The 2026-07-17 authenticated read-only entitlement differential used the same configured 64-byte decoded service key: the existing ten-day API returned HTTP 200 `application/xml`, while Itemtrade HS30 returned HTTP 403 `text/plain`. This is dataset-specific entitlement failure, not missing credentials or double encoding. Keep the monthly provider unscheduled and production-ineligible until an actual HTTP 200 XML response passes this parser; synthetic fixtures are not deployment evidence.

## Task 5: Verification and release

1. Run focused tests after every final edit.
2. Snapshot production DB main/WAL/SHM metadata before/after all tests.
3. Run full baseline and candidate suites against exact fingerprints.
4. Run mutation probes for wrong calendar domain, D20/EOM denominator, synthetic midnight, mixed/future workday vectors, precision→bio, missing taxonomy evidence, and production import contamination.
5. Run an authenticated no-persistence smoke only after external API entitlement; never print keys/raw URLs.
6. Freeze manifest, complete independent contract/security/regression reviews, then build and round-trip a deployment handoff.

## Verified implementation status

- KCS workday fetcher validates the complete bounded result set across at most 20 pages, then cross-checks list metadata against the official detail header's title, agency, and release date. Forged or ambiguous lineage fails closed before storage.
- Workday observations remain append-only, date-only, shadow-only, and production-score-ineligible. D10/D20/EOM workday-adjusted interval features require matching current/prior official releases.
- HS30 uses the official UN Comtrade H6 Chapter 30 taxonomy, explicitly labels it `Pharmaceutical products`, records the HS2022 effective interval as `202201..None`, and never treats precision instruments as biotechnology.
- The Itemtrade provider rejects pre-HS2022 periods, future periods, incomplete requested month ranges, hostile taxonomy objects, deep JSON recursion, and secret-bearing transport/cleanup failures. It remains unimported by scheduler/scorer/order paths.
- Focused Customs evidence: `314 passed`; scheduler subset: `69 passed`; compile-surviving mutations: `12/12 KILLED`.
- Whole-repository candidate evidence: `3644 passed`, with the same 14 unrelated failures as the clean baseline (`candidate-only=0`, `baseline-only=0`).
- Independent reviews: KCS lineage/pagination PASS; taxonomy/HS blocker re-review PASS; production non-interference PASS.
- Release constraint: Itemtrade returned authenticated HTTP 403 as documented above. Do not schedule, persist, score, backtest, or deploy HS30 until a real HTTP 200 XML response passes the parser and a new reviewed artifact is issued.
