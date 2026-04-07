# CLAUDE.md

## 프로젝트 개요

주식 투자 시뮬레이터 — AI 기반 자동 브리핑 + 터미널 매매 의사결정 도구.
전략 논의/분석은 Claude Code 터미널에서, 자동화 브리핑은 API로 운영.

## 구조

```text
Sanjuk-Stock-Simulator/
├── core/                  # 핵심 비즈니스 로직
│   ├── market.py          # yfinance 실시간 시세 (1분봉 인트라데이 + 일봉 폴백)
│   ├── news.py            # Gemini Google Search 뉴스 수집 (시장별 프롬프트)
│   ├── analyzer.py        # 11단계 멀티 에이전트 AI 분석 파이프라인
│   ├── multi_agent.py     # 4개 페르소나 분석 (Haiku) + 종합 (Sonnet) + 계좌별 전략
│   ├── indicators.py      # 기술 지표 (RSI/MACD/볼린저/OBV + 합류 점수)
│   ├── sentiment.py       # 감성 분석 (뉴스 → -100~+100 점수)
│   ├── risk.py            # 리스크 관리 (ATR 포지션 사이징/상관관계/낙폭)
│   ├── backtest.py        # 백테스팅 엔진 (RSI/MACD/볼린저 전략 검증)
│   ├── kr_market.py       # 한국 시장 강화 (KRX 기관·외국인/펀더멘털)
│   ├── memory.py          # AI 메모리 (추천 기록 + 정확도 추적)
│   ├── regime.py          # 시장 레짐 감지 (VIX/모멘텀 → 강세/약세/횡보/위기)
│   ├── chart_vision.py    # 멀티모달 차트 분석 (이미지 → Gemini 패턴 인식)
│   ├── fundamentals.py    # 재무 데이터 (PER/EPS/매출/실적일정)
│   ├── portfolio.py       # 포트폴리오 관리 (보유종목, 손익, 매매 제약)
│   ├── notion.py          # Notion 브리핑 저장 (블록 빌더 + 페이지 생성)
│   ├── telegram.py        # 텔레그램 알림 전송 (브리핑 결과만)
│   ├── telegram_bot.py    # 텔레그램 봇 명령 수신 (getUpdates 폴링)
│   ├── monitor.py         # 2-tier 시장 모니터 (수치 체크 + AI 알림)
│   ├── monitor_models.py  # 모니터링 데이터 모델 (AlertTrigger/AlertResult)
│   ├── market_hours.py    # 장 시간 판별 (한국장/미국장/써머타임)
│   ├── briefing_runner.py # 브리핑 실행 공통 로직 (API+봇 공유)
│   ├── price_updater.py   # Notion 주가 자동 업데이트
│   └── models.py          # 데이터 모델 (frozen dataclass)
├── api/                   # API 서버 (자동화용)
│   └── server.py          # FastAPI 브리핑 엔드포인트
├── terminal/              # 터미널 UI (Textual TUI)
│   ├── app.py             # 메인 TUI 앱
│   └── screens/           # 화면 모듈
├── db/                    # 데이터 저장
│   └── store.py           # SQLite (매매 기록, 포지션, 예수금)
├── config/                # 설정
│   └── settings.py        # 환경변수, 포트폴리오, HOLDINGS, 시장별 분리
├── deploy/                # GCP 배포
├── main.py                # 엔트리포인트
├── requirements.txt
├── .env.example
└── .gitignore
```

## 운영 모드

```text
전략 논의 / 분석 대화  →  Claude Code 터미널 (무료)
자동 브리핑 (한국장)   →  KST 08:30, 한국 종목 중심
자동 브리핑 (미국장)   →  KST 21:00, 미국 종목 중심
수시 브리핑 (폰)       →  텔레그램에서 "전체 브리핑" 입력
수시 브리핑 (PC)       →  python main.py briefing (MANUAL)
긴급 시장 알림         →  시장 모니터 자동 감지 → 텔레그램 알림
보유종목 확인 (폰)     →  텔레그램에서 "보유종목 확인" 입력
```

## 사용법

```bash
python main.py              # TUI 터미널 실행
python main.py briefing     # 브리핑 생성 (Notion + 텔레그램 알림)
python main.py bot          # 텔레그램 봇 + 시장 모니터 실행 (GCP 상시)
python main.py monitor      # 시장 모니터만 실행
python main.py server       # API 서버 시작 (자동화용)
python main.py price        # Notion 주가 업데이트
```

## 텔레그램 봇 명령어

```text
전체 브리핑       →  전체 포트폴리오 11단계 AI 분석 (3~5분 소요)
한국장 브리핑     →  한국 종목 중심 분석
미국장 브리핑     →  미국 종목 중심 분석
보유종목 확인     →  전 계좌 현재 시세 + 수익률
도움말            →  명령어 목록
```

## 시장 모니터 (긴급 알림)

```text
5분 간격 자동 감시 (yfinance, 무료) → 트리거 시 AI 분석 (Haiku, ~$0.02/회)
- VIX 30+ 급등        →  🚨 공포지수 급등 알림
- RSI < 30 과매도     →  📉 과매도 진입 알림
- RSI > 70 과매수     →  📈 과매수 경고 알림
- 일중 ±5% 급등락    →  🔻/🔺 급등락 감지 알림
- 동일 종목 1시간 쿨다운, 장 시간에만 동작
```

