"""core/ai_berkshire_toss.py

Toss sell_to_fund 자동매도용 AI Berkshire read-only score layer.

[배경]
weakness_score(손실률 기반)만으로 리밸런싱 매도 후보를 뽑으면 장기 보유
논지가 있는 종목(예: ABBV hold)까지 자동 SELL 후보로 올라온다. 이 모듈은
db/data/ai_berkshire_scores.json 의 종목별 판정(classification)을 읽어
sell_to_fund 후보에 병합하고, 자동매도 허용 여부를 fail-closed로 판정한다.

[규칙]
- 자동매도 허용: sell_to_fund / trim / avoid
- 자동매도 제외: hold / protect / gray_zone / score 없음 (fail-closed)
- score 파일이 없거나 깨졌으면 모든 후보 auto_sell_eligible=False
- 이 게이트는 sell_to_fund 에만 적용. 손절/hard stop 등 리스크 매도는
  이 모듈을 거치지 않고 기존 우선 규칙을 그대로 따른다.
- read-only: 주문/취소/정정/파일 쓰기 부작용 없음.

[thesis freshness — 낡은 판단이 무기한 자동매도를 좌우하지 못하게]
- 저장 classification(stored)과 적용 classification(effective)을 분리한다.
- valid_until 경과(다음 날부터) → effective=gray_zone, thesis_expired=true,
  block_reason=ai_berkshire_thesis_expired
- valid_until 누락/형식 오류, source_urls 누락/빈 목록 → effective=gray_zone
  (근거 없는 판단은 자동매도 권한이 없다 — fail-closed)
- 가격 변화만으로는 classification을 바꾸지 않는다. 갱신은 red_lines 위반 등
  검증 가능한 사실 변화가 있을 때 JSON 파일 수정으로만 한다.

[자동 BUY 게이트]
evaluate_ai_berkshire_buy_gate()는 SELL 게이트와 반대 방향이다. 근거가
살아있는(freshness_valid) effective avoid 또는 strict checklist가 거부한
신규 BUY를 하드 차단한다. strict marker가 있는 row는 classification/checklist/
freshness 손상을 모두 차단한다. marker 없는 legacy row만 avoid_only 호환을
유지한다. score unavailable은 helper에서 진단하고 주문 직전 경로에서 차단한다.

[명시적 자동매도 거부]
score 항목에 auto_sell_eligible=false가 있으면 trim/avoid라도 자동
sell_to_fund를 허용하지 않는다. true는 분류 게이트를 우회할 권한을 주지
않으며, 기존 classification 규칙을 그대로 통과해야 한다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_SCORES_FILE = "ai_berkshire_scores.json"

# 자동 sell_to_fund 허용 판정 (그 외 전부 제외 — fail-closed)
_AUTO_SELL_CLASSIFICATIONS = frozenset({"sell_to_fund", "trim", "avoid"})

# 자동 신규 BUY 하드 차단 판정 (기존 score는 avoid_only 호환)
_BUY_BLOCK_CLASSIFICATIONS = frozenset({"avoid"})

# 리서치 결과가 신규 BUY를 명시적으로 거부/유보한 상태
_BUY_CHECKLIST_BLOCK_STATUSES = frozenset({"fail", "gray_zone"})

# BUY 게이트가 "판단은 있으나 BUY 의미를 자동 결정하지 않는다"로 보는 판정
_BUY_REVIEWED_CLASSIFICATIONS = frozenset({"hold", "protect", "trim", "sell_to_fund"})
_VALID_CLASSIFICATIONS = frozenset({
    "sell_to_fund", "avoid", "trim", "gray_zone", "hold", "protect",
})
_VALID_BUY_CHECKLIST_STATUSES = frozenset({"pass", "fail", "gray_zone"})
_STRICT_BUY_GATE_VERSION = 1
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# classification → 기본 매도성향 점수 (0~10, 높을수록 팔아도 되는 쪽)
_CLASSIFICATION_BASE_SCORE = {
    "sell_to_fund": 8.0,
    "avoid": 7.0,
    "trim": 6.0,
    "gray_zone": 4.0,
    "hold": 3.0,
    "protect": 1.0,
}
_UNKNOWN_BASE_SCORE = 2.0


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _symbol_variants(symbol: str) -> set[str]:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return set()
    out = {sym}
    if sym.endswith((".KS", ".KQ")):
        out.add(sym.split(".", 1)[0])
    elif sym.isdigit() and len(sym) == 6:
        out.add(f"{sym}.KS")
        out.add(f"{sym}.KQ")
    return out


def _default_scores_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "db" / "data" / _SCORES_FILE


def load_ai_berkshire_scores(path: str | Path | None = None) -> dict:
    """score 파일 로드. 없거나 깨졌으면 빈 dict (→ 자동매도 후보 0개)."""
    p = Path(path) if path else _default_scores_path()
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            log.warning("ai_berkshire scores malformed: %s", p)
            return {}
        return data
    except Exception as e:
        log.warning("ai_berkshire scores load failed (%s): %s", p, e)
        return {}


def _coerce_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not _ISO_DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _freshness_issues(raw: Mapping, today: date) -> list[str]:
    """thesis freshness 필수조건 검사 — 하나라도 걸리면 gray_zone 강등.

    가능한 값: missing_as_of / invalid_as_of / missing_valid_until /
    invalid_valid_until / invalid_date_range / expired / missing_thesis /
    missing_red_lines / missing_source_urls / invalid_source_urls
    """
    issues: list[str] = []

    as_of_raw = raw.get("as_of")
    as_of = _coerce_date(as_of_raw)
    if as_of_raw in (None, ""):
        issues.append("missing_as_of")
    elif as_of is None:
        issues.append("invalid_as_of")

    valid_until_raw = raw.get("valid_until")
    valid_until = _coerce_date(valid_until_raw)
    if valid_until_raw in (None, ""):
        issues.append("missing_valid_until")
    elif valid_until is None:
        issues.append("invalid_valid_until")

    if as_of and valid_until and as_of > valid_until:
        issues.append("invalid_date_range")
    if valid_until and today > valid_until:
        issues.append("expired")

    thesis = raw.get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        issues.append("missing_thesis")

    red_lines = raw.get("red_lines")
    if not isinstance(red_lines, (list, tuple)) or not any(
            isinstance(r, str) and r.strip() for r in red_lines):
        issues.append("missing_red_lines")

    source_urls = raw.get("source_urls")
    if source_urls in (None, [], ()):
        issues.append("missing_source_urls")
    elif not isinstance(source_urls, (list, tuple)):
        issues.append("invalid_source_urls")   # 문자열 하나는 목록이 아님
    elif not any(isinstance(u, str) and u.strip().startswith(("http://", "https://"))
                 for u in source_urls):
        issues.append("invalid_source_urls")

    return issues


def normalize_ai_berkshire_item(raw, as_of_date=None, *, strict_buy_gate=False) -> dict:
    """score 항목 정규화 + thesis freshness 판정.

    stored_classification은 파일에 저장된 값, classification은 만료/근거
    불량 강등을 반영한 적용값이다. valid_until 당일까지 유효, 다음 날 만료.
    as_of·valid_until·thesis·red_lines·source_urls 필수조건을 전부 통과해야
    저장 classification이 적용된다 (fail-closed).
    """
    raw = raw if isinstance(raw, Mapping) else {}
    today = _coerce_date(as_of_date) or datetime.now(KST).date()
    stored = str(raw.get("classification") or "").lower().strip() or None
    strict = bool(
        strict_buy_gate
        or raw.get("strict_buy_gate") is True
        or "buy_checklist_status" in raw
    )
    classification_valid = stored in _VALID_CLASSIFICATIONS

    issues = _freshness_issues(raw, today)
    if strict and not classification_valid:
        issues.insert(
            0,
            "missing_classification" if stored is None else "invalid_classification",
        )
    freshness_valid = not issues
    thesis_expired = "expired" in issues
    effective = stored if freshness_valid else "gray_zone"

    raw_urls = raw.get("source_urls")
    source_urls = ([str(u).strip() for u in raw_urls if str(u or "").strip()]
                   if isinstance(raw_urls, (list, tuple)) else [])
    raw_red_lines = raw.get("red_lines")
    red_lines = ([str(r) for r in raw_red_lines]
                 if isinstance(raw_red_lines, (list, tuple)) else [])
    # 감사 필드: 갱신 근거 URL은 HTTP(S) 문자열만, reason/checked_at은 공백 정리.
    raw_evidence_urls = raw.get("evidence_urls")
    evidence_urls = (
        [
            str(url).strip()
            for url in raw_evidence_urls
            if isinstance(url, str)
            and url.strip().startswith(("http://", "https://"))
        ]
        if isinstance(raw_evidence_urls, (list, tuple))
        else []
    )
    classification_change_reason = raw.get("classification_change_reason")
    checked_at = raw.get("checked_at")

    item = {
        "name": str(raw.get("name") or ""),
        "stored_classification": stored,
        "classification": effective,
        "classification_valid": classification_valid,
        "strict_buy_gate": strict,
        "thesis_expired": thesis_expired,
        "freshness_valid": freshness_valid,
        "freshness_issues": issues,
        "as_of": str(raw.get("as_of") or "") or None,
        "valid_until": str(raw.get("valid_until") or "") or None,
        "thesis": str(raw.get("thesis") or "") or None,
        "red_lines": red_lines,
        "sell_to_fund_adjustment": _num(raw.get("sell_to_fund_adjustment"), 0.0),
        "confidence": str(raw.get("confidence") or "medium"),
        "source_urls": source_urls,
        "buy_checklist_status": (
            str(raw.get("buy_checklist_status") or "").lower().strip() or None
        ),
        # bool만 명시적 override로 인정한다. 문자열 "false" 등은 권한이 없다.
        "auto_sell_eligible": (
            raw.get("auto_sell_eligible")
            if isinstance(raw.get("auto_sell_eligible"), bool)
            else None
        ),
        "auto_sell_config_valid": isinstance(raw.get("auto_sell_eligible"), bool),
        "classification_change_reason": (
            classification_change_reason.strip()
            if isinstance(classification_change_reason, str)
            and classification_change_reason.strip()
            else None
        ),
        "evidence_urls": evidence_urls,
        "checked_at": (
            checked_at.strip()
            if isinstance(checked_at, str) and checked_at.strip()
            else None
        ),
    }
    item["berkshire_score"] = compute_berkshire_score(item)
    return item


def compute_berkshire_score(scores) -> float:
    """항목 → 매도성향 점수 (0~10). classification 기본값 + adjustment."""
    scores = scores if isinstance(scores, Mapping) else {}
    classification = str(scores.get("classification") or "").lower().strip()
    base = _CLASSIFICATION_BASE_SCORE.get(classification, _UNKNOWN_BASE_SCORE)
    value = base + _num(scores.get("sell_to_fund_adjustment"), 0.0)
    return round(max(0.0, min(value, 10.0)), 2)


def score_for_symbol(symbol: str, scores: Mapping | None = None,
                     as_of_date=None) -> dict | None:
    """심볼(6자리 코드 ↔ .KS/.KQ 변형 포함)로 정규화된 score 항목 조회."""
    if scores is None:
        scores = load_ai_berkshire_scores()
    items = scores.get("items") if isinstance(scores, Mapping) else None
    if not isinstance(items, Mapping):
        return None
    wanted = _symbol_variants(symbol)
    if not wanted:
        return None
    for key, raw in items.items():
        if _symbol_variants(key) & wanted:
            return normalize_ai_berkshire_item(
                raw,
                as_of_date=as_of_date,
                strict_buy_gate=(
                    scores.get("strict_buy_gate_version") == _STRICT_BUY_GATE_VERSION
                ),
            )
    return None


def _buy_gate_result(symbol: str, item: dict | None, buy_block: bool,
                     buy_reason: str, research_status: str) -> dict:
    return {
        "symbol": str(symbol or "").upper().strip(),
        "name": item["name"] if item else "",
        "stored_classification": item["stored_classification"] if item else None,
        "classification": item["classification"] if item else None,
        "classification_valid": item["classification_valid"] if item else False,
        "strict_buy_gate": item["strict_buy_gate"] if item else False,
        "freshness_valid": item["freshness_valid"] if item else False,
        "thesis_expired": item["thesis_expired"] if item else False,
        "freshness_issues": item["freshness_issues"] if item else [],
        "buy_block": buy_block,
        "buy_reason": buy_reason,
        "research_status": research_status,
        "as_of": item["as_of"] if item else None,
        "valid_until": item["valid_until"] if item else None,
        "thesis": item["thesis"] if item else None,
        "red_lines": item["red_lines"] if item else [],
        "confidence": item["confidence"] if item else None,
        "source_urls": item["source_urls"] if item else [],
        "buy_checklist_status": item["buy_checklist_status"] if item else None,
        "classification_change_reason": (
            item["classification_change_reason"] if item else None
        ),
        "evidence_urls": item["evidence_urls"] if item else [],
        "checked_at": item["checked_at"] if item else None,
    }


def evaluate_ai_berkshire_buy_gate(symbol: str, scores: Mapping | None = None,
                                   as_of_date=None) -> dict:
    """신규 BUY 1건에 대한 AI Berkshire 판정.

    strict row는 classification/checklist/freshness가 하나라도 손상되면 차단한다.
    marker 없는 legacy row는 avoid_only 호환을 유지한다. score 파일 누락/파손은
    예외 대신 needs_research 진단으로 반환하며 dispatch 계층이 fail-closed한다.

    research_status: ok / needs_research / expired / invalid
    입력 scores/row는 변경하지 않는다.
    """
    if scores is None:
        try:
            scores = load_ai_berkshire_scores()
        except Exception as e:                       # score 파일 IO 오류도 fail-open 진단
            log.warning("ai_berkshire buy gate scores load failed: %s", e)
            scores = {}

    items = scores.get("items") if isinstance(scores, Mapping) else None
    if not isinstance(items, Mapping) or not items:
        return _buy_gate_result(symbol, None, False,
                                "ai_berkshire_scores_unavailable", "needs_research")

    try:
        item = score_for_symbol(symbol, scores, as_of_date=as_of_date)
    except Exception as e:
        log.warning("ai_berkshire buy gate lookup failed (%s): %s", symbol, e)
        item = None

    if item is None:
        return _buy_gate_result(symbol, None, False,
                                "ai_berkshire_unscored", "needs_research")

    checklist = item["buy_checklist_status"]
    strict_checklist = item["strict_buy_gate"]
    if strict_checklist and not item["classification_valid"]:
        return _buy_gate_result(
            symbol,
            item,
            True,
            "ai_berkshire_strict_classification_invalid",
            "invalid",
        )
    if strict_checklist and checklist is None:
        return _buy_gate_result(
            symbol,
            item,
            True,
            "ai_berkshire_buy_checklist_missing",
            "invalid",
        )
    if strict_checklist and checklist not in _VALID_BUY_CHECKLIST_STATUSES:
        return _buy_gate_result(
            symbol,
            item,
            True,
            "ai_berkshire_buy_checklist_unknown",
            "invalid",
        )
    if not item["stored_classification"]:
        return _buy_gate_result(symbol, item, False,
                                "ai_berkshire_unscored", "needs_research")
    if item["thesis_expired"]:
        return _buy_gate_result(
            symbol,
            item,
            strict_checklist,
            (
                "ai_berkshire_strict_thesis_expired"
                if strict_checklist
                else "ai_berkshire_thesis_expired"
            ),
            "expired",
        )
    if not item["freshness_valid"]:
        return _buy_gate_result(
            symbol,
            item,
            strict_checklist,
            (
                "ai_berkshire_strict_thesis_invalid"
                if strict_checklist
                else "ai_berkshire_thesis_invalid"
            ),
            "invalid",
        )

    if checklist in _BUY_CHECKLIST_BLOCK_STATUSES:
        return _buy_gate_result(
            symbol,
            item,
            True,
            f"ai_berkshire_buy_checklist_{checklist}",
            "ok",
        )
    if strict_checklist and checklist != "pass":
        return _buy_gate_result(
            symbol,
            item,
            True,
            "ai_berkshire_buy_checklist_unknown",
            "invalid",
        )

    effective = item["classification"]
    if effective in _BUY_BLOCK_CLASSIFICATIONS:
        return _buy_gate_result(symbol, item, True, "ai_berkshire_avoid", "ok")
    if effective in _BUY_REVIEWED_CLASSIFICATIONS:
        return _buy_gate_result(symbol, item, False, "reviewed_non_avoid", "ok")
    # gray_zone 등 판단 유보 — 차단하지 않고 재리서치 대상으로만 표시
    return _buy_gate_result(symbol, item, False,
                            f"ai_berkshire_{effective}", "invalid")


def apply_berkshire_to_sell_to_fund(
    candidates: Iterable[Mapping] | None,
    scores: Mapping | None = None,
    as_of_date=None,
) -> list[dict]:
    """sell_to_fund 후보 rows에 AI Berkshire 판정을 병합한다.

    입력 rows는 변경하지 않고 새 dict 목록을 반환한다.
    adjusted_sell_priority(= weakness_score + adjustment) 내림차순 정렬.
    eligibility는 만료 강등이 반영된 effective classification 기준.
    """
    if scores is None:
        scores = load_ai_berkshire_scores()
    items = scores.get("items") if isinstance(scores, Mapping) else None
    scores_available = isinstance(items, Mapping) and bool(items)

    out: list[dict] = []
    for row in candidates or []:
        if not isinstance(row, Mapping):
            continue
        merged = dict(row)
        weakness = _num(merged.get("weakness_score"), 0.0)
        item = (score_for_symbol(merged.get("symbol") or "", scores, as_of_date=as_of_date)
                if scores_available else None)

        if not scores_available:
            eligible, block_reason = False, "ai_berkshire_scores_unavailable"
        elif item is None:
            eligible, block_reason = False, "ai_berkshire_unscored"
        elif item["thesis_expired"]:
            eligible, block_reason = False, "ai_berkshire_thesis_expired"
        elif not item["freshness_valid"]:
            eligible, block_reason = False, "ai_berkshire_thesis_invalid"
        elif item["strict_buy_gate"] and not item["auto_sell_config_valid"]:
            eligible, block_reason = False, "ai_berkshire_strict_auto_sell_invalid"
        elif item["auto_sell_eligible"] is False:
            eligible, block_reason = False, "ai_berkshire_auto_sell_disabled"
        elif item["classification"] in _AUTO_SELL_CLASSIFICATIONS:
            eligible, block_reason = True, None
        else:
            eligible = False
            block_reason = f"ai_berkshire_{item['classification'] or 'unscored'}"

        adjustment = item["sell_to_fund_adjustment"] if item else 0.0
        merged["ai_berkshire"] = {
            "stored_classification": item["stored_classification"] if item else None,
            "classification": item["classification"] if item else None,
            "classification_valid": item["classification_valid"] if item else False,
            "strict_buy_gate": item["strict_buy_gate"] if item else False,
            "thesis_expired": item["thesis_expired"] if item else False,
            "freshness_valid": item["freshness_valid"] if item else False,
            "freshness_issues": item["freshness_issues"] if item else [],
            "as_of": item["as_of"] if item else None,
            "valid_until": item["valid_until"] if item else None,
            "thesis": item["thesis"] if item else None,
            "red_lines": item["red_lines"] if item else [],
            "berkshire_score": item["berkshire_score"] if item else None,
            "confidence": item["confidence"] if item else None,
            "source_urls": item["source_urls"] if item else [],
            "buy_checklist_status": item["buy_checklist_status"] if item else None,
            "score_auto_sell_eligible": item["auto_sell_eligible"] if item else None,
            "auto_sell_config_valid": item["auto_sell_config_valid"] if item else False,
            "classification_change_reason": (
                item["classification_change_reason"] if item else None
            ),
            "evidence_urls": item["evidence_urls"] if item else [],
            "checked_at": item["checked_at"] if item else None,
        }
        merged["sell_to_fund_adjustment"] = adjustment
        merged["adjusted_sell_priority"] = round(weakness + adjustment, 4)
        merged["auto_sell_eligible"] = eligible
        merged["auto_sell_block_reason"] = block_reason
        out.append(merged)

    out.sort(key=lambda r: r.get("adjusted_sell_priority", 0.0), reverse=True)
    return out
