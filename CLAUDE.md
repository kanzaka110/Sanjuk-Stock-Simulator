# CLAUDE.md

## 프로젝트 개요

주식 투자 시뮬레이터 — AI 기반 자동 브리핑 + 터미널 매매 의사결정 도구.
전략 논의/분석은 Claude Code 터미널에서, 자동화 브리핑은 API로 운영.

## 구조

```text
Sanjuk-Stock-Simulator/
├── core/                  # 핵심 비즈니스 로직
│   ├── market.py          # yfinance 데이터 수집 (지수, 매크로, 종목)
│   ├── news.py            # Gemini Google Search 뉴스 수집
│   ├── analyzer.py        # 11단계 멀티 에이전트 AI 분석 파이프라인
│   ├── multi_agent.py     # 4개 페르소나 분석 (Haiku) + 종합 (Sonnet)
│   ├── indicators.py      # 기술 지표 (RSI/MACD/볼린저/OBV + 합류 점수)
│   ├── sentiment.py       # 감성 분석 (뉴스 → -100~+100 점수)
│   ├── risk.py            # 리스크 관리 (ATR 포지션 사이징/상관관계/낙폭)
│   ├── backtest.py        # 백테스팅 엔진 (RSI/MACD/볼린저 전략 검증)
│   ├── kr_market.py       # 한국 시장 강화 (KRX 기관·외국인/펀더멘털)
│   ├── memory.py          # AI 메모리 (추천 기록 + 정확도 추적)
│   ├── regime.py          # 시장 레짐 감지 (VIX/모멘텀 → 강세/약세/횡보/위기)
│   ├── chart_vision.py    # 멀티모달 차트 분석 (이미지 → Gemini 패턴 인식)
│   ├── portfolio.py       # 포트폴리오 관리 (보유종목, 손익 계산)
│   ├── notion.py          # Notion 브리핑 저장 (블록 빌더 + 페이지 생성)
│   ├── telegram.py        # 텔레그램 알림 전송 (브리핑 결과만)
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
│   └── settings.py        # 환경변수, 포트폴리오 설정
├── deploy/                # GCP 배포
├── main.py                # 엔트리포인트
├── requirements.txt
├── .env.example
└── .gitignore
```

## 운영 모드

```text
전략 논의 / 분석 대화  →  Claude Code 터미널 (무료)
자동 브리핑            →  python main.py briefing 또는 API (유료 ~$0.06/회)
브리핑 알림 수신       →  텔레그램 (자동 전송)
```

## 사용법

```bash
python main.py              # TUI 터미널 실행
python main.py briefing     # 브리핑 생성 (Notion + 텔레그램 알림)
python main.py server       # API 서버 시작 (자동화용)
python main.py price        # Notion 주가 업데이트
```

## 11단계 분석 파이프라인

```text
[1] 뉴스 수집: Gemini 2.5 Pro + Google Search
[2] 시장 레짐: VIX + 모멘텀 → 강세/약세/횡보/위기 자동 분류
[3] 기술 지표: RSI, MACD, 볼린저밴드, OBV + 합류 점수 (로컬)
[4] 감성 분석: Gemini 2.5 Flash (뉴스 → -100~+100 점수)
[5] 리스크 분석: ATR 포지션 사이징, 상관관계, 최대 낙폭 (로컬)
[6] 백테스트: RSI/MACD/볼린저 전략 검증 (로컬)
[7] 한국 시장: KRX 기관/외국인 매매, PER/PBR/배당률
[8] AI 메모리: 과거 추천 정확도 추적, 미결 추천 자동 평가
[9] 차트 패턴: matplotlib 차트 → Gemini Vision 패턴 인식
[10] 페르소나 분석: Claude Haiku 4.5 × 4 (가치/성장/기술/매크로, 병렬)
[11] 종합 전략: Claude Sonnet 4.6 (11개 데이터 소스 통합 판단)
```

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
BRIEFING_TYPE=MANUAL  # 브리핑 유형
API_SECRET_KEY=       # API 서버 인증키 (비어있으면 인증 비활성화)
API_PORT=8000         # API 서버 포트
```

## GCP 관리

- 인스턴스: sanjuk-project (us-central1-b)
- 서비스: stock-simulator (브리핑 자동화)
- 리포 경로: ~/Sanjuk-Stock-Simulator/
- venv: ~/Sanjuk-Stock-Simulator/venv/ (독립)

## 개발 참고

- Textual TUI 프레임워크 사용
- 전략 논의는 Claude Code 터미널에서 직접 수행 (추가 비용 없음)
- 자동화 브리핑만 API 호출 (월 ~$4)
- 문서는 한국어로 작성