## 시장별 브리핑 분리

```text
BRIEFING_TYPE=KR_BEFORE  →  한국 종목(삼성전자, 한화에어로 등) + KOSPI/KOSDAQ 중심
BRIEFING_TYPE=US_BEFORE  →  미국 종목(NVDA, GOOGL, MU, LMT) + S&P500/NASDAQ 중심
BRIEFING_TYPE=MANUAL     →  전체 포트폴리오 (기본값)
```

## 11단계 분석 파이프라인

```text
[1] 뉴스 수집: Gemini 2.5 Pro + Google Search (시장별 맞춤 프롬프트)
[2] 시장 레짐: VIX + 모멘텀 → 강세/약세/횡보/위기 자동 분류
[3] 기술 지표: RSI, MACD, 볼린저밴드, OBV + 합류 점수 (로컬)
[4] 감성 분석: Gemini 2.5 Flash (뉴스 → -100~+100 점수)
[5] 리스크 분석: ATR 포지션 사이징, 상관관계, 최대 낙폭 (로컬)
[6] 백테스트: RSI/MACD/볼린저 전략 검증 (로컬)
[7] 재무 데이터: PER/EPS/매출/실적일정 (yfinance)
[8] 한국 시장: KRX 기관/외국인 매매, PER/PBR/배당률 (US_BEFORE 시 스킵)
[9] AI 메모리: 과거 추천 정확도 추적, 미결 추천 자동 평가
[10] 차트 패턴: matplotlib 차트 → Gemini Vision 패턴 인식
[11] 페르소나 분석: Claude Haiku 4.5 × 4 (가치/성장/기술/매크로, 병렬)
[12] 종합 전략: Claude Sonnet 4.6 — 실제 보유 포지션 + 계좌별 전략 포함
```

## 계좌별 전략 시스템

브리핑 AI에 실제 보유 포지션(HOLDINGS)과 계좌 규칙이 주입됨:
- 모든 매수/매도 신호에 `[일반]` `[ISA]` `[RIA]` `[연금저축]` `[IRP]` 태그 필수
- RIA 5/31 데드라인, 해외매수 금지 등 규칙 AI 프롬프트에 직접 반영
- ISA 계좌: 국내주식/국내상장 ETF만 매수 가능, 예수금 2,000만원
- briefing_type에 따라 HOLDINGS도 한국/미국 필터링 (KR_BEFORE → .KS만)
- 브리핑 마지막에 계좌별 전략 요약 섹션 자동 생성

## 실시간 시세

- 장중: yfinance 1분봉 인트라데이 (`_get_quote_realtime`)
- 장 마감: 일봉 자동 폴백 (`_get_quote_daily`)
- 추가 API 키 불필요

## 환경변수

```bash
GEMINI_API_KEY=       # Gemini 2.5 Pro/Flash (뉴스 + 감성 + 차트)
CLAUDE_API_KEY=       # Claude Haiku/Sonnet (페르소나 + 종합 판단)
NOTION_API_KEY=       # Notion 브리핑 저장
NOTION_DB_ID=         # Notion 브리핑 DB ID
NOTION_TOKEN=         # Notion 주가 업데이트 토큰
NOTION_DATABASE_ID=   # Notion 주가 DB ID
TELEGRAM_BOT_TOKEN=   # 텔레그램 봇 토큰 (알림 전송용)
TELEGRAM_CHAT_ID=     # 텔레그램 채팅 ID
BRIEFING_TYPE=MANUAL  # 브리핑 유형 (KR_BEFORE / US_BEFORE / MANUAL)
API_SECRET_KEY=       # API 서버 인증키 (비어있으면 인증 비활성화)
API_PORT=8000         # API 서버 포트
```

## GCP 관리

- 인스턴스: sanjuk-project (us-central1-b)
- **주의: SSH 사용자(`ohmil`) ≠ 서비스 사용자(`kanzaka110`)**
- 서비스 리포: /home/kanzaka110/Sanjuk-Stock-Simulator/
- SSH 리포: /home/ohmil/Sanjuk-Stock-Simulator/
- venv: /home/kanzaka110/Sanjuk-Stock-Simulator/venv/
- **배포 시 kanzaka110 경로에도 반드시 git pull:**
  `sudo -u kanzaka110 git -C /home/kanzaka110/Sanjuk-Stock-Simulator pull origin master`
- 서비스: `stock-bot` (텔레그램 봇 + 모니터, systemd 상시 실행)
- 로그: `sudo journalctl -u stock-bot -f`
- 한국장 브리핑: KST 08:30 (UTC 23:30) — cron
- 미국장 브리핑: KST 21:00 (UTC 12:00) — cron
- 주가 업데이트: 국내 개장/마감 + 미국 개장/마감 (4회/일)

## 개발 참고

- Textual TUI 프레임워크 사용
- 전략 논의는 Claude Code 터미널에서 직접 수행 (추가 비용 없음)
- 텔레그램 대화형 챗봇은 비용 문제로 폐기 — 명령 수신 + 알림 전송만
- 자동화 브리핑 API 호출 (월 ~$4) + 모니터 알림 (월 ~$0.1)
- 문서는 한국어로 작성
- 개인 금융정보 포함 — 보안 유의 (API 키/계좌번호 하드코딩 금지)
