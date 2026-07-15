"""
한국 시장 강화 — 네이버 금융 조회

KRX data.krx.co.kr가 투자자별 매매/펀더멘털 데이터를 로그인 게이트로 막아
(OTP 응답 "LOGOUT") 직접 호출이 불가능해졌다. 로그인 불필요한 네이버 금융을
스크래핑하여 기관/외국인 순매매와 펀더멘털(PER/PBR/배당률)을 수집한다.
"""

from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config.settings import KRW_TICKERS
from core.market_data_fetch import CacheSource, FetchErrorType, FetchResult, FetchStatus

log = logging.getLogger(__name__)

# 네이버 금융 (로그인 불필요, 공개 데이터)
NAVER_FRGN_URL = "https://finance.naver.com/item/frgn.naver"
NAVER_MAIN_URL = "https://finance.naver.com/item/main.naver"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}

# 종목코드 → 종목명 매핑 (네이버는 이름을 안 주므로 설정에서 보강)
_NAME_MAP: dict[str, str] = {}


def _name_for(code: str) -> str:
    """KRX 종목코드(6자리)로 종목명 조회. 설정의 포트폴리오/관심/RIA 맵 통합."""
    global _NAME_MAP
    if not _NAME_MAP:
        from config.settings import PORTFOLIO, RIA_ALLOWED_TICKERS, WATCHLIST

        for src in (PORTFOLIO, WATCHLIST, RIA_ALLOWED_TICKERS):
            for tk, nm in src.items():
                _NAME_MAP[_krx_ticker(tk)] = nm
    return _NAME_MAP.get(code, code)


# 네이버 frgn 페이지 일별 시리즈 캐시 (프로세스 수명 동안 — 브리핑은 단명 프로세스)
_FRGN_CACHE: dict[str, list[dict]] = {}
# 신규 typed 경로는 원 조회 완료시각을 별도 보존한다. 기존 list cache shape는 유지한다.
_FRGN_CACHE_FETCHED_AT: dict[str, datetime] = {}

# 파일 캐시: 배치 사전수집(tools/supply_demand_warm_cache.py)이 채우고
# bot/dashboard 등 다른 프로세스가 소비한다. 일별 데이터라 26시간 유효.
_FRGN_FILE_TTL_HOURS = 26.0


