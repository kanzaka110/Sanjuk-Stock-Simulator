"""오프라인 캘리브레이션/신뢰도 측정 — 실주문 경로 미변경 (read-only 분석).

`quality_gate_decisions`(실현 outcome 평가된 결정)로 "결정 점수가 실제 승률과
맞는가"를 측정한다. 점수가 높을수록 실현 승률이 높아야(단조·양의 상관) 캘리브레이션 OK.

NOTE: win_prob 자체는 결정 시점에만 계산되고 DB에 저장되지 않는다
(quality_gate_decisions에 win_prob 컬럼 없음). 따라서 여기서는 저장된
score_total/decision_bucket ↔ 실현 outcome 캘리브레이션을 측정한다.
진짜 win_prob 캘리브레이션을 하려면 결정 시 win_prob을 함께 저장해야 한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from statistics import mean

WIN = "win"
LOSS = "loss"
EXPIRED = "expired"


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "db" / "data" / "toss_quality_gate.db"


def load_evaluated_decisions(
    db_path: str | Path | None = None, include_expired: bool = False
) -> list[dict]:
    """실현 outcome이 평가된 결정 행을 read-only로 로드.

    include_expired=False → win/loss만 (승률 계산 대상).
    """
    path = str(db_path or default_db_path())
    outcomes = (WIN, LOSS) if not include_expired else (WIN, LOSS, EXPIRED)
    placeholders = ",".join("?" * len(outcomes))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(quality_gate_decisions)")}
        base = "ticker, side, decision_bucket, score_total, outcome, return_3d, return_5d, decided_at"
        # win_prob/win_prob_candidate는 봇 재시작 후에야 생기므로 존재할 때만 선택.
        optional = [c for c in ("win_prob", "win_prob_candidate") if c in cols]
        select = base + "".join(f", {c}" for c in optional)
        cur = con.execute(
            f"SELECT {select} FROM quality_gate_decisions WHERE outcome IN ({placeholders})",
            outcomes,
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r.setdefault("win_prob", None)
            r.setdefault("win_prob_candidate", None)
        return rows
    finally:
        con.close()


def _win01(outcome: str) -> float:
    return 1.0 if outcome == WIN else 0.0


def _rankdata(a: list[float]) -> list[float]:
    """동점 평균순위 랭크."""
    order = sorted(range(len(a)), key=lambda i: a[i])
    ranks = [0.0] * len(a)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    """스피어만 순위상관 (scipy 불필요)."""
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    rx = _rankdata(xs)
    ry = _rankdata(ys)
    n = len(rx)
    mx = mean(rx)
    my = mean(ry)
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((v - mx) ** 2 for v in rx)
    vy = sum((v - my) ** 2 for v in ry)
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx**0.5 * vy**0.5)


def score_reliability(
    rows: list[dict], score_field: str = "score_total", n_buckets: int = 5
) -> list[dict]:
    """점수 분위 버킷별 실현 승률/평균수익 신뢰도표."""
    vals = [
        (r[score_field], _win01(r["outcome"]), r)
        for r in rows
        if r.get(score_field) is not None and r.get("outcome") in (WIN, LOSS)
    ]
    vals.sort(key=lambda t: t[0])
    n = len(vals)
    if n == 0:
        return []

    n_buckets = max(1, min(n_buckets, n))
    size = max(1, n // n_buckets)
    buckets: list[dict] = []
    for b in range(n_buckets):
        lo = b * size
        hi = n if b == n_buckets - 1 else (b + 1) * size
        chunk = vals[lo:hi]
        if not chunk:
            continue
        r3 = [r.get("return_3d") for _, _, r in chunk if r.get("return_3d") is not None]
        buckets.append(
            {
                "bucket": b + 1,
                "n": len(chunk),
                "score_min": round(float(chunk[0][0]), 2),
                "score_max": round(float(chunk[-1][0]), 2),
                "win_rate": mean(w for _, w, _ in chunk),
                "mean_return_3d": (round(mean(r3), 3) if r3 else None),
            }
        )
    return buckets


def calibration_summary(rows: list[dict], score_field: str = "score_total") -> dict:
    """점수↔실현결과 캘리브레이션 요약 (판별력/단조성/기저승률)."""
    scored = [
        (float(r[score_field]), _win01(r["outcome"]))
        for r in rows
        if r.get(score_field) is not None and r.get("outcome") in (WIN, LOSS)
    ]
    if not scored:
        return {"n": 0}

    xs = [s for s, _ in scored]
    ys = [w for _, w in scored]
    buckets = score_reliability(rows, score_field)
    spread = (buckets[-1]["win_rate"] - buckets[0]["win_rate"]) if len(buckets) >= 2 else 0.0
    monotonic = all(
        buckets[i]["win_rate"] <= buckets[i + 1]["win_rate"] + 1e-9
        for i in range(len(buckets) - 1)
    )
    return {
        "n": len(scored),
        "base_win_rate": mean(ys),
        "spearman_score_win": spearman(xs, ys),
        "top_minus_bottom_win_rate": spread,
        "monotonic": monotonic,
        "buckets": buckets,
    }


def brier_score(rows: list[dict], field: str = "win_prob") -> float | None:
    """예측 win_prob(field)의 Brier 점수 (낮을수록 좋음). 없으면 None."""
    pairs = [
        (float(r[field]), _win01(r["outcome"]))
        for r in rows
        if r.get(field) is not None and r.get("outcome") in (WIN, LOSS)
    ]
    if not pairs:
        return None
    return mean((p - o) ** 2 for p, o in pairs)


def compare_win_prob_calibration(rows: list[dict]) -> dict:
    """live win_prob vs 후보(win_prob_candidate) 캘리브레이션 비교."""
    return {
        "current": win_prob_calibration(rows, field="win_prob"),
        "candidate": win_prob_calibration(rows, field="win_prob_candidate"),
    }


def win_prob_calibration(rows: list[dict], n_buckets: int = 5, field: str = "win_prob") -> dict:
    """예측 win_prob ↔ 실현 승률 캘리브레이션 (Brier/ECE/신뢰도 버킷).

    win_prob이 아직 기록 안 됐으면 {"n": 0, "available": False}.
    """
    pairs = [
        (float(r[field]), _win01(r["outcome"]))
        for r in rows
        if r.get(field) is not None and r.get("outcome") in (WIN, LOSS)
    ]
    if not pairs:
        return {"n": 0, "available": False}

    pairs.sort(key=lambda t: t[0])
    n = len(pairs)
    n_buckets = max(1, min(n_buckets, n))
    size = max(1, n // n_buckets)
    buckets: list[dict] = []
    ece = 0.0
    for b in range(n_buckets):
        lo = b * size
        hi = n if b == n_buckets - 1 else (b + 1) * size
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        pred = mean(p for p, _ in chunk)
        actual = mean(o for _, o in chunk)
        buckets.append(
            {
                "bucket": b + 1,
                "n": len(chunk),
                "pred_win_prob": round(pred, 4),
                "actual_win_rate": round(actual, 4),
                "gap": round(pred - actual, 4),
            }
        )
        ece += (len(chunk) / n) * abs(pred - actual)

    return {
        "n": n,
        "available": True,
        "brier": brier_score(rows, field=field),
        "ece": ece,  # expected calibration error (낮을수록 예측=실제)
        "mean_pred": mean(p for p, _ in pairs),
        "base_win_rate": mean(o for _, o in pairs),
        "buckets": buckets,
    }


def win_prob_calibration_text(cal: dict) -> str:
    if not cal or not cal.get("available"):
        return "(win_prob 캘리브레이션: 예측 win_prob 기록 없음 — 봇 재시작 후 결정부터 축적)"
    lines = [
        f"【win_prob 캘리브레이션】 (n={cal['n']})",
        f"  Brier {cal['brier']:.4f} | ECE {cal['ece']:.4f} | "
        f"평균예측 {cal['mean_pred'] * 100:.1f}% vs 실제 {cal['base_win_rate'] * 100:.1f}%",
    ]
    for b in cal["buckets"]:
        lines.append(
            f"    B{b['bucket']} n={b['n']:3d} 예측 {b['pred_win_prob'] * 100:5.1f}% "
            f"실제 {b['actual_win_rate'] * 100:5.1f}% (gap {b['gap'] * 100:+.1f}%p)"
        )
    return "\n".join(lines)


def calibration_text(summary: dict, score_field: str = "score_total") -> str:
    if not summary or summary.get("n", 0) == 0:
        return "(캘리브레이션: 평가된 결정 데이터 없음)"
    lines = [
        f"【점수 캘리브레이션】 {score_field} vs 실현 승률 (n={summary['n']})",
        f"  기저 승률: {summary['base_win_rate'] * 100:.1f}%  |  "
        f"Spearman(점수,승): {summary['spearman_score_win']:+.3f}  |  "
        f"단조성: {'OK' if summary['monotonic'] else '깨짐'}",
        f"  최상위-최하위 버킷 승률차: {summary['top_minus_bottom_win_rate'] * 100:+.1f}%p "
        f"({'판별력 있음' if summary['top_minus_bottom_win_rate'] > 0.05 else '판별력 약함'})",
    ]
    for b in summary["buckets"]:
        ret = f"{b['mean_return_3d']:+.2f}%" if b["mean_return_3d"] is not None else "-"
        lines.append(
            f"    B{b['bucket']} 점수[{b['score_min']:.1f}~{b['score_max']:.1f}] "
            f"n={b['n']:3d} 승률 {b['win_rate'] * 100:5.1f}% 3d수익 {ret}"
        )
    return "\n".join(lines)
