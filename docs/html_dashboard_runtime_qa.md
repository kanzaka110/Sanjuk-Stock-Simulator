# HTML Dashboard Runtime QA

## 1. 검증 시각

- **KST**: 2026-06-21 15:03
- **커밋**: `8de1401 docs: add mobile app parity specification`
- **Branch**: master

## 2. 서버 상태

| 항목 | 상태 |
|---|---|
| Dashboard process | `pid 3815777` — `./venv/bin/python main.py dashboard` (실행 중) |
| Port 8787 | LISTEN (localhost) |
| Cloudflared | `pid 4015245` — `cloudflared tunnel --url http://localhost:8787` |
| Public URL | `*.trycloudflare.com` (quick tunnel, 재시작 시 변경됨) |
| Basic Auth | 활성화됨 (DASHBOARD_USER/PASS 환경변수) |

**참고**: 현재 프로세스는 6/18부터 실행 중이므로 최신 코드(chart API 등)가 반영되려면 대시보드 재시작 필요.

## 3. API 스모크 결과

| Endpoint | Status | Size | 핵심 Key | Secret |
|---|---|---|---|---|
| `/api/health` | 200 | 67B | status, db_available, now | clean |
| `/api/status` | 200 | 1.2KB | now, db, service, latest_briefing | clean |
| `/api/market` | 200 | 1.0KB | indices, macro, session, status_text | clean |
| `/api/portfolio` | 200 | 6.6KB | accounts, total_eval, total_pnl_pct | clean |
| `/api/portfolio/analytics` | 200 | 3.5KB | weighted_day_pct, total_eval, contributors | clean |
| `/api/performance` | 200 | 3.3KB | days, summary, by_action_type | clean |
| `/api/predictions` | 200 | 14.3KB | recent, open, closed | clean |
| `/api/accuracy` | 200 | 2.5KB | by_ticker | clean |
| `/api/news` | 200 | 13.9KB | articles, count, cached_at, error | clean |
| `/api/signals` | 200 | 5.6KB | items, count, generated_at | clean |
| `/api/calendar` | 200 | 3.8KB | items, count, generated_at | clean |
| `/api/decision-brief` | 200 | 2.0KB | day, blocks, empty | clean |
| `/api/ticker/MU` | 200 | 15.7KB | ticker, name, current_price, day_pct | clean |
| `/api/ticker/MU/chart` | 200 | 146B | ticker, name (points 빈 배열*) | clean |
| `/api/ticker/005930.KS/chart` | 200 | 160B | ticker, name (points 빈 배열*) | clean |

**\* chart endpoint**: 현재 실행 중인 프로세스가 chart 기능 추가 전 코드. 재시작 후 정상 동작 예상. 단위 테스트(`venv/bin/python`으로 직접 호출)에서는 78개 포인트 정상 반환 확인됨.

## 4. HTML 기능 체크

| 마커 | 존재 |
|---|---|
| `refresh-status-bar` | OK |
| `kis-holding-strip` | OK |
| `stock-detail-terminal` | OK |
| `ticker-chart-panel` | OK |
| `kis-holding-card` | OK |
| `action-detail-sheet` | OK |
| `performance-detail-sheet` | OK |
| `info-hub-panel` | OK |
| `folded-phone-layout` | OK |
| `fold-open-layout` | OK |
| `tablet-terminal-layout` | OK |
| `desktop-wide-layout` | OK |

## 5. 안전 체크

| 항목 | 결과 |
|---|---|
| GET only | OK — POST/PUT/DELETE handler 0건 |
| 금지 CTA | OK — `주문 실행`/`매수하기`/`매도하기` 0건 |
| KIS secret/account 노출 | OK — API 응답에 app_key/password 문자열 0건 |
| `.env` 추적 | 미추적 (.gitignore) |
| `.claude/settings.json` | untracked 유지, commit 안 함 |

## 6. 폴드7 수동 확인 체크리스트

### 접힘 폰 모드 (< 650px)

- [ ] 첫 화면에서 오늘 결론/총평가/보유 스트립/액션 매트릭스가 읽히는가
- [ ] KIS 보유 스트립이 가로 스크롤 되는가
- [ ] 액션 타일 클릭 시 상세 시트가 열리는가
- [ ] 종목 상세창이 bottom sheet처럼 열리고 닫히는가
- [ ] 차트 range 버튼(1D/5D/1M/3M)이 동작하는가
- [ ] 긴 문장이 너무 길게 한 줄로 늘어지지 않는가
- [ ] 새로고침 상태바가 보이고 "새로고침" 버튼이 터치 가능한가
- [ ] 포트폴리오 보유 카드에 현재가/source badge가 보이는가

### 펼침 폴드 모드 (650~899px)

- [ ] 2열/터미널 레이아웃이 자연스러운가
- [ ] 포트폴리오/성과/뉴스/신호가 중복 과밀하지 않은가
- [ ] 종목 상세창의 차트/보유/추천이 보기 좋은가
- [ ] 정보 허브가 2열로 과하지 않게 보이는가
- [ ] KIS 보유 스트립이 grid로 전환되는가

### 태블릿/PC 폭 (900px+)

- [ ] 메인+사이드 2열 구조가 깨지지 않는가
- [ ] 정보 허브가 3~4열로 표시되는가
- [ ] phone-only 중복 섹션이 숨겨져 있는가

### 공통

- [ ] 새로고침 상태바가 보이는가
- [ ] 수동 새로고침이 동작하는가 (갱신 중… → 시각 갱신)
- [ ] 탭 숨김 후 복귀 시 즉시 갱신이 트리거되는가
- [ ] 차트 range 1D/5D/1M/3M 버튼이 동작하는가
- [ ] 보유 관리가 빨간 매도처럼 보이지 않는가 (주황/중립)
- [ ] 조건부 매수가 즉시 매수처럼 보이지 않는가 ("조건 도달 시만")
- [ ] 오류 발생 시 기존 데이터가 유지되는가 (stale-data-badge)
- [ ] 성과 승/패/무 클릭 시 상세 시트가 열리는가
- [ ] 뉴스/신호/이벤트 클릭 시 상세 시트가 열리는가

## 7. 앱 이식 전 남은 판단

| 항목 | 상태 | 비고 |
|---|---|---|
| HTML UI 구조 | 완성 | 8개 마이크로 패치 완료 |
| 앱 이식 시작 가능 | 예 | `docs/mobile_app_parity_spec.md` 기준서 작성 완료 |
| 대시보드 재시작 필요 | 예 | 차트 API 등 최신 코드 반영 위해 프로세스 재시작 필요 |
| 고정 도메인 필요 여부 | 선택 | 현재 trycloudflare (무료/임시). 안정 운영 시 named tunnel 전환 검토 |
| KIS WebSocket 실시간 | 별도 phase | 현재 폴링(30~120초)으로 충분. 차후 필요 시 추가 |
| 282개 자동 테스트 | 전체 pass | HTML 마커 + 백엔드 + 안전 규칙 검증 완료 |
