# CLAUDE.md

## 프로젝트 개요

주식 투자 시뮬레이터 — AI 기반 인터랙티브 터미널 매매 의사결정 도구.
Sanjuk-Notion-Telegram-Bot/Stock_bot의 모든 기능을 통합한 독립 프로젝트.

## 구조

```
Sanjuk-Stock-Simulator/
├── core/                  # 핵심 비즈니스 로직
│   ├── market.py          # yfinance 데이터 수집 (지수, 매크로, 종목)
│   ├── news.py            # Gemini Google Search 뉴스 수집
│   ├── analyzer.py        # Claude Sonnet AI 분석 (매수/매도 신호)
│   ├── portfolio.py       # 포트폴리오 관리 (보유종목, 손익 계산)
│   ├── notion.py          # Notion 브리핑 저장 (블록 빌더 + 페이지 생성)
│   ├── telegram.py        # 텔레그램 알림 + 챗봇 (Gemini)
│   ├── price_updater.py   # Notion 주가 자동 업데이트
│   ├── chat_repl.py       # 대화형 CLI REPL (멀티턴 전략 논의)
│   └── models.py          # 데이터 모델 (frozen dataclass)
├── api/                   # API 서버 (자동화용)
│   └── server.py          # FastAPI 브리핑 엔드포인트
├── terminal/              # 터미널 UI (Textual TUI)
│   ├── app.py             # 메인 TUI 앱
│   └── screens/           # 화면 모듈
│       ├── dashboard.py   # [d] 대시보드 (지수/매크로/포트폴리오)
│       ├── analysis.py    # [a] AI 분석 결과 화면
│       ├── trade.py       # [t] 매매 시뮬레이션 화면
│       ├── ask.py         # [q] AI 질의 화면
│       ├── briefing.py    # [b] 브리핑 생성 → Notion + 텔레그램
│       └── services.py    # [s] 서비스 관리 (챗봇, 주가 업데이트)
├── db/                    # 데이터 저장
│   ├── store.py           # SQLite (매매 기록, 포지션, 예수금)
│   └── chat_store.py      # SQLite (챗봇 대화, 매매 노트)
├── config/                # 설정
│   └── settings.py        # 환경변수, 포트폴리오 설정
├── deploy/                # GCP 배포
│   ├── stock-simulator.service  # TUI 서비스
│   ├── stock-chatbot.service    # 텔레그램 챗봇 서비스
│   ├── run_simulator.sh
│   └── setup.sh
├── main.py                # 엔트리포인트
├── requirements.txt
├── .env.example
└── .gitignore
```

## AI 모델 사용

```
정보 수집: Gemini 2.5 Pro + Google Search (실시간 뉴스)
분석/전략: Claude Sonnet 4.6 (매수/매도 신호, 전략 생성)
챗봇 대화: Gemini 2.5 Flash (텔레그램 대화)
```

## 핵심 기능

1. **실시간 대시보드 [d]** — 포트폴리오 현황, 지수, 매크로 지표
2. **AI 브리핑 [b]** — 생성 → Notion 저장 → 텔레그램 전송 (원클릭)
3. **AI 분석 [a]** — 매수/매도 신호 + 전략 상세
4. **매매 시뮬레이션 [t]** — 가상 매수/매도, 수익률 추적
5. **AI 질의 [q]** — 자연어 질문 ("한화에어로스페이스 팔때 됐나?")
6. **서비스 관리 [s]** — 텔레그램 챗봇 시작, Notion 주가 업데이트
7. **대화형 CLI [chat]** — 멀티턴 전략 논의 (대화 맥락 유지)
8. **API 서버 [server]** — 외부에서 브리핑 트리거 (POST /api/briefing)

## 환경변수

```
GEMINI_API_KEY=       # Gemini 2.5 Pro/Flash (뉴스 수집 + 챗봇)
CLAUDE_API_KEY=       # Claude Sonnet 4.6 (분석)
NOTION_API_KEY=       # Notion 브리핑 저장
NOTION_DB_ID=         # Notion 브리핑 DB ID
NOTION_TOKEN=         # Notion 주가 업데이트 토큰
NOTION_DATABASE_ID=   # Notion 주가 DB ID
TELEGRAM_BOT_TOKEN=   # 텔레그램 봇 토큰
TELEGRAM_CHAT_ID=     # 텔레그램 채팅 ID
BRIEFING_TYPE=MANUAL  # 브리핑 유형
API_SECRET_KEY=       # API 서버 인증키 (비어있으면 인증 비활성화)
API_PORT=8000         # API 서버 포트
```

## GCP 관리

- 인스턴스: sanjuk-project (기존 봇 리포와 동일 서버)
- 서비스: stock-simulator (TUI), stock-chatbot (텔레그램 봇)
- 리포 경로: ~/Sanjuk-Stock-Simulator/
- venv: ~/Sanjuk-Stock-Simulator/venv/ (독립)
- .env: ~/Sanjuk-Stock-Simulator/.env

## 개발 참고

- Textual TUI 프레임워크 사용
- Stock_bot에서 모든 기능 이전 완료 (Notion + 텔레그램 포함)
- 독립 실행 가능한 터미널 앱
- GCP SSH로도 TUI 사용 가능
- 문서는 한국어로 작성