class _NaverParseError(ValueError):
    """공급자 원문 없이 typed fetcher가 분류할 수 있는 파싱 오류."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def _naver_number(value: object) -> float:
    if isinstance(value, bool) or value is None:
        raise _NaverParseError("numeric")
    text = str(value).replace(",", "").strip()
    if not text:
        raise _NaverParseError("numeric")
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise _NaverParseError("numeric") from None
    if not math.isfinite(number):
        raise _NaverParseError("numeric")
    return number


def _parse_naver_frgn_tables(tables, code: str) -> dict:
    nine_column_tables = []
    for table in tables:
        shape = getattr(table, "shape", None)
        if shape is not None and len(shape) == 2 and shape[1] == 9:
            nine_column_tables.append(table)
    if not nine_column_tables:
        raise _NaverParseError("malformed")

    valid_empty_seen = False
    candidate_errors: list[str] = []
    selected_rows: list[dict] | None = None
    for table in nine_column_tables:
        if table.shape[0] == 0:
            valid_empty_seen = True
            continue

        rows: list[dict] = []
        try:
            for _, row in table.iterrows():
                values = list(row)
                if len(values) != 9:
                    raise _NaverParseError("malformed")
                raw_date = str(values[0]).strip()
                match = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2})", raw_date)
                if not match:
                    continue
                date = f"{match.group(1)}{match.group(2)}{match.group(3)}"
                try:
                    datetime.strptime(date, "%Y%m%d")
                except ValueError:
                    raise _NaverParseError("malformed") from None
                rows.append(
                    {
                        "date": date,
                        "close": _naver_number(values[1]),
                        "inst_shares": _naver_number(values[5]),
                        "foreign_shares": _naver_number(values[6]),
                    }
                )
            if not rows:
                raise _NaverParseError("malformed")
        except _NaverParseError as exc:
            candidate_errors.append(exc.error_type)
            continue

        selected_rows = rows
        break

    if selected_rows is None:
        if valid_empty_seen:
            selected_rows = []
        else:
            error_type = "numeric" if "numeric" in candidate_errors else "malformed"
            raise _NaverParseError(error_type)

    normalized_code = code.replace(".KS", "").replace(".KQ", "")
    return {
        "code": normalized_code,
        "rows": selected_rows,
        "units": {
            "date": "business_date",
            "close": "KRW/share",
            "inst_shares": "shares",
            "foreign_shares": "shares",
        },
        "derived_schema_version": "1",
    }


def parse_naver_frgn_html(html: str, code: str, table_reader=None) -> dict:
    """네이버 일별 외국인·기관 HTML을 네트워크 없이 순수 파싱한다."""
    if not isinstance(html, str):
        raise _NaverParseError("malformed")
    try:
        source = io.StringIO(html)
        if table_reader is None:
            import pandas as pd

            tables = pd.read_html(source)
        else:
            tables = table_reader(source)
        return _parse_naver_frgn_tables(tables, code)
    except _NaverParseError:
        raise
    except Exception:
        raise _NaverParseError("malformed") from None


def _frgn_file_cache_path():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "db" / "data" / "kr_frgn_cache.json"


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class _FrgnFileEntryResult:
    """파일 캐시 상태와 원 조회시각을 typed 경로에 전달한다."""

    status: str
    rows: list[dict] | None = None
    fetched_at: datetime | None = None


def _valid_frgn_rows(rows: object) -> bool:
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        date = row.get("date")
        if not isinstance(date, str) or re.fullmatch(r"\d{8}", date) is None:
            return False
        try:
            datetime.strptime(date, "%Y%m%d")
        except ValueError:
            return False
        for key in ("close", "inst_shares", "foreign_shares"):
            value = row.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                return False
        if row["close"] < 0:
            return False
        if not float(row["inst_shares"]).is_integer():
            return False
        if not float(row["foreign_shares"]).is_integer():
            return False
    return True


def _valid_frgn_memory_rows(rows: object) -> bool:
    return isinstance(rows, list) and (not rows or _valid_frgn_rows(rows))


def _load_frgn_file_entry_result(
    code: str, *, now: datetime | None = None
) -> _FrgnFileEntryResult:
    """파일 캐시를 missing/fresh/stale/corrupt로 구분해 읽는다."""
    import json as _json

    current = _aware_utc(now or datetime.now(timezone.utc))
    p = _frgn_file_cache_path()
    if not p.exists():
        return _FrgnFileEntryResult("missing")
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug(
            "frgn 파일 캐시 읽기 실패 (%s): exception_class=%s",
            code,
            type(exc).__name__,
        )
        return _FrgnFileEntryResult("corrupt")
    if not isinstance(data, dict):
        return _FrgnFileEntryResult("corrupt")

    entry = data.get(code)
    if entry is None:
        return _FrgnFileEntryResult("missing")
    if not isinstance(entry, dict):
        return _FrgnFileEntryResult("corrupt")
    rows = entry.get("rows")
    if not _valid_frgn_rows(rows):
        return _FrgnFileEntryResult("corrupt")
    try:
        fetched_at = _aware_utc(datetime.fromisoformat(str(entry.get("fetched_at"))))
    except (TypeError, ValueError):
        return _FrgnFileEntryResult("corrupt")

    age_hours = (current - fetched_at).total_seconds() / 3600.0
    if age_hours < 0:
        return _FrgnFileEntryResult("corrupt")
    if age_hours > _FRGN_FILE_TTL_HOURS:
        return _FrgnFileEntryResult("stale", rows, fetched_at)
    return _FrgnFileEntryResult("fresh", rows, fetched_at)


def _load_frgn_file_entry(code: str) -> list[dict] | None:
    """파일 캐시에서 fresh 항목 조회. 없거나 만료/파손이면 None."""
    result = _load_frgn_file_entry_result(code)
    return result.rows if result.status == "fresh" else None


def _save_frgn_file_entry(
    code: str, rows: list[dict], *, fetched_at: datetime | None = None
) -> None:
    """파일 캐시에 항목 기록 (atomic replace, 실패 무해)."""
    import json as _json
    import os as _os
    import tempfile

    if not _valid_frgn_rows(rows):
        return  # 실패 결과로 정상 캐시를 덮지 않는다
    timestamp = _aware_utc(fetched_at or datetime.now(timezone.utc))
    try:
        p = _frgn_file_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if p.exists():
            try:
                data = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[code] = {
            "fetched_at": timestamp.isoformat(),
            "rows": rows,
        }
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_json.dumps(data, ensure_ascii=False))
        _os.replace(tmp, p)
    except Exception as exc:
        log.debug(
            "frgn 파일 캐시 쓰기 실패 (%s): exception_class=%s",
            code,
            type(exc).__name__,
        )


def _clock_now_utc(clock=None) -> datetime:
    return _aware_utc(datetime.now(timezone.utc) if clock is None else clock())


def _naver_value(code: str, rows: list[dict]) -> dict:
    return {
        "code": code,
        "rows": rows,
        "units": {
            "date": "business_date",
            "close": "KRW/share",
            "inst_shares": "shares",
            "foreign_shares": "shares",
        },
        "derived_schema_version": "1",
    }


def _naver_fetch_result(
    *,
    status: FetchStatus,
    code: str,
    started: datetime,
    completed: datetime,
    error_type: FetchErrorType,
    cache_source: CacheSource,
    fallback_used: bool,
    value: dict | None,
    source_fetched_at: datetime | None = None,
) -> FetchResult[dict]:
    return FetchResult(
        status=status,
        provider="NAVER",
        endpoint=NAVER_FRGN_URL,
        tr_id=None,
        venue="KRX",
        symbol=code,
        started_at_utc=started,
        completed_at_utc=completed,
        error_type=error_type,
        cache_source=cache_source,
        fallback_used=fallback_used,
        value=value,
        source_fetched_at_utc=source_fetched_at,
    )


def fetch_naver_frgn_result(
    code: str,
    force_refresh: bool = False,
    fallback_used: bool = False,
    clock=None,
    http_get=None,
    table_reader=None,
) -> FetchResult[dict]:
    """네이버 외국인·기관 순매매를 캐시 시각까지 보존해 typed 조회한다."""
    if type(fallback_used) is not bool:
        raise TypeError("fallback_used must be bool")

    symbol = code.replace(".KS", "").replace(".KQ", "")
    started = _clock_now_utc(clock)
    if not force_refresh:
        if symbol in _FRGN_CACHE:
            rows = _FRGN_CACHE[symbol]
            if _valid_frgn_memory_rows(rows):
                fetched_at = _FRGN_CACHE_FETCHED_AT.get(symbol)
                if fetched_at is None:
                    return _naver_fetch_result(
                        status=FetchStatus.INCOMPLETE,
                        code=symbol,
                        started=started,
                        completed=_clock_now_utc(clock),
                        error_type=FetchErrorType.CACHE_TIMESTAMP_MISSING,
                        cache_source=CacheSource.MEMORY,
                        fallback_used=fallback_used,
                        value=_naver_value(symbol, rows),
                    )
                try:
                    original = _aware_utc(fetched_at)
                except (TypeError, ValueError):
                    original = None
                if original is not None:
                    age_hours = (started - original).total_seconds() / 3600.0
                    if 0.0 <= age_hours <= _FRGN_FILE_TTL_HOURS:
                        return _naver_fetch_result(
                            status=(
                                FetchStatus.SUCCESS if rows else FetchStatus.EMPTY
                            ),
                            code=symbol,
                            started=started,
                            completed=_clock_now_utc(clock),
                            error_type=FetchErrorType.NONE,
                            cache_source=CacheSource.MEMORY,
                            fallback_used=fallback_used,
                            value=_naver_value(symbol, rows),
                            source_fetched_at=original,
                        )

        file_entry = _load_frgn_file_entry_result(symbol, now=started)
        if file_entry.status == "fresh":
            rows = file_entry.rows
            original = file_entry.fetched_at
            assert rows is not None and original is not None
            _FRGN_CACHE[symbol] = rows
            _FRGN_CACHE_FETCHED_AT[symbol] = original
            return _naver_fetch_result(
                status=FetchStatus.SUCCESS,
                code=symbol,
                started=started,
                completed=_clock_now_utc(clock),
                error_type=FetchErrorType.NONE,
                cache_source=CacheSource.FILE,
                fallback_used=fallback_used,
                value=_naver_value(symbol, rows),
                source_fetched_at=original,
            )

    request = requests.get if http_get is None else http_get
    try:
        response = request(
            NAVER_FRGN_URL,
            params={"code": symbol},
            headers=HEADERS,
            timeout=10,
        )
    except Exception as exc:
        log.warning(
            "네이버 typed 수급 네트워크 실패 (%s): exception_class=%s",
            symbol,
            type(exc).__name__,
        )
        return _naver_fetch_result(
            status=FetchStatus.FAILED,
            code=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.NETWORK,
            cache_source=CacheSource.NONE,
            fallback_used=fallback_used,
            value=None,
        )

    if getattr(response, "status_code", None) != 200:
        log.warning("네이버 typed 수급 HTTP 실패 (%s): error_type=http", symbol)
        return _naver_fetch_result(
            status=FetchStatus.FAILED,
            code=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.HTTP,
            cache_source=CacheSource.NONE,
            fallback_used=fallback_used,
            value=None,
        )

    try:
        parsed = parse_naver_frgn_html(response.text, symbol, table_reader=table_reader)
    except _NaverParseError as exc:
        error_type = FetchErrorType(exc.error_type)
        log.warning(
            "네이버 typed 수급 파싱 실패 (%s): error_type=%s exception_class=%s",
            symbol,
            error_type.value,
            type(exc).__name__,
        )
        return _naver_fetch_result(
            status=FetchStatus.FAILED,
            code=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=error_type,
            cache_source=CacheSource.NONE,
            fallback_used=fallback_used,
            value=None,
        )
    except Exception as exc:
        log.warning(
            "네이버 typed 수급 파싱 실패 (%s): "
            "error_type=malformed exception_class=%s",
            symbol,
            type(exc).__name__,
        )
        return _naver_fetch_result(
            status=FetchStatus.FAILED,
            code=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.MALFORMED,
            cache_source=CacheSource.NONE,
            fallback_used=fallback_used,
            value=None,
        )

    completed = _clock_now_utc(clock)
    rows = parsed["rows"]
    _FRGN_CACHE[symbol] = rows
    _FRGN_CACHE_FETCHED_AT[symbol] = completed
    if rows:
        _save_frgn_file_entry(symbol, rows, fetched_at=completed)
    return _naver_fetch_result(
        status=FetchStatus.SUCCESS if rows else FetchStatus.EMPTY,
        code=symbol,
        started=started,
        completed=completed,
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        fallback_used=fallback_used,
        value=parsed,
    )


def _fetch_naver_frgn(code: str, *, force_refresh: bool = False) -> list[dict]:
    """기존 list 계약을 typed 네이버 조회 결과에서 투영한다."""
    result = fetch_naver_frgn_result(code, force_refresh=force_refresh)
    if result.status not in (FetchStatus.SUCCESS, FetchStatus.INCOMPLETE):
        return []
    if not isinstance(result.value, dict):
        return []
    rows = result.value.get("rows")
    return rows if isinstance(rows, list) else []


def _flow_from_row(code: str, row: dict) -> "InstitutionalFlow":
    """네이버 일별 행(주식수) → InstitutionalFlow(백만원 환산)."""
    close = row["close"]
    # 순매매량(주) × 종가(원) / 1e6 = 순매매대금(백만원)
    foreign_won = row["foreign_shares"] * close / 1_000_000
    inst_won = row["inst_shares"] * close / 1_000_000
    return InstitutionalFlow(
        ticker=f"{code}.KS",
        name=_name_for(code),
        foreign_net=foreign_won,
        institution_net=inst_won,
        individual_net=0.0,  # 네이버 frgn 페이지는 개인 순매매 미제공
        foreign_label="매수" if foreign_won > 0 else "매도",
        institution_label="매수" if inst_won > 0 else "매도",
    )


@dataclass(frozen=True)
class InstitutionalFlow:
    """기관/외국인 매매 동향."""

    ticker: str
    name: str
    foreign_net: float  # 외국인 순매수 (금액)
    institution_net: float  # 기관 순매수 (금액)
    individual_net: float  # 개인 순매수 (금액)
    foreign_label: str  # 매수/매도
    institution_label: str


@dataclass(frozen=True)
class Fundamental:
    """종목 펀더멘털."""

    ticker: str
    name: str
    per: float = 0.0  # PER
    pbr: float = 0.0  # PBR
    eps: float = 0.0  # EPS
    bps: float = 0.0  # BPS
    div_yield: float = 0.0  # 배당수익률 (%)
    market_cap: float = 0.0  # 시가총액 (억원)


def _krx_ticker(ticker: str) -> str:
    """yfinance 티커를 KRX 종목코드로 변환. 예: 005930.KS → 005930"""
    return ticker.replace(".KS", "").replace(".KQ", "")


def fetch_institutional_flow(date: str | None = None) -> list[InstitutionalFlow]:
    """네이버 금융 투자자별 매매 동향 조회 (포트폴리오 종목).

    Args:
        date: 조회 날짜 (YYYYMMDD). None이면 각 종목의 최신 거래일.

    Returns:
        InstitutionalFlow 리스트 (외국인/기관 순매매를 백만원으로 환산)
    """
    results: list[InstitutionalFlow] = []
    codes = {_krx_ticker(tk) for tk in KRW_TICKERS}

    for code in codes:
        series = _fetch_naver_frgn(code)
        if not series:
            continue

        row = None
        if date is not None:
            row = next((r for r in series if r["date"] == date), None)
        if row is None:
            row = series[0]  # 최신 거래일

        results.append(_flow_from_row(code, row))

    if not results:
        log.warning("네이버 기관/외국인 매매 조회 실패 (전 종목)")
    return results


def _fetch_naver_fundamental(code: str) -> Fundamental | None:
    """네이버 금융 종목 페이지에서 PER/EPS/PBR/배당률 조회."""
    try:
        r = requests.get(
            NAVER_MAIN_URL, params={"code": code}, headers=HEADERS, timeout=10
        )
        if r.status_code != 200:
            return None
        r.encoding = "euc-kr"
        html = r.text

        def from_id(eid: str) -> float:
            m = re.search(rf'id="{eid}"[^>]*>([^<]+)<', html)
            if not m:
                return 0.0
            try:
                return float(m.group(1).replace(",", "").strip())
            except ValueError:
                return 0.0

        def from_label(label: str) -> float:
            # 투자정보 테이블의 "배당수익률 N.NN%" 형태
            m = re.search(rf'{label}[^0-9\-]*?([\-]?[\d,]+\.?\d*)', html)
            if not m:
                return 0.0
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                return 0.0

        per = from_id("_per")
        eps = from_id("_eps")
        pbr = from_id("_pbr")
        div_yield = from_label("배당수익률")

        if per == 0.0 and pbr == 0.0 and eps == 0.0:
            return None  # 데이터 미확보 → 폴백 유도

        return Fundamental(
            ticker=f"{code}.KS",
            name=_name_for(code),
            per=per,
            pbr=pbr,
            eps=eps,
            bps=0.0,  # 네이버 메인 페이지 미제공
            div_yield=div_yield,
            market_cap=0.0,  # 네이버 메인 페이지 미제공
        )
    except Exception as e:
        log.warning(f"네이버 펀더멘털 조회 실패 ({code}): {e}")
        return None


def fetch_fundamentals() -> list[Fundamental]:
    """네이버 금융 종목별 펀더멘털 데이터 조회 (PER/PBR/배당률).

    Returns:
        Fundamental 리스트 (포트폴리오 종목만)
    """
    results: list[Fundamental] = []
    codes = {_krx_ticker(tk) for tk in KRW_TICKERS}
    for code in codes:
        f = _fetch_naver_fundamental(code)
        if f:
            results.append(f)
    return results


def fetch_cumulative_flows(days: int = 5) -> dict[str, dict]:
    """N일 누적 외국인/기관 순매수 데이터 — 네이버 종목별 1회 조회.

    Returns:
        {ticker: {"foreign_5d": sum, "institution_5d": sum, "foreign_trend": "매수전환/매도지속/..."}}
    """
    cumulative: dict[str, dict] = {}
    codes = {_krx_ticker(tk) for tk in KRW_TICKERS}

    for code in codes:
        series = _fetch_naver_frgn(code)
        if not series:
            continue

        ticker = f"{code}.KS"
        # 네이버는 최신순 → 최근 days일을 일별 환산(백만원)
        recent = [_flow_from_row(code, r) for r in series[:days]]
        if not recent:
            continue

        cumulative[ticker] = {
            "name": recent[0].name,
            "foreign_5d": sum(f.foreign_net for f in recent),
            "institution_5d": sum(f.institution_net for f in recent),
            "daily_foreign": [f.foreign_net for f in recent],  # 최신순
        }

    # 추세 레이블 생성
    for ticker, data in cumulative.items():
        daily = data.get("daily_foreign", [])
        cum = data["foreign_5d"]

        if len(daily) < 2:
            data["foreign_trend"] = "데이터부족"
        elif cum > 0 and daily[0] > 0:
            data["foreign_trend"] = "매수지속" if all(d > 0 for d in daily) else "매수전환"
        elif cum < 0 and daily[0] < 0:
            data["foreign_trend"] = "매도지속" if all(d < 0 for d in daily) else "매도전환"
        elif cum > 0:
            data["foreign_trend"] = "매수전환"
        elif cum < 0:
            data["foreign_trend"] = "매도전환"
        else:
            data["foreign_trend"] = "중립"

        # daily_foreign은 텍스트 생성에 불필요하므로 제거
        del data["daily_foreign"]

    return cumulative


def fetch_short_selling(days: int = 10) -> dict[str, dict]:
    """KIS 공매도 일별추이 기반 종목별 공매도 요약 (포트폴리오 KR 종목).

    KRX 잔고 데이터는 로그인 게이트라 불가 → KIS '거래' 기반 지표 사용.

    Returns:
        {ticker: {"name", "short_ratio_pct"(당일), "avg5_ratio_pct"(5일 평균),
                  "trend"(급증/증가/보통/감소), "date"}}
    """
    from datetime import datetime, timedelta, timezone

    from core.market_kis import get_domestic_short_sale

    KST = timezone(timedelta(hours=9))
    end = datetime.now(KST)
    start = end - timedelta(days=days + 7)  # 휴장일 여유

    results: dict[str, dict] = {}
    for tk in KRW_TICKERS:
        series = get_domestic_short_sale(
            tk, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        )
        if not series:
            continue
        latest = series[0]
        ratios = [r["short_ratio_pct"] for r in series[:5]]
        avg5 = sum(ratios) / len(ratios) if ratios else 0.0
        today = latest["short_ratio_pct"]

        if avg5 >= 1 and today >= avg5 * 2 and today >= 8:
            trend = "급증"
        elif today > avg5 * 1.3 and today >= 5:
            trend = "증가"
        elif today < avg5 * 0.6:
            trend = "감소"
        else:
            trend = "보통"

        results[tk] = {
            "name": _name_for(_krx_ticker(tk)),
            "short_ratio_pct": today,
            "avg5_ratio_pct": round(avg5, 2),
            "trend": trend,
            "date": latest["date"],
        }
    return results


def kr_market_to_text(
    flows: list[InstitutionalFlow],
    fundamentals: list[Fundamental],
    cumulative: dict[str, dict] | None = None,
    short_selling: dict[str, dict] | None = None,
) -> str:
    """한국 시장 데이터를 텍스트로 변환."""
    lines = ["【한국 시장 심층 데이터】"]

    if flows:
        lines.append("\n  [기관/외국인 매매 동향]")
        for f in flows:
            cum_info = ""
            if cumulative and f.ticker in cumulative:
                c = cumulative[f.ticker]
                cum_info = (
                    f" | 5일 누적 외국인 {c['foreign_5d']:+,.0f}백만 ({c['foreign_trend']})"
                    f" | 5일 누적 기관 {c['institution_5d']:+,.0f}백만"
                )
            lines.append(
                f"  {f.name}: 외국인 {f.foreign_net:+,.0f}백만 ({f.foreign_label}) | "
                f"기관 {f.institution_net:+,.0f}백만 ({f.institution_label}){cum_info}"
            )

    if short_selling:
        lines.append("\n  [공매도 거래 비중 — KIS 일별추이 (잔고 아님)]")
        for tk, s in sorted(
            short_selling.items(),
            key=lambda kv: kv[1]["short_ratio_pct"],
            reverse=True,
        ):
            mark = " ⚠️" if s["trend"] in ("급증", "증가") else ""
            lines.append(
                f"  {s['name']}: 당일 {s['short_ratio_pct']:.1f}% | "
                f"5일 평균 {s['avg5_ratio_pct']:.1f}% | {s['trend']}{mark}"
            )

    if fundamentals:
        lines.append("\n  [펀더멘털]")
        for f in fundamentals:
            lines.append(
                f"  {f.name}: PER {f.per:.1f} | PBR {f.pbr:.2f} | "
                f"EPS {f.eps:,.0f} | 배당률 {f.div_yield:.1f}% | "
                f"시총 {f.market_cap:,.0f}억"
            )

    if len(lines) == 1:
        lines.append("  (데이터 없음 — 비거래일 또는 조회 실패)")

    return "\n".join(lines)
