"""Shadow 검증 하네스 — 후보 win_prob/스코어/랭킹을 과거 결정에 적용해
시간순 홀드아웃에서 실현결과와의 상관을 오프라인 측정한다 (read-only).

목적: 실주문 결정 로직을 바꾸기 **전에**, 수정안이 실제로 더 나은지
(승률·실현수익과의 상관, 상위분위 성과) live 없이 검증한다. armed 무관·안전.

사용:
    rows = load_decisions_for_shadow()
    report = run_shadow(rows, {
        "baseline_score": score_ranking,
        "current_ev": ev_ranking(current_winprob),
        "candidate":  ev_ranking(my_new_winprob),
    })
    print(shadow_text(report))
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from statistics import mean
from typing import Callable

from core.calibration_report import LOSS, WIN, _win01, spearman

RankingFn = Callable[[dict], "float | None"]


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "db" / "data" / "toss_quality_gate.db"


def load_decisions_for_shadow(
    db_path: str | Path | None = None, include_expired: bool = False
) -> list[dict]:
    """shadow 평가에 필요한 컬럼을 read-only 로드 (win_prob은 있으면 포함)."""
    path = str(db_path or default_db_path())
    outcomes = (WIN, LOSS) if not include_expired else (WIN, LOSS, "expired")
    ph = ",".join("?" * len(outcomes))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(quality_gate_decisions)")}
        base = ("decided_at, score_total, rr_ratio, entry_price, stop_loss, "
                "target_price, outcome, return_5d")
        select = base + (", win_prob" if "win_prob" in cols else "")
        rows = [
            dict(r)
            for r in con.execute(
                f"SELECT {select} FROM quality_gate_decisions WHERE outcome IN ({ph})",
                outcomes,
            )
        ]
        for r in rows:
            r.setdefault("win_prob", None)
        return rows
    finally:
        con.close()


# ── 랭킹 함수 빌더 ─────────────────────────────────────────
def score_ranking(row: dict) -> float | None:
    """베이스라인: 현재 quality score_total 그대로."""
    v = row.get("score_total")
    return float(v) if v is not None else None


def current_winprob(row: dict) -> float:
    """현재 estimate_win_prob의 score 기반 근사 (baseline EV용)."""
    s = float(row.get("score_total") or 60.0)
    p = 0.52 + max(min(s - 60.0, 30.0), -20.0) * 0.005
    return max(0.42, min(p, 0.72))


def ev_ranking(winprob_fn: Callable[[dict], float]) -> RankingFn:
    """win_prob 함수 → EV 랭킹 함수. EV = wp*upside - (1-wp)*downside."""

    def _fn(row: dict) -> float | None:
        e = float(row.get("entry_price") or 0)
        s = float(row.get("stop_loss") or 0)
        t = float(row.get("target_price") or 0)
        if e <= 0 or s <= 0 or t <= 0:
            return None
        wp = winprob_fn(row)
        up = (t - e) / e
        dn = (e - s) / e
        return wp * up - (1.0 - wp) * dn

    return _fn


# ── 평가 ───────────────────────────────────────────────────
def time_split(rows: list[dict], holdout_frac: float = 0.5) -> tuple[list[dict], list[dict]]:
    """시간순 정렬 후 앞 train / 뒤 holdout 분할."""
    ordered = sorted(rows, key=lambda r: r.get("decided_at") or "")
    k = int(len(ordered) * (1.0 - holdout_frac))
    return ordered[:k], ordered[k:]


def evaluate_ranking(rows: list[dict], ranking_fn: RankingFn) -> dict:
    """랭킹 함수를 rows에 적용해 실현결과와의 상관/상위분위 성과 측정."""
    scored = []
    for r in rows:
        if r.get("outcome") not in (WIN, LOSS):
            continue
        v = ranking_fn(r)
        if v is None:
            continue
        scored.append((float(v), _win01(r["outcome"]), r.get("return_5d")))
    n = len(scored)
    if n == 0:
        return {"n": 0}

    ranks = [s for s, _, _ in scored]
    wins = [w for _, w, _ in scored]
    rho_win = spearman(ranks, wins)

    ret_pairs = [(s, rr) for s, _, rr in scored if rr is not None]
    rho_ret5 = (
        spearman([s for s, _ in ret_pairs], [rr for _, rr in ret_pairs])
        if len(ret_pairs) >= 3
        else 0.0
    )

    order = sorted(scored, key=lambda t: t[0], reverse=True)
    topn = max(1, n // 4)
    top = order[:topn]
    top_ret = [rr for _, _, rr in top if rr is not None]
    return {
        "n": n,
        "rho_win": rho_win,
        "rho_ret5": rho_ret5,
        "top_q_win_rate": mean(w for _, w, _ in top),
        "top_q_ret5": (mean(top_ret) if top_ret else None),
        "base_win_rate": mean(wins),
    }


def fit_ols_ranking(
    train_rows: list[dict], features: list[str], target: str = "return_5d"
) -> RankingFn:
    """train에서 OLS로 피처→타깃(return_5d 또는 'win') 선형모델을 적합하고
    예측값을 랭킹으로 쓰는 함수를 반환한다 (train 통계로 표준화).

    반드시 결정 시점 피처만 사용할 것 (outcome/return을 피처로 넣지 말 것 — 누수).
    후보는 이 함수로 만들어 run_shadow의 holdout에서 검증한다.
    """
    import numpy as np

    def _x(rs: list[dict]) -> "np.ndarray":
        return np.array([[1.0] + [float(r.get(f) or 0) for f in features] for r in rs])

    if target == "win":
        y = np.array([1.0 if r.get("outcome") == WIN else 0.0 for r in train_rows])
    else:
        y = np.array([float(r.get(target) or 0) for r in train_rows])

    xtr = _x(train_rows)
    mu = xtr.mean(0)
    mu[0] = 0.0
    sd = xtr.std(0)
    sd[0] = 1.0
    sd[sd == 0] = 1.0
    beta = np.linalg.lstsq((xtr - mu) / sd, y, rcond=None)[0]

    def _fn(row: dict) -> float | None:
        xv = (_x([row]) - mu) / sd
        return float((xv @ beta)[0])

    return _fn


def run_shadow(
    rows: list[dict], ranking_fns: dict[str, RankingFn], holdout_frac: float = 0.5
) -> dict:
    """시간순 홀드아웃에서 각 랭킹 함수를 A/B 평가."""
    ordered = [r for r in rows if r.get("decided_at")]
    _, test = time_split(ordered, holdout_frac)
    return {
        "n_total": len(ordered),
        "n_test": len(test),
        "holdout_frac": holdout_frac,
        "results": {name: evaluate_ranking(test, fn) for name, fn in ranking_fns.items()},
    }


def shadow_text(report: dict) -> str:
    if not report or not report.get("results"):
        return "(shadow: 데이터 없음)"
    lines = [
        f"【Shadow 홀드아웃 검증】 test n={report['n_test']} "
        f"(holdout {report['holdout_frac'] * 100:.0f}%)",
        f"  {'전략':22s} {'rho(승)':>8s} {'rho(5d수익)':>11s} {'상위25%승률':>10s} {'상위25%수익':>10s}",
    ]
    for name, m in report["results"].items():
        if m.get("n", 0) == 0:
            lines.append(f"  {name:22s} (데이터 없음)")
            continue
        tr = f"{m['top_q_ret5']:+.2f}%" if m["top_q_ret5"] is not None else "-"
        lines.append(
            f"  {name:22s} {m['rho_win']:+8.3f} {m['rho_ret5']:+11.3f} "
            f"{m['top_q_win_rate'] * 100:9.1f}% {tr:>10s}"
        )
    return "\n".join(lines)
