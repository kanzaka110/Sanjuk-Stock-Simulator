# CLAUDE.md

## 프로젝트 개요

주식 투자 시뮬레이터 — AI 기반 인터랙티브 터미널 매매 의사결정 도구.
Sanjuk-Notion-Telegram-Bot/Stock_bot에서 분리된 독립 프로젝트.

## 구조

```
Sanjuk-Stock-Simulator/
├── core/                  # 핵심 비즈니스 로직
│   ├── market.py          # yfinance 데이터 수집 (지수, 매크로, 종목)
│   ├── news.py            # Gemini Google Search 뉴스 수집
│   ├── analyzer.py        # Claude Sonnet AI 분석 (매수/매도 신호)
│   ├── portfolio.py       # 포트폴리오 관리 (보유종목, 손익 계산)
│   └── models.py          # 데이터 모델 (frozen dataclass)
├── terminal/              # 터미널 UI (Textual TUI)
│   ├── app.py             # 메인 TUI 앱
│   ├── screens/           # 화면 모듈
│   │   ├── dashboard.py   # 대시보드 (지수/매크로/포트폴리오)
│   │   ├── analysis.py    # AI 분석 결과 화면
│   │   ├── trade.py       # 매매 시뮬레이션 화면
│   │   └── ask.py         # AI 질의 화면
│   └── widgets/           # 커스텀 위젯
│       ├── ticker_table.py
│       ├── signal_badge.py
│       └── chart.py
├── db/                    # 데이터 저장
│   └── store.py           # SQLite (매매 기록, 브리핑 히스토리)
├── config/                # 설정
│   └── settings.py        # 환경변수, 포트폴리오 설정
├── deploy/                # GCP 배포
│   ├── stock-simulator.service
│   └── run_simulator.sh
├── main.py                # 엔트리포인트
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## AI 모델 사용

```
정보 수집: Gemini 2.5 Pro + Google Search (실시간 뉴스)
분석/전략: Claude Sonnet 4.6 (매수/매도 신호, 전략 생성)
```

## 핵심 기능

1. **실시간 대시보드** — 포트폴리오 현황, 지수, 매크로 지표
2. **AI 브리핑** — 기존 Stock_bot 브리핑 로직 재활용 + 터미널 렌더링
3. **매매 시뮬레이션** — 가상 매수/매도, 수익률 추적
4. **AI 질의** — "한화에어로스페이스 팔때 됐나?" 같은 자연어 질문
5. **매매 기록** — SQLite 기반 거래 이력 관리

## 환경변수

```
GEMINI_API_KEY=       # Gemini 2.5 Pro (뉴스 수집)
CLAUDE_API_KEY=       # Claude Sonnet 4.6 (분석)
```

## 개발 참고

- Textual TUI 프레임워크 사용
- 기존 Stock_bot의 Notion/텔레그램 의존성 제거
- 독립 실행 가능한 터미널 앱
- GCP에서도 실행 가능 (SSH 터미널)
- 문서는 한국어로 작성
