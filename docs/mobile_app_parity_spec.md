# Sanjuk Stock Mobile Parity Spec

HTML 대시보드에서 검증된 투자 터미널 구조를 React Native 앱으로 이식하기 위한 기준서.

## 1. 목적

- HTML 대시보드에서 검증된 투자 터미널 구조를 앱으로 이식한다.
- 앱은 HTML과 동일한 정보 구조/안전 문구/상세창 동작을 가져간다.
- 실제 주문/자동매매는 포함하지 않는다.
- 이 문서가 앱 이식의 "소스 오브 트루스"다.

## 2. 공통 원칙

- **read-only** — 모든 API 호출은 GET만 사용
- POST/PUT/DELETE 금지
- 실제 주문 CTA 금지 (`주문 실행`, `매수하기`, `매도하기`)
- KIS 키/계좌정보 앱 노출 금지
- 앱은 GCP API(Cloudflare Tunnel)만 호출
- `EXPO_PUBLIC_*`는 노출 가능하므로 read-only gateway URL만 사용
- 안전 문구 필수:
  - `보유 관리 · 실행 매도 아님`
  - `조건 도달 시만`
  - `준실시간 · 60초 캐시 · 실시간 보장 아님`

## 3. 데이터 API 목록

| Endpoint | 앱 사용 위치 | 갱신 주기 |
|---|---|---|
| `GET /api/health` | 앱 시작/헬스체크 | — |
| `GET /api/status` | Home status bar | 30초 |
| `GET /api/market` | Home hero, Market tab | 30초 |
| `GET /api/portfolio` | Home strip, Portfolio tab | 120초 |
| `GET /api/portfolio/analytics` | Home bento, Portfolio contribution | 120초 |
| `GET /api/performance` | Performance tab | 120초 |
| `GET /api/predictions` | Actions, Performance closed | 120초 |
| `GET /api/accuracy` | Performance ticker accuracy | 120초 |
| `GET /api/news` | Info hub, Market tab | 600초 |
| `GET /api/signals` | Info hub, Signals section | 300초 |
| `GET /api/calendar` | Info hub, Events section | 600초 |
| `GET /api/decision-brief` | Home decision card | 30초 |
| `GET /api/recommendations/timeline` | Actions timeline | 30초 |
| `GET /api/ticker/{ticker}` | TickerDetailSheet | on-demand |
| `GET /api/ticker/{ticker}/chart?range=1d&interval=5m` | PriceChartPanel | on-demand (60초 캐시) |

### Chart API 응답 shape

```json
{
  "ticker": "MU",
  "name": "마이크론",
  "range": "1d",
  "interval": "5m",
  "source": "KIS+yfinance",
  "updated_at": "2026-06-21T13:00:35",
  "cache_age_sec": 12,
  "current_price": 1151.95,
  "day_pct": 1.58,
  "points": [
    {"time": "09:30", "open": 120, "high": 122, "low": 119, "close": 121, "volume": 100000}
  ],
  "error": ""
}
```

### Range/Interval 매핑

| range | interval | yfinance period |
|---|---|---|
| `1d` | `5m` | `period="1d"` |
| `5d` | `15m` | `period="5d"` |
| `1mo` | `1d` | `period="1mo"` |
| `3mo` | `1d` | `period="3mo"` |

허용 외 값 → 안전 fallback `1d`/`5m`

## 4. 화면 구조

### Home

```
RefreshStatusBar (준실시간 · 마지막 갱신 · 새로고침 버튼)
PortfolioHero (총 평가액 / 손익 / 시장 모드)
LiveHoldingStrip (주요 보유 8종목 가로 스크롤)
TodayDecisionCard (오늘 결론 1~2줄)
ActionMatrix (2x2: 실행/조건부/보유관리/관망, 클릭→상세)
PriorityRail (실행→조건부→보유관리 top 3)
AlertStack (위험/보호종목)
PortfolioBento (기여도·보유TOP·자산군·계좌)
InfoHubCompact (뉴스·신호·이벤트·타임라인 4섹션)
```

