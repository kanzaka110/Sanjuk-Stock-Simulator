/* 액션센터 공용 렌더러 (PC index_pc.html + 모바일 index.html 공용)
 * read-only GET만 사용. 존재하는 컨테이너 ID에만 렌더링:
 *   #ac-banner  홈 긴급 펄스 배너 (urgent>0일 때만 표시)
 *   #ac-now     NOW — HIT/NEAR 카드
 *   #ac-watch   감시 중 — 레벨 거리 테이블
 *   #ac-alerts  알림 이력 / #ac-dart DART 공시 / #ac-ob 호가 / #ac-qg 품질게이트 / #ac-scan 스캐너
 * 색상: 한국식 — 상승/매수=var(--up, 빨강 계열), 하락/매도=var(--dn, 파랑 계열)
 */
(function () {
  "use strict";

  async function acJ(u) {
    try { const r = await fetch(u); if (!r.ok) return null; return await r.json(); }
    catch { return null; }
  }
  const acE = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const acF = n => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const el = id => document.getElementById(id);

  // 펄스 애니메이션 1회 주입
  if (!document.getElementById("ac-style")) {
    const st = document.createElement("style");
    st.id = "ac-style";
    st.textContent = `
@keyframes acPulse{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,.4)}50%{box-shadow:0 0 0 8px rgba(239,68,68,0)}}
#ac-banner{display:none;cursor:pointer;margin-bottom:12px;padding:13px 16px;border-radius:12px;
  background:linear-gradient(120deg,rgba(239,68,68,.20),rgba(239,68,68,.06));border:1px solid rgba(239,68,68,.5);color:#fecaca;
  font-weight:800;font-size:13.5px;line-height:1.4;animation:acPulse 2.2s infinite;align-items:center;gap:10px}
#ac-banner b{color:#fff}
#ac-banner .arr{margin-left:auto;flex-shrink:0;opacity:.7;font-weight:900}
#ac-now{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
#ac-now .ac-emp{grid-column:1/-1}
.ac-card{position:relative;border:1px solid var(--border,#2a3040);border-left-width:3px;border-radius:12px;padding:12px 14px;background:var(--bg2,rgba(255,255,255,.03))}
.ac-card.hit{border-color:rgba(239,68,68,.35);border-left-color:#ef4444;background:linear-gradient(135deg,rgba(239,68,68,.11),rgba(239,68,68,.03))}
.ac-card.near{border-color:rgba(245,158,11,.32);border-left-color:#f59e0b;background:linear-gradient(135deg,rgba(245,158,11,.09),rgba(245,158,11,.02))}
.ac-hd{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.ac-nm{font-weight:800;font-size:13.5px;line-height:1.35;min-width:0}
.ac-nm .tk{opacity:.45;font-size:10.5px;font-weight:600;margin-left:4px}
.ac-nm .acct{opacity:.6;font-size:10.5px;font-weight:700;margin-left:4px}
.ac-price{font-size:17px;font-weight:900;font-variant-numeric:tabular-nums;letter-spacing:-.4px;white-space:nowrap;flex-shrink:0}
.ac-kind{font-size:11px;opacity:.75;margin-top:2px}
.ac-tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:800;margin-right:6px;vertical-align:1px}
.ac-tag.hit{background:rgba(239,68,68,.22);color:#fca5a5}
.ac-tag.near{background:rgba(245,158,11,.2);color:#fbbf24}
.ac-tag.far{background:rgba(148,163,184,.15);color:#94a3b8}
.ac-lv{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px;font-size:11.5px}
.ac-lv .c{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.05);border-radius:8px;padding:6px 9px;margin:0;min-width:116px;font-variant-numeric:tabular-nums}
.ac-lv .c .g{height:3px;border-radius:2px;background:rgba(255,255,255,.09);margin-top:5px;overflow:hidden}
.ac-lv .c .g i{display:block;height:100%;border-radius:2px}
.ac-meta{font-size:10.5px;opacity:.58;margin-top:7px;line-height:1.5}
.ac-tbl{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
.ac-tbl th{font-size:10px;letter-spacing:.3px;opacity:.55;font-weight:800;text-align:left;padding:7px 9px;border-bottom:1px solid var(--border,#2a3040);white-space:nowrap}
.ac-tbl td{padding:7.5px 9px;text-align:left;border-bottom:1px solid var(--border2,rgba(255,255,255,.05));white-space:nowrap}
.ac-tbl tbody tr:last-child td{border-bottom:none}
.ac-tbl tbody tr:hover{background:rgba(255,255,255,.03)}
.ac-tbl .num{text-align:right}
.ac-sec{border:1px solid var(--border2,rgba(255,255,255,.06));border-radius:11px;margin-bottom:9px;background:var(--bg2,rgba(255,255,255,.02))}
.ac-sec:last-child{margin-bottom:0}
.ac-sec summary{cursor:pointer;font-weight:800;font-size:12.5px;padding:11px 13px;list-style:none;display:flex;align-items:center;gap:8px;user-select:none}
.ac-sec summary::-webkit-details-marker{display:none}
.ac-sec summary::before{content:none}
.ac-sec summary::after{content:"▾";margin-left:auto;opacity:.4;transition:transform .16s}
.ac-sec[open] summary::after{transform:rotate(180deg)}
.ac-sec>*:not(summary){padding:0 13px 12px}
.ac-badge{font-size:10px;font-weight:800;padding:1px 8px;border-radius:999px;background:rgba(255,255,255,.08);opacity:.85}
.ac-badge.warn{background:rgba(245,158,11,.18);color:#fbbf24;opacity:1}
.ac-badge.hot{background:rgba(239,68,68,.2);color:#fca5a5;opacity:1}
.ac-emp{opacity:.5;font-size:12px;padding:14px 4px;text-align:center}`;
    document.head.appendChild(st);
  }

  const stTag = st => `<span class="ac-tag ${st === "HIT" ? "hit" : st === "NEAR" ? "near" : "far"}">${st === "HIT" ? "도달" : st === "NEAR" ? "근접" : "감시"}</span>`;
  const ageTxt = s => s == null ? "" : s < 90 ? `${s}초 전` : s < 5400 ? `${Math.round(s / 60)}분 전` : `${Math.round(s / 3600)}시간 전`;
  const srcTxt = it => [it.price_source, ageTxt(it.price_age_sec)].filter(Boolean).join(" · ");
  function setBadge(id, n, tone) {
    const b = el(id); if (!b) return;
    b.textContent = n > 0 ? String(n) : "";
    b.className = "ac-badge" + (n > 0 && tone ? ` ${tone}` : "");
    b.style.display = n > 0 ? "" : "none";
  }

  function lvCell(l) {
    const dir = l.direction === "above" ? "↑" : "↓";
    const dist = Math.abs(l.dist_pct || 0);
    const hit = l.state === "HIT";
    const col = hit ? "#ef4444" : l.state === "NEAR" ? "#f59e0b" : "rgba(148,163,184,.6)";
    const distC = hit ? "color:#fca5a5;font-weight:800" : l.state === "NEAR" ? "color:#fbbf24;font-weight:800" : "opacity:.7";
    // 근접도 게이지: 도달=100%, 10% 이상 멀면 0%
    const gauge = hit ? 100 : Math.max(0, Math.round(100 - Math.min(dist, 10) * 10));
    return `<div class="c">${acE(l.type)} ${dir} <b>${acF(l.level)}</b><br>
      <span style="${distC}">${hit ? "도달" : `${acF(dist)}% 남음`}</span>
      <div class="g"><i style="width:${gauge}%;background:${col}"></i></div></div>`;
  }

  function acCard(it) {
    const cls = it.state === "HIT" ? "hit" : it.state === "NEAR" ? "near" : "";
    const acct = it.account ? `<span class="acct">[${acE(it.account)}]</span>` : "";
    const kind = it.kind === "price_alert" ? "가격 알림" : acE(it.signal || "추천");
    return `<div class="ac-card ${cls}">
      <div class="ac-hd">
        <div class="ac-nm">${stTag(it.state)}${acE(it.name)}${acct}<span class="tk">${acE(it.ticker)}</span>
          <div class="ac-kind">${kind}${it.confidence ? ` · 신뢰 ${it.confidence}%` : ""}</div></div>
        <div class="ac-price">${acF(it.price)}</div>
      </div>
      <div class="ac-lv">${(it.levels || []).map(lvCell).join("")}</div>
      ${it.reason ? `<div class="ac-meta">${acE(it.reason)}</div>` : ""}
      <div class="ac-meta">${srcTxt(it)}</div>
    </div>`;
  }

  function renderActionCenter(d) {
    const banner = el("ac-banner");
    if (banner) {
      const n = (d && d.urgent_count) || 0;
      if (n > 0) {
        banner.style.display = "flex";
        banner.innerHTML = `🚨 지금 주문 검토 <b>${n}건</b>&nbsp;— 목표/손절/진입 레벨 도달·근접<span class="arr">액션 탭 →</span>`;
      } else { banner.style.display = "none"; banner.innerHTML = ""; }
    }
    const now = el("ac-now");
    if (now) {
      const urgent = (d && d.urgent) || [];
      now.innerHTML = urgent.length ? urgent.map(acCard).join("")
        : '<div class="ac-emp">지금 할 것 없음 — 레벨 도달·근접 종목이 없어요.</div>';
      const sub = el("ac-now-sub");
      if (sub) sub.textContent = `도달·근접(±${(d && d.near_threshold_pct) || 2}%) ${urgent.length}건 · ${((d && d.generated_at) || "").slice(11, 16)} 기준`;
    }
    const watch = el("ac-watch");
    if (watch) {
      const all = [...((d && d.urgent) || []), ...((d && d.watching) || [])];
      watch.innerHTML = all.length ? `<div style="overflow-x:auto"><table class="ac-tbl">
        <thead><tr><th>종목</th><th class="num">현재가</th><th>유형</th><th class="num">가까운 레벨</th><th class="num">거리</th><th>상태</th></tr></thead>
        <tbody>${all.map(it => {
          const nl = it.nearest_level || {};
          const dC = nl.state === "HIT" ? "color:#fca5a5;font-weight:800" : nl.state === "NEAR" ? "color:#fbbf24;font-weight:800" : "opacity:.7";
          return `<tr><td><b>${acE(it.name)}</b> <span style="opacity:.45;font-size:10px">${acE(it.ticker)}</span></td>
            <td class="num" style="font-weight:700">${acF(it.price)}</td><td>${it.kind === "price_alert" ? "알림" : "추천"}</td>
            <td class="num">${acE(nl.type || "")} ${acF(nl.level || 0)}</td>
            <td class="num" style="${dC}">${nl.state === "HIT" ? "도달" : acF(Math.abs(nl.dist_pct || 0)) + "%"}</td>
            <td>${stTag(it.state)}</td></tr>`;
        }).join("")}</tbody></table></div>`
        : '<div class="ac-emp">감시 대상 없음 (미결 추천·가격 알림 0건)</div>';
      const sub = el("ac-watch-sub");
      if (sub) sub.textContent = `미결 추천 ${(d && d.open_recommendation_count) || 0}건 + 가격 알림`;
    }
  }

  function renderAlerts(d) {
    const box = el("ac-alerts"); if (!box) return;
    const items = (d && d.items) || [];
    setBadge("ac-alerts-badge", items.length, items.some(a => a.severity === "CRITICAL") ? "hot" : "warn");
    box.innerHTML = items.length ? `<div style="overflow-x:auto"><table class="ac-tbl">
      <thead><tr><th>시각</th><th>종목</th><th>유형</th><th>내용</th><th>전송</th></tr></thead>
      <tbody>${items.slice(0, 30).map(a => `<tr>
        <td style="opacity:.7">${acE((a.created_at || "").slice(5, 16))}</td>
        <td><b>${acE(a.name || a.ticker)}</b></td>
        <td>${acE(a.alert_type || "")}${a.severity === "CRITICAL" ? " 🚨" : ""}</td>
        <td style="white-space:normal;max-width:340px;line-height:1.4">${acE((a.title || "").slice(0, 80))}</td>
        <td>${a.delivered ? "✅" : `<span style="opacity:.6">억제(${acE(a.suppress_reason || "-")})</span>`}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="ac-emp">최근 48시간 긴급 알림 없음</div>';
  }

  function renderDart(d) {
    const box = el("ac-dart"); if (!box) return;
    setBadge("ac-dart-badge", d && d.ok ? ((d.risk || []).length + (d.items || []).length) : 0,
      d && (d.risk || []).length ? "hot" : "");
    if (!d || !d.ok) { box.innerHTML = `<div class="ac-emp">DART 공시 데이터 없음${d && d.reason ? ` (${acE(d.reason)})` : ""}</div>`; return; }
    const risk = (d.risk || []).map(h =>
      `<div class="ac-card ${h.severity === "high" ? "hit" : "near"}">
        <b>${h.severity === "high" ? "🚨" : "⚠️"} ${acE(h.name || h.corp_name)}</b>
        <span style="opacity:.6;font-size:10.5px">${acE(h.stock_code)}</span> — ${acE(h.report_nm)}
        <div class="ac-meta">${acE(h.keyword)} · ${acE(h.rcept_dt)}</div></div>`).join("");
    const normal = (d.items || []).slice(0, 10).map(it =>
      `<tr><td>${acE(it.rcept_dt)}</td><td><b>${acE(it.name || it.corp_name)}</b></td>
       <td style="white-space:normal">${acE(it.report_nm)}</td></tr>`).join("");
    box.innerHTML = (risk || normal)
      ? risk + (normal ? `<div style="overflow-x:auto"><table class="ac-tbl"><thead><tr><th>일자</th><th>종목</th><th>공시</th></tr></thead><tbody>${normal}</tbody></table></div>` : "")
      : '<div class="ac-emp">보유종목 최근 공시 없음</div>';
  }

  function renderOrderbook(d) {
    const box = el("ac-ob"); if (!box) return;
    const items = (d && d.items) || [];
    box.innerHTML = items.length ? `<div style="overflow-x:auto"><table class="ac-tbl">
      <thead><tr><th>종목</th><th class="num">임밸런스</th><th>방향</th><th>유동성</th><th class="num">스프레드</th></tr></thead>
      <tbody>${items.map(o => {
        const c = o.imbalance_pct > 5 ? "color:var(--up,#f6465d)" : o.imbalance_pct < -5 ? "color:var(--dn,#3b82f6)" : "opacity:.7";
        return `<tr><td><b>${acE(o.name)}</b> <span style="opacity:.45;font-size:10px">${acE(o.code)}</span></td>
          <td class="num" style="${c};font-weight:800">${o.imbalance_pct > 0 ? "+" : ""}${acF(o.imbalance_pct)}%</td>
          <td>${acE(o.side)}</td><td>${acE(o.liquidity || "—")}</td><td class="num">${acF(o.spread_pct)}%</td></tr>`;
      }).join("")}</tbody></table></div>`
      : `<div class="ac-emp">호가 데이터 없음${d && d.reason ? ` (${acE(d.reason)})` : ""}</div>`;
  }

  function renderQG(d) {
    const box = el("ac-qg"); if (!box) return;
    if (!d) { box.innerHTML = '<div class="ac-emp">품질게이트 데이터 없음</div>'; return; }
    const total = ["pass_count", "small_pass_count", "wait_count", "watch_count", "chase_block_count", "block_count"]
      .reduce((s, k) => s + (d[k] || 0), 0);
    if (!total) { box.innerHTML = '<div class="ac-emp">오늘 품질게이트 판정 없음</div>'; return; }
    box.innerHTML = `<div class="ac-lv" style="margin-top:0">
      <div class="c">PASS <b style="color:var(--up,#f6465d)">${d.pass_count || 0}</b></div>
      <div class="c">SMALL_PASS <b>${d.small_pass_count || 0}</b></div>
      <div class="c">WAIT <b>${d.wait_count || 0}</b></div>
      <div class="c">WATCH <b>${d.watch_count || 0}</b></div>
      <div class="c">CHASE_BLOCK <b style="color:#fbbf24">${d.chase_block_count || 0}</b></div>
      <div class="c">BLOCK <b style="color:var(--dn,#3b82f6)">${d.block_count || 0}</b></div></div>
      ${d.avg_pass_score ? `<div class="ac-meta">PASS 평균 점수 ${d.avg_pass_score} · 평균 RR ${d.avg_pass_rr || "—"}</div>` : ""}
      ${(d.top_block_reasons || []).length ? `<div class="ac-meta">주요 차단: ${d.top_block_reasons.slice(0, 3).map(acE).join(" / ")}</div>` : ""}
      <div class="ac-meta">게이트 차단 사유는 삼성증권 수동 주문 판단에도 동일 적용.</div>`;
  }

  function renderScan(d) {
    const box = el("ac-scan"); if (!box) return;
    const items = (d && d.items) || [];
    setBadge("ac-scan-badge", items.length, "");
    box.innerHTML = items.length ? `<div style="overflow-x:auto"><table class="ac-tbl">
      <thead><tr><th>종목</th><th class="num">현재가</th><th class="num">등락</th><th class="num">점수</th><th>바이어스</th><th>아이디어</th></tr></thead>
      <tbody>${items.slice(0, 12).map(s => `<tr>
        <td><b>${acE(s.name)}</b> <span style="opacity:.45;font-size:10px">${acE(s.symbol)}</span></td>
        <td class="num">${acF(s.price)}</td>
        <td class="num" style="color:${(s.change_pct || 0) >= 0 ? "var(--up,#f6465d)" : "var(--dn,#3b82f6)"};font-weight:700">${(s.change_pct || 0) >= 0 ? "+" : ""}${acF(s.change_pct)}%</td>
        <td class="num">${s.score ?? "—"}</td><td style="font-size:10.5px">${acE(s.action_bias || "")}</td>
        <td style="white-space:normal;max-width:260px;font-size:11px;line-height:1.4">${acE((s.idea || "").slice(0, 60))}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="ac-emp">스캐너 후보 없음</div>';
  }

  async function loadActionCenter() {
    const d = await acJ("/api/action-center");
    renderActionCenter(d);
  }

  async function loadActionExtras() {
    const [al, dart, ob, qg, sc] = await Promise.all([
      acJ("/api/alerts/history"),
      acJ("/api/dart/disclosures"),
      acJ("/api/orderbook/summary"),
      acJ("/api/quality-report"),
      acJ("/api/market/discovery"),
    ]);
    renderAlerts(al); renderDart(dart); renderOrderbook(ob); renderQG(qg); renderScan(sc);
  }

  window.initActionCenter = function () {
    loadActionCenter();
    loadActionExtras();
    setInterval(loadActionCenter, 60_000);   // NOW/감시는 1분 갱신
    setInterval(loadActionExtras, 300_000);  // 접이식 섹션은 5분 갱신
  };
})();
