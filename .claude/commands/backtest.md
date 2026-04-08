---
description: 백테스트 실행 — 지정 종목/전략의 과거 성과 검증
user-invocable: true
---

# 백테스트 실행

사용자의 요청에 따라 백테스트를 실행합니다.

## 지시사항

1. `core/backtest.py`의 백테스트 엔진을 사용하여 지정된 종목과 전략을 테스트합니다.
2. 인자가 없으면 주요 보유 종목(NVDA, 005930.KS, 012450.KS, MU)에 대해 RSI/MACD/볼린저 전략을 실행합니다.
3. 인자가 있으면 해당 종목/전략만 실행합니다.

## 실행 방법

```bash
cd $PROJECT_ROOT
python -c "
from core.backtest import backtest_all_strategies, backtest_regime_aware
results = backtest_all_strategies(['$ARGUMENTS' if '$ARGUMENTS' else 'NVDA,005930.KS'])
for r in results:
    print(r)
print('---')
regime = backtest_regime_aware(['$ARGUMENTS' if '$ARGUMENTS' else 'NVDA,005930.KS'])
for r in regime:
    print(r)
"
```

## 결과 해석

- 승률, 수익률, 최대 낙폭(MDD), 샤프 비율을 보고합니다.
- RSI 역추세, MACD 크로스오버, 볼린저밴드 전략 각각의 성과를 비교합니다.
- 레짐 기반 백테스트는 시장 상태(강세/약세/횡보)에 따른 전략 전환 효과를 보여줍니다.