### Portfolio

```
PortfolioSummary (총평가/손익/현금비중)
AllocationDonut (자산군 배분)
AccountSections (계좌별 HoldingCard 리스트)
ContributionBars (종목별 손익 기여)
RankingBar (수익률 순위)
```

### Actions

```
ActionSummary (실행/조건부/보유관리/관망 count)
ActionDetailSheet (그룹별 상세 모달)
RecommendationTimeline (오늘 추천 흐름)
OpenPredictions (미결 조건 리스트)
```

### Performance

```
ThirtyDaySummary (승/패/무 클릭→상세)
ActionTypePerf (액션별 avg_pnl, 클릭→상세)
BriefingTypePerf
TickerAccuracy (종목별 승률, 클릭→상세)
ClosedResults (최근 종료)
BenchmarkComparison
ContributionChart
```

### News/Info (InfoHubPanel)

```
NewsList (카테고리 필터)
SignalsList (합류 점수 정렬)
EventCalendar (D-day 정렬)
DecisionTimeline (최근 판단)
각 항목 클릭 → 상세 모달
```

### TickerDetailSheet

```
Header (종목명/티커/현재가/등락/source badge/cache)
PriceChartPanel (1D/5D/1M/3M range chips)
HoldingSnapshot (보유수량/평단/평가금/수익률/비중)
RecommendationSnapshot (미결조건/이력 접힘)
RelatedNews (관련 뉴스 3개)
SafetyFooter (준실시간 · 60초 캐시 · 실시간 보장 아님)
```

## 5. 반응형 레이아웃 기준

| 이름 | width | columns | 특징 |
|---|---|---|---|
| foldedPhone | < 650px | 1열 | 핵심 정보 가독성 우선, 가로 스크롤 strip |
| foldOpen | 650~899px | 1~2열 | 내부 패널 2열 가능, 카드 최소폭 280px |
| tabletTerminal | 900~1199px | 2열 + wide | 메인 1.8fr / 사이드 0.8fr |
| desktopWeb | ≥ 1200px | 2~3열 | max-width 1400px, 3열까지만 |

### 상세창 크기

- foldedPhone: bottom-sheet, max-height 88vh
- foldOpen+: center modal, max-width 840px, max-height 84vh

## 6. 상세창/상호작용

| 트리거 | 함수 | 모달 내용 |
|---|---|---|
| 종목 클릭 | `openM(ticker)` → `loadTicker` | TickerDetailSheet |
| 액션 타일 클릭 | `openActionDetail(kind)` | ActionDetailSheet |
| 성과 항목 클릭 | `openPerfDetail(kind, key)` | PerformanceDetailSheet |
| 뉴스/신호/이벤트/타임라인 | `openInfoDetail(kind, idx)` | InfoDetailSheet |

### 공통 규칙

- 카드/row에 `상세 ›` 텍스트 표시
- 닫으면 기존 스크롤 위치 유지
- 중첩 모달 금지 — 모달 body 교체 또는 닫고 열기
- 상세창 내 종목명 클릭 → 모달 닫기 → setTimeout → openM

## 7. 차트/시세

### PriceChartPanel

- endpoint: `GET /api/ticker/{ticker}/chart?range={range}`
- range chips: 1D / 5D / 1M / 3M
- source 표시: `KIS`, `KIS+yfinance`, `yfinance`
- cache_age_sec 표시: `N초 전 갱신` 또는 `방금 갱신`
- 실패 시: 기존 차트 유지 + "차트 데이터 대기" 메시지
- 항상 표시: `준실시간 · 60초 캐시 · 실시간 보장 아님`

### SVG 차트 구성

- 라인차트 (close 기준)
- 볼륨 바 (하단)
- 고가/저가 H/L 마커
- 현재가 dot + 추천 기준선 (진입/목표/손절)
- gradient fill

## 8. 자동 갱신

