"""
읽기 전용 웹 대시보드 (FastAPI).

조회 전용 — 주문 실행/DB 수정 엔드포인트 없음. 기본 127.0.0.1:8787 바인드.
외부 공개 금지(SSH 터널 전제). DASHBOARD_HOST/DASHBOARD_PORT로 오버라이드.

실행:
  python -m web.app
  python main.py dashboard
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from core import dashboard_data as dd

app = FastAPI(title="Sanjuk Dashboard", docs_url=None, redoc_url=None)


# ─── API (전부 읽기 전용 GET) ──────────────────────────
@app.get("/api/health")
def api_health():
    return JSONResponse(dd.health())


@app.get("/api/status")
def api_status():
    return JSONResponse(dd.system_status())


@app.get("/api/predictions")
def api_predictions():
    return JSONResponse({
        "recent": dd.recent_predictions(20),
        "open": dd.open_predictions(50),
        "closed": dd.closed_summary(30),
    })


@app.get("/api/accuracy")
def api_accuracy():
    return JSONResponse({"by_ticker": dd.accuracy_by_ticker()})


# ─── HTML 대시보드 ─────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


HTML_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>산적 대시보드</title>
<style>
  :root { --bg:#0f1115; --card:#1a1e27; --line:#2a2f3a; --txt:#e6e9ef;
          --muted:#8b93a7; --green:#3fb950; --red:#f85149; --amber:#d29922; --blue:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         font-size:14px; line-height:1.5; padding:12px; padding-bottom:40px; }
  h1 { font-size:18px; margin:4px 0 12px; }
  h2 { font-size:14px; margin:0 0 8px; color:var(--blue); }
  .muted { color:var(--muted); font-size:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:12px; margin-bottom:12px; }
  .row { display:flex; justify-content:space-between; gap:8px; padding:3px 0;
         border-bottom:1px solid var(--line); }
  .row:last-child { border-bottom:none; }
  .badge { display:inline-block; padding:1px 7px; border-radius:6px; font-size:11px; font-weight:600; }
  .b-buy { background:rgba(63,185,80,.15); color:var(--green); }
  .b-sell { background:rgba(248,81,73,.15); color:var(--red); }
  .b-hold { background:rgba(210,153,34,.15); color:var(--amber); }
  .b-cond { background:rgba(88,166,255,.15); color:var(--blue); }
  .b-watch { background:rgba(139,147,167,.15); color:var(--muted); }
  .pill { display:inline-block; padding:2px 8px; margin:2px 4px 2px 0; border-radius:12px;
          background:#222733; font-size:12px; }
  .win { color:var(--green); } .loss { color:var(--red); } .neu { color:var(--muted); }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .stat { text-align:center; padding:8px; background:#222733; border-radius:8px; }
  .stat .v { font-size:20px; font-weight:700; }
  .stat .l { font-size:11px; color:var(--muted); }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  td,th { padding:4px 6px; text-align:left; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; }
  .small { font-size:12px; }
  .ok { color:var(--green); } .bad { color:var(--red); }
</style>
</head>
<body>
<h1>🥷 산적 주식 대시보드 <span class="muted" id="clock"></span></h1>
<div class="muted" style="margin-bottom:10px">읽기 전용 · 30초 자동 새로고침</div>

<div class="card"><h2>📡 시스템 상태</h2><div id="status">로딩…</div></div>
<div class="card"><h2>🔔 최근 브리핑 액션</h2><div id="latest">로딩…</div></div>
<div class="card"><h2>📋 최근 추천 20</h2><div id="recent">로딩…</div></div>
<div class="card"><h2>⏳ 미결(open) 예측</h2><div id="open">로딩…</div></div>
<div class="card"><h2>📊 최근 종료 결과</h2><div id="closed">로딩…</div></div>
<div class="card"><h2>🎯 종목별 적중률</h2><div id="accuracy">로딩…</div></div>

<script>
const esc = s => (s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function sigBadge(s, at){
  const t = at||s||"";
  let cls="b-watch";
  if(t.includes("NEW_BUY")||t.includes("ADD_BUY")||s==="매수") cls="b-buy";
  else if(t.includes("CONDITIONAL")) cls="b-cond";
  else if(t==="AI_SELL_MANAGEMENT"||s==="매도") cls="b-sell";
  else if(t.includes("HOLD")||t.includes("CANCEL")) cls="b-hold";
  return `<span class="badge ${cls}">${esc(at||s)}</span>`;
}
async function j(u){ try{ const r=await fetch(u); return await r.json(); }catch(e){ return null; } }

async function load(){
  document.getElementById("clock").textContent = new Date().toLocaleTimeString("ko-KR");

  const st = await j("/api/status");
  if(st){
    const db=st.db||{}, sv=st.service||{};
    const svcCls = sv.active==="active" ? "ok":"bad";
    document.getElementById("status").innerHTML =
      `<div class="row"><span>서비스 stock-bot</span><span class="${svcCls}">${esc(sv.active)} / ${esc(sv.sub)}</span></div>`+
      `<div class="row"><span>DB</span><span>${db.db_exists?"연결":"없음"}</span></div>`+
      `<div class="grid" style="margin-top:8px">`+
        `<div class="stat"><div class="v">${db.predictions||0}</div><div class="l">전체 추천</div></div>`+
        `<div class="stat"><div class="v">${db.open||0}</div><div class="l">미결</div></div>`+
        `<div class="stat"><div class="v">${db.v1||0}</div><div class="l">v1</div></div>`+
        `<div class="stat"><div class="v small">${esc((db.last_created||"").slice(5,16))}</div><div class="l">최근 추천</div></div>`+
      `</div>`+
      `<div class="muted" style="margin-top:6px">서버시각 ${esc(st.now)}</div>`;

    const lb=st.latest_briefing||{};
    let bt=Object.entries(lb.by_type||{}).map(([k,v])=>`<span class="pill">${esc(k)}: ${v}</span>`).join("");
    let rows=(lb.rows||[]).map(r=>
      `<div class="row"><span>${sigBadge(r.signal,r.action_type)} ${esc(r.name)} <span class="muted">${esc(r.account_type)}</span></span>`+
      `<span class="muted">${esc(r.entry_price)} ${esc(r.normalizer_version)}</span></div>`).join("");
    document.getElementById("latest").innerHTML =
      (lb.day?`<div class="muted">${esc(lb.day)}</div>${bt}<div style="margin-top:6px">${rows||"<span class='muted'>없음</span>"}</div>`:"<span class='muted'>브리핑 없음</span>");
  }

  const pr = await j("/api/predictions");
  if(pr){
    document.getElementById("recent").innerHTML = (pr.recent&&pr.recent.length)?
      pr.recent.map(r=>`<div class="row"><span>${sigBadge(r.signal,r.action_type)} ${esc(r.name)}</span>`+
        `<span class="muted">${esc((r.created_at||"").slice(5,16))} ${esc(r.status)}</span></div>`).join("")
      : "<span class='muted'>추천 없음</span>";
    document.getElementById("open").innerHTML = (pr.open&&pr.open.length)?
      pr.open.map(r=>`<div class="row"><span>${sigBadge(r.signal,r.action_type)} ${esc(r.name)} <span class="muted">${esc(r.account_type)}</span></span>`+
        `<span class="muted">진입 ${esc(r.entry_price)} → 목표 ${esc(r.target_price)}</span></div>`).join("")
      : "<span class='muted'>미결 예측 없음</span>";
    const c=pr.closed||{};
    let recent=(c.recent||[]).map(r=>{
      const cls=r.outcome==="win"?"win":r.outcome==="loss"?"loss":"neu";
      const icon=r.outcome==="win"?"✅":r.outcome==="loss"?"❌":"➖";
      return `<div class="row"><span>${icon} ${esc(r.name)} ${esc(r.signal)}</span><span class="${cls}">${(r.pnl_pct>=0?"+":"")}${esc(r.pnl_pct)}%</span></div>`;
    }).join("");
    document.getElementById("closed").innerHTML =
      `<div class="grid"><div class="stat"><div class="v ok">${c.win||0}</div><div class="l">승</div></div>`+
      `<div class="stat"><div class="v bad">${c.loss||0}</div><div class="l">패</div></div></div>`+
      `<div class="muted" style="margin:6px 0">30일 평균 ${esc(c.avg_pnl)}% · 무승부 ${c.neutral||0}</div>`+
      (recent||"<span class='muted'>종료 없음</span>");
  }

  const ac = await j("/api/accuracy");
  if(ac && ac.by_ticker){
    document.getElementById("accuracy").innerHTML = ac.by_ticker.length?
      `<table><tr><th>종목</th><th>평가</th><th>승률</th><th>평균</th><th>PF</th></tr>`+
      ac.by_ticker.map(r=>{
        const wr=Math.round(r.win_rate||0);
        const cls=wr>=60?"ok":wr<40?"bad":"";
        return `<tr><td>${esc(r.ticker)}</td><td>${r.evaluated_count}</td>`+
          `<td class="${cls}">${wr}%</td><td>${(r.avg_pnl>=0?"+":"")}${esc(r.avg_pnl)}%</td>`+
          `<td>${esc(r.profit_factor)}</td></tr>`;
      }).join("")+`</table>`
      : "<span class='muted'>평가 데이터 없음</span>";
  }
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""


def run():
    """대시보드 서버 실행 (main.py dashboard / python -m web.app)."""
    import uvicorn
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))
    print(f"📊 대시보드 (읽기 전용): http://{host}:{port}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"⚠️ 외부 바인드({host}) — SSH 터널 권장, 방화벽 확인 필요")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
