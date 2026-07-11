"""결정론 포트폴리오 군집 위험 엔진.

보유 비중·정적 분류·선택적 가격 상관행렬만 사용한다. LLM, 주문, DB 쓰기,
브로커 호출이 없으며 입력 dict를 변경하지 않는다.

테마 비중은 한 종목이 여러 테마에 속할 수 있어 합계가 100%를 넘을 수 있다.
섹터/지역/경제통화는 종목당 하나만 배정되어 투자자산 기준 합계가 100%다.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

VERSION = "portfolio_cluster_risk_v1"

DEFAULT_THRESHOLDS: dict[str, tuple[float, float]] = {
    "position": (15.0, 25.0),
    "position_etf": (25.0, 40.0),
    "sector": (40.0, 45.0),
    "theme": (40.0, 55.0),
    "region": (70.0, 85.0),
    "economic_currency": (70.0, 85.0),
    "correlation_cluster": (35.0, 50.0),
    "unknown": (10.0, 20.0),
}

# 경제적 노출 분류다. 거래통화와 다를 수 있다(예: 국내상장 미국 ETF).
# ETF 내부 종목별 look-through 비중을 추정하지 않고 대표 위험요인만 태깅한다.
DEFAULT_TAXONOMY: dict[str, dict[str, Any]] = {
    "005930.KS": {"sector": "semiconductors", "region": "KR", "economic_currency": "KRW", "themes": ["ai_semiconductor", "memory_cycle"], "instrument_type": "stock"},
    "000660.KS": {"sector": "semiconductors", "region": "KR", "economic_currency": "KRW", "themes": ["ai_semiconductor", "memory_cycle"], "instrument_type": "stock"},
    "NVDA": {"sector": "semiconductors", "region": "US", "economic_currency": "USD", "themes": ["ai_semiconductor", "us_growth"], "instrument_type": "stock"},
    "MU": {"sector": "semiconductors", "region": "US", "economic_currency": "USD", "themes": ["ai_semiconductor", "memory_cycle"], "instrument_type": "stock"},
    "LMT": {"sector": "defense", "region": "US", "economic_currency": "USD", "themes": ["defense_cycle"], "instrument_type": "stock"},
    "003670.KS": {"sector": "battery_materials", "region": "KR", "economic_currency": "KRW", "themes": ["ev_battery"], "instrument_type": "stock"},
    "005380.KS": {"sector": "automobiles", "region": "KR", "economic_currency": "KRW", "themes": ["automobiles", "exporters"], "instrument_type": "stock"},
    "090430.KS": {"sector": "consumer", "region": "KR", "economic_currency": "KRW", "themes": ["consumer_asia"], "instrument_type": "stock"},
    "041510.KQ": {"sector": "entertainment", "region": "KR", "economic_currency": "KRW", "themes": ["k_content"], "instrument_type": "stock"},
    "352820.KS": {"sector": "entertainment", "region": "KR", "economic_currency": "KRW", "themes": ["k_content"], "instrument_type": "stock"},
    "328130.KQ": {"sector": "healthcare_technology", "region": "KR", "economic_currency": "KRW", "themes": ["ai_healthcare"], "instrument_type": "stock"},
    "462870.KS": {"sector": "gaming", "region": "KR", "economic_currency": "KRW", "themes": ["gaming_ip", "k_content"], "instrument_type": "stock"},
    "207940.KS": {"sector": "biopharma", "region": "KR", "economic_currency": "KRW", "themes": ["biopharma"], "instrument_type": "stock"},
    "133690.KS": {"sector": "technology_index", "region": "US", "economic_currency": "USD", "themes": ["us_equity", "us_growth", "large_cap"], "instrument_type": "etf"},
    "360750.KS": {"sector": "broad_market", "region": "US", "economic_currency": "USD", "themes": ["us_equity", "large_cap"], "instrument_type": "etf"},
    "251350.KS": {"sector": "broad_market", "region": "developed_global", "economic_currency": "FOREIGN", "themes": ["global_equity", "large_cap"], "instrument_type": "etf"},
    "192090.KS": {"sector": "broad_market", "region": "CN", "economic_currency": "CNY", "themes": ["china_equity"], "instrument_type": "etf"},
    "069500.KS": {"sector": "broad_market", "region": "KR", "economic_currency": "KRW", "themes": ["korea_equity", "large_cap"], "instrument_type": "etf"},
    "229200.KS": {"sector": "broad_market", "region": "KR", "economic_currency": "KRW", "themes": ["korea_equity", "korea_growth"], "instrument_type": "etf"},
    "091160.KS": {"sector": "semiconductors", "region": "KR", "economic_currency": "KRW", "themes": ["ai_semiconductor", "memory_cycle"], "instrument_type": "etf"},
    "161510.KS": {"sector": "dividend_equity", "region": "KR", "economic_currency": "KRW", "themes": ["income"], "instrument_type": "etf"},
    "329200.KS": {"sector": "real_estate", "region": "KR", "economic_currency": "KRW", "themes": ["income", "rate_sensitive"], "instrument_type": "etf"},
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _symbol(value: Any) -> str:
    return str(value or "").upper().strip()


def _pct(value: float, total: float) -> float:
    return round(value / total * 100.0, 2) if total > 0 else 0.0


def _level(pct: float, thresholds: tuple[float, float]) -> str:
    warning, critical = thresholds
    if pct >= critical:
        return "critical"
    if pct >= warning:
        return "warning"
    return "ok"


def _fallback_taxonomy(symbol: str, name: str, quote_currency: str) -> dict[str, Any]:
    upper_name = str(name or "").upper()
    is_kr = symbol.endswith((".KS", ".KQ"))
    is_etf = any(token in upper_name for token in ("TIGER", "KODEX", "PLUS", "ETF"))
    region = "KR" if is_kr else "US"
    economic_currency = quote_currency or ("KRW" if is_kr else "USD")
    sector = "unknown"
    themes: list[str] = []

    if is_etf:
        sector = "broad_market"
        if "S&P" in upper_name or "나스닥" in name or "NASDAQ" in upper_name:
            region, economic_currency = "US", "USD"
            themes.append("us_equity")
        elif "반도체" in name:
            sector = "semiconductors"
            themes.extend(["ai_semiconductor", "memory_cycle"])
        elif "200" in upper_name or "코스닥" in name:
            themes.append("korea_equity")
    return {
        "sector": sector,
        "region": region,
        "economic_currency": economic_currency,
        "themes": themes,
        "instrument_type": "etf" if is_etf else "stock",
        "taxonomy_source": "heuristic",
    }


def normalize_positions(
    portfolio: Mapping[str, Any] | None,
    taxonomy: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """dashboard portfolio payload를 종목 단위로 합산한다."""
    portfolio = deepcopy(dict(portfolio or {}))
    taxonomy_map = {**DEFAULT_TAXONOMY, **dict(taxonomy or {})}
    aggregated: dict[str, dict[str, Any]] = {}

    for account in portfolio.get("accounts") or []:
        if not isinstance(account, Mapping):
            continue
        account_name = str(account.get("name") or "unknown")
        for row in account.get("items") or []:
            if not isinstance(row, Mapping):
                continue
            symbol = _symbol(row.get("ticker") or row.get("symbol"))
            value = _num(row.get("eval_krw") or row.get("market_value_krw"))
            if not symbol or value <= 0:
                continue
            current = aggregated.setdefault(symbol, {
                "symbol": symbol,
                "name": str(row.get("name") or symbol),
                "eval_krw": 0.0,
                "quote_currency": str(row.get("currency") or ""),
                "accounts": set(),
            })
            current["eval_krw"] += value
            current["accounts"].add(account_name)
            if current["name"] == symbol and row.get("name"):
                current["name"] = str(row.get("name"))

    holdings_total = sum(row["eval_krw"] for row in aggregated.values())
    total_asset = _num(portfolio.get("total_asset") or portfolio.get("total_eval"))
    if total_asset < holdings_total:
        total_asset = holdings_total
    total_cash = max(0.0, _num(portfolio.get("total_cash"), total_asset - holdings_total))
    if total_asset <= 0:
        total_asset = holdings_total + total_cash

    positions: list[dict[str, Any]] = []
    classified_value = 0.0
    for symbol, row in aggregated.items():
        tax = dict(taxonomy_map.get(symbol) or _fallback_taxonomy(
            symbol, row["name"], row["quote_currency"]))
        tax.setdefault("taxonomy_source", "explicit" if symbol in taxonomy_map else "heuristic")
        sector = str(tax.get("sector") or "unknown")
        if sector != "unknown":
            classified_value += row["eval_krw"]
        positions.append({
            "symbol": symbol,
            "name": row["name"],
            "eval_krw": round(row["eval_krw"]),
            "invested_weight_pct": _pct(row["eval_krw"], holdings_total),
            "asset_weight_pct": _pct(row["eval_krw"], total_asset),
            "quote_currency": row["quote_currency"],
            "accounts": sorted(row["accounts"]),
            "sector": sector,
            "region": str(tax.get("region") or "unknown"),
            "economic_currency": str(tax.get("economic_currency") or "unknown"),
            "themes": sorted({str(x) for x in (tax.get("themes") or []) if str(x)}),
            "instrument_type": str(tax.get("instrument_type") or "unknown"),
            "taxonomy_source": str(tax.get("taxonomy_source") or "unknown"),
        })
    positions.sort(key=lambda row: row["eval_krw"], reverse=True)
    return {
        "positions": positions,
        "holdings_eval_krw": round(holdings_total),
        "total_asset_krw": round(total_asset),
        "cash_krw": round(total_cash),
        "cash_weight_pct": _pct(total_cash, total_asset),
        "taxonomy_coverage_pct": _pct(classified_value, holdings_total),
    }


def _aggregate_dimension(
    positions: list[dict[str, Any]], dimension: str, holdings_total: float,
    *, multi: bool = False,
) -> list[dict[str, Any]]:
    values: dict[str, float] = defaultdict(float)
    symbols: dict[str, list[str]] = defaultdict(list)
    for row in positions:
        keys = row.get(dimension) if multi else [row.get(dimension)]
        for raw_key in keys or []:
            key = str(raw_key or "unknown")
            values[key] += _num(row.get("eval_krw"))
            symbols[key].append(row["symbol"])
    return sorted(({
        "key": key,
        "eval_krw": round(value),
        "invested_weight_pct": _pct(value, holdings_total),
        "symbols": sorted(set(symbols[key])),
        "position_count": len(set(symbols[key])),
        "non_additive": multi,
    } for key, value in values.items()), key=lambda row: row["eval_krw"], reverse=True)


def _matrix_value(matrix: Any, a: str, b: str) -> float | None:
    try:
        if hasattr(matrix, "loc"):
            value = matrix.loc[a, b]
        else:
            value = matrix[a][b]
        return float(value)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def build_correlation_clusters(
    positions: list[dict[str, Any]], correlation_matrix: Any,
    *, threshold: float = 0.75,
) -> tuple[list[dict[str, Any]], float]:
    """양의 상관관계 연결요소를 군집화한다. 음의 상관은 분산효과라 묶지 않는다."""
    symbols = [row["symbol"] for row in positions]
    values = {row["symbol"]: _num(row["eval_krw"]) for row in positions}
    total = sum(values.values())
    parent = {symbol: symbol for symbol in symbols}
    observed: set[str] = set()
    edges: list[tuple[str, str, float]] = []

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for idx, a in enumerate(symbols):
        for b in symbols[idx + 1:]:
            corr = _matrix_value(correlation_matrix, a, b)
            if corr is None:
                continue
            observed.update((a, b))
            if corr >= threshold:
                union(a, b)
                edges.append((a, b, corr))

    groups: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols:
        groups[find(symbol)].append(symbol)
    clusters: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        group_edges = [edge for edge in edges if edge[0] in member_set and edge[1] in member_set]
        if not group_edges:
            continue
        value = sum(values[symbol] for symbol in members)
        clusters.append({
            "symbols": sorted(members),
            "position_count": len(members),
            "eval_krw": round(value),
            "invested_weight_pct": _pct(value, total),
            "average_link_correlation": round(sum(edge[2] for edge in group_edges) / len(group_edges), 3),
            "edge_count": len(group_edges),
            "threshold": threshold,
        })
    clusters.sort(key=lambda row: row["eval_krw"], reverse=True)
    observed_value = sum(values[symbol] for symbol in observed)
    return clusters, _pct(observed_value, total)


def fetch_price_correlation_matrix(
    symbols: list[str] | tuple[str, ...] | set[str],
    *,
    period: str = "6mo",
    min_points: int = 40,
) -> tuple[Any, dict[str, Any]]:
    """yfinance 배치 GET으로 일별 수익률 상관행렬을 만든다.

    dashboard GET 경로에서는 호출하지 않는다. Hermes 주기 작업이나 수동 분석이
    명시적으로 사용한다. 실패 시 빈 DataFrame과 data-quality 메타를 반환한다.
    """
    import pandas as pd
    import yfinance as yf

    requested = sorted({_symbol(value) for value in symbols if _symbol(value)})
    metadata = {
        "source": "yfinance_batch_history",
        "period": period,
        "min_points": int(min_points),
        "requested_symbols": requested,
        "available_symbols": [],
        "missing_symbols": list(requested),
        "return_points": 0,
        "status": "unavailable",
    }
    if len(requested) < 2:
        metadata["status"] = "insufficient_symbols"
        return pd.DataFrame(), metadata
    try:
        data = yf.download(
            requested, period=period, auto_adjust=True, progress=False,
            threads=True, group_by="column",
        )
        if data is None or getattr(data, "empty", True):
            return pd.DataFrame(), metadata
        close = data["Close"] if "Close" in data else pd.DataFrame()
        if isinstance(close, pd.Series):
            close = close.to_frame(name=requested[0])
        if not isinstance(close, pd.DataFrame):
            return pd.DataFrame(), metadata
        close.columns = [str(column).upper() for column in close.columns]
        available = [str(column) for column in close.columns
                     if len(close[str(column)].dropna()) >= int(min_points)]
        if len(available) < 2:
            metadata["available_symbols"] = sorted(available)
            metadata["missing_symbols"] = sorted(set(requested) - set(available))
            metadata["status"] = "insufficient_history"
            return pd.DataFrame(), metadata
        returns: pd.DataFrame = pd.DataFrame(close.loc[:, available]).pct_change(
            fill_method=None).dropna(how="all")
        matrix: pd.DataFrame = returns.corr(min_periods=int(min_points))
        metadata.update({
            "available_symbols": sorted(available),
            "missing_symbols": sorted(set(requested) - set(available)),
            "return_points": int(len(returns)),
            "status": "ok" if not matrix.empty else "insufficient_history",
        })
        return matrix, metadata
    except Exception as exc:
        metadata["status"] = f"error:{type(exc).__name__}"
        return pd.DataFrame(), metadata


def format_cluster_risk_summary(report: Mapping[str, Any] | None) -> str:
    """Hermes용 결정론 요약. 매수·매도·주문 지시는 생성하지 않는다."""
    source: Mapping[str, Any] = report if isinstance(report, Mapping) else {}
    summary_raw = source.get("summary")
    quality_raw = source.get("data_quality")
    summary: Mapping[str, Any] = summary_raw if isinstance(summary_raw, Mapping) else {}
    quality: Mapping[str, Any] = quality_raw if isinstance(quality_raw, Mapping) else {}
    lines = [
        f"포트폴리오 군집 위험: {source.get('overall_risk', 'unknown')}",
        (
            f"투자자산 {int(_num(summary.get('holdings_eval_krw'))):,}원 · "
            f"현금비중 {_num(summary.get('cash_weight_pct')):.1f}% · "
            f"보유 {int(_num(summary.get('position_count')))}종목"
        ),
    ]
    for alert in list(source.get("alerts") or [])[:8]:
        if not isinstance(alert, Mapping):
            continue
        lines.append(
            f"- [{str(alert.get('severity') or '').upper()}] "
            f"{alert.get('message') or alert.get('key')} · "
            f"{', '.join(str(x) for x in (alert.get('symbols') or []))}"
        )
    lines.append(
        f"데이터: taxonomy {quality.get('taxonomy_coverage_pct', 0)}% · "
        f"correlation {quality.get('correlation_status', 'unknown')} "
        f"({quality.get('correlation_coverage_pct', 0)}%)"
    )
    lines.append("해석 전용 · 자동매도/주문 권한 없음")
    return "\n".join(lines)


def calculate_portfolio_cluster_risk(
    portfolio: Mapping[str, Any] | None,
    *,
    taxonomy: Mapping[str, Mapping[str, Any]] | None = None,
    correlation_matrix: Any = None,
    thresholds: Mapping[str, tuple[float, float]] | None = None,
    correlation_threshold: float = 0.75,
) -> dict[str, Any]:
    """군집 위험 JSON을 생성한다. 모든 결과는 read-only 진단이다."""
    limits = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    normalized = normalize_positions(portfolio, taxonomy)
    positions = normalized["positions"]
    holdings_total = _num(normalized["holdings_eval_krw"])

    clusters = {
        "sector": _aggregate_dimension(positions, "sector", holdings_total),
        "theme": _aggregate_dimension(positions, "themes", holdings_total, multi=True),
        "region": _aggregate_dimension(positions, "region", holdings_total),
        "economic_currency": _aggregate_dimension(positions, "economic_currency", holdings_total),
    }
    correlation_clusters: list[dict[str, Any]] = []
    correlation_coverage = 0.0
    if correlation_matrix is not None:
        correlation_clusters, correlation_coverage = build_correlation_clusters(
            positions, correlation_matrix, threshold=correlation_threshold)

    alerts: list[dict[str, Any]] = []
    for row in positions:
        position_limit = (
            limits["position_etf"]
            if row.get("instrument_type") == "etf"
            else limits["position"]
        )
        severity = _level(row["invested_weight_pct"], position_limit)
        if severity != "ok":
            alerts.append({
                "type": "position_concentration", "dimension": "position",
                "key": row["symbol"], "severity": severity,
                "invested_weight_pct": row["invested_weight_pct"],
                "symbols": [row["symbol"]],
                "message": f"단일 종목 {row['symbol']} 비중 {row['invested_weight_pct']:.1f}%",
            })

    for dimension, rows in clusters.items():
        limit_key = dimension
        for row in rows:
            severity = _level(row["invested_weight_pct"], limits[limit_key])
            if severity != "ok":
                alerts.append({
                    "type": "cluster_concentration", "dimension": dimension,
                    "key": row["key"], "severity": severity,
                    "invested_weight_pct": row["invested_weight_pct"],
                    "symbols": row["symbols"],
                    "message": f"{dimension} 군집 {row['key']} 비중 {row['invested_weight_pct']:.1f}%",
                })

    for idx, row in enumerate(correlation_clusters, start=1):
        severity = _level(row["invested_weight_pct"], limits["correlation_cluster"])
        row["cluster_id"] = f"corr_{idx}"
        row["severity"] = severity
        if severity != "ok":
            alerts.append({
                "type": "correlation_cluster", "dimension": "correlation",
                "key": row["cluster_id"], "severity": severity,
                "invested_weight_pct": row["invested_weight_pct"],
                "symbols": row["symbols"],
                "message": f"가격 상관 군집 비중 {row['invested_weight_pct']:.1f}%",
            })

    unknown_pct = round(100.0 - normalized["taxonomy_coverage_pct"], 2) if holdings_total else 0.0
    unknown_severity = _level(unknown_pct, limits["unknown"])
    if unknown_severity != "ok":
        alerts.append({
            "type": "data_quality", "dimension": "taxonomy", "key": "unknown",
            "severity": unknown_severity, "invested_weight_pct": unknown_pct,
            "symbols": [row["symbol"] for row in positions if row["sector"] == "unknown"],
            "message": f"미분류 투자자산 비중 {unknown_pct:.1f}%",
        })

    severity_rank = {"critical": 0, "warning": 1, "ok": 2}
    alerts.sort(key=lambda row: (severity_rank[row["severity"]], -row["invested_weight_pct"], row["key"]))
    critical_count = sum(1 for row in alerts if row["severity"] == "critical")
    warning_count = sum(1 for row in alerts if row["severity"] == "warning")
    if critical_count:
        overall = "critical"
    elif warning_count >= 3:
        overall = "high"
    elif warning_count >= 1:
        overall = "moderate"
    else:
        overall = "low"

    return {
        "version": VERSION,
        "read_only": True,
        "order_side_effects": False,
        "overall_risk": overall,
        "summary": {
            "position_count": len(positions),
            "holdings_eval_krw": normalized["holdings_eval_krw"],
            "total_asset_krw": normalized["total_asset_krw"],
            "cash_krw": normalized["cash_krw"],
            "cash_weight_pct": normalized["cash_weight_pct"],
            "critical_alert_count": critical_count,
            "warning_alert_count": warning_count,
        },
        "data_quality": {
            "taxonomy_coverage_pct": normalized["taxonomy_coverage_pct"],
            "unknown_invested_weight_pct": unknown_pct,
            "correlation_status": "available" if correlation_matrix is not None else "not_requested",
            "correlation_coverage_pct": correlation_coverage,
            "theme_weights_non_additive": True,
            "etf_lookthrough_mode": "representative_risk_tags_only",
        },
        "thresholds": {key: {"warning": value[0], "critical": value[1]} for key, value in limits.items()},
        "positions": positions,
        "clusters": clusters,
        "correlation_clusters": correlation_clusters,
        "alerts": alerts,
    }


def hermes_interpretation_payload(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """Hermes가 창작 없이 해석할 최소 사실 묶음."""
    report = report if isinstance(report, Mapping) else {}
    alerts = list(report.get("alerts") or [])
    return {
        "version": "portfolio_cluster_risk_interpretation_v1",
        "read_only": True,
        "overall_risk": report.get("overall_risk", "unknown"),
        "summary": dict(report.get("summary") or {}),
        "top_alerts": alerts[:8],
        "data_quality": dict(report.get("data_quality") or {}),
        "interpretation_rules": [
            "수치와 종목 목록을 재계산하거나 창작하지 않는다",
            "critical을 먼저, warning을 다음 순서로 설명한다",
            "분산 검토와 신규 노출 제한을 제안할 수 있으나 자동매도·주문을 지시하지 않는다",
            "테마 비중은 중복 합산 가능하며 합계 100%로 해석하지 않는다",
            "상관 데이터가 없으면 상관 위험을 단정하지 않는다",
        ],
    }