| 그룹 | 주기 | API |
|---|---|---|
| 고빈도 | 30초 | market, action, timeline, decision-brief |
| 중빈도 | 120초 | portfolio, performance, simulator, analytics |
| 저빈도(신호) | 300초 | signals |
| 저빈도(뉴스) | 600초 | news, calendar |

### 동작 규칙

- foreground: 정상 갱신
- background/hidden: 갱신 중지 (safeInterval)
- visible 복귀: 즉시 refreshAllNow() 1회
- 수동 새로고침: 모든 고/중빈도 API 즉시 호출
- 실패 시: DOM 덮어쓰지 않음, stale-data-badge 표시
- 중복 클릭 방지

## 9. UI 컴포넌트 매핑

| HTML marker | 앱 컴포넌트 |
|---|---|
| `refresh-status-bar` | `RefreshStatusBar` |
| `kis-holding-strip` | `LiveHoldingStrip` |
| `stock-detail-terminal` | `TickerDetailSheet` |
| `ticker-chart-panel` | `PriceChartPanel` |
| `kis-holding-card` | `HoldingCard` |
| `holding-pnl-grid` | `HoldingPnlGrid` |
| `quote-source-badge` | `QuoteSourceBadge` |
| `action-detail-sheet` | `ActionDetailSheet` |
| `performance-detail-sheet` | `PerformanceDetailSheet` |
| `info-hub-panel` | `InfoHubPanel` |
| `news-detail-sheet` | `NewsDetailSheet` |
| `signal-detail-sheet` | `SignalDetailSheet` |
| `event-detail-sheet` | `EventDetailSheet` |
| `decision-timeline-panel` | `DecisionTimelinePanel` |
| `action-matrix` | `ActionMatrix` |
| `priority-rail` | `PriorityRail` |
| `bento-hero` | `PortfolioHero` |

## 10. 금지/주의

### 절대 금지

- `주문 실행` CTA
- `매수하기` / `매도하기` 버튼
- 실제 주문 endpoint 호출
- KIS secret/account 앱 노출
- 네이버 크롤링
- POST/PUT/DELETE API

### 주의

- WebSocket 실시간은 별도 phase (이번 스펙 범위 밖)
- yfinance 과신 금지 — source/cache 항상 표시
- 0%/무의미 값 표시 금지 (`isMeaningfulPct`, `fmtPctSmart`)
- `AI_SELL_MANAGEMENT` → 사용자에게 `보유 관리`
- MU/보호종목을 빨간 매도처럼 표시 금지

## 11. 앱 이식 순서 (마이크로 패치)

1. API 타입/client 정리 (`lib/api/types.ts`, `lib/api/client.ts`)
2. `RefreshStatusBar` + polling hook
3. `LiveHoldingStrip` (가로 FlatList)
4. `TickerDetailSheet` + `PriceChartPanel` (react-native-svg)
5. `HoldingCard` / Portfolio screen
6. `ActionDetailSheet` (BottomSheet)
7. `PerformanceDetailSheet`
8. `InfoHubPanel` (News/Signal/Event/Timeline)
9. Fold layout (useWindowDimensions breakpoints)
10. QA / APK 빌드

## 12. 검증 체크리스트

- [ ] TypeScript typecheck pass
- [ ] Jest unit tests pass
- [ ] Expo export 성공
- [ ] 금지 CTA grep: `주문 실행|매수하기|매도하기` → 0건
- [ ] POST/PUT/DELETE grep → 0건
- [ ] `.env`/secret 미포함
- [ ] foldedPhone / foldOpen / tablet 스크린샷 확인
- [ ] API 오류 시 fallback 동작 확인
- [ ] 차트 실패 시 기존 데이터 유지 확인
- [ ] `보유 관리 · 실행 매도 아님` 문구 존재 확인
- [ ] `조건 도달 시만` 문구 존재 확인
- [ ] `준실시간 · 60초 캐시 · 실시간 보장 아님` 문구 존재 확인
