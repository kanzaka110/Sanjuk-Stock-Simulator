---
description: 전 계좌 보유종목 현황 + 현재 시세 + 수익률 확인
user-invocable: true
---

# 보유종목 확인

현재 보유 중인 전 계좌의 종목 현황을 조회합니다.

## 지시사항

1. `config/settings.py`에서 HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_IRP, HOLDINGS_PENSION을 읽습니다.
2. `core/market.py`의 `_get_quote_realtime()`으로 각 종목의 현재가를 조회합니다.
3. 계좌별로 종목명, 보유수량, 평균단가, 현재가, 수익률을 테이블로 보여줍니다.
4. 계좌별 합계와 전체 포트폴리오 합계를 계산합니다.

## 실행

```python
from config.settings import (
    HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_IRP, HOLDINGS_PENSION,
    DEFAULT_CASH, ISA_CASH, IRP_CASH, IRP_DEFAULT_OPTION, PENSION_MMF,
    PORTFOLIO
)
from core.market import _get_quote_realtime

all_holdings = {
    "[일반]": (HOLDINGS_GENERAL, DEFAULT_CASH),
    "[ISA]": (HOLDINGS_ISA, ISA_CASH),
    "[IRP]": (HOLDINGS_IRP, IRP_CASH),
    "[연금저축]": (HOLDINGS_PENSION, PENSION_MMF),
}

for account, (holdings, cash) in all_holdings.items():
    # 각 종목 현재가 조회 후 수익률 계산
    pass
```

## 출력 형식

| 계좌 | 종목 | 수량 | 평균단가 | 현재가 | 수익률 | 평가금액 |
|------|------|------|---------|--------|--------|---------|

마지막에 전체 평가금액, 총 수익률, 계좌별 전략 요약을 덧붙입니다.
