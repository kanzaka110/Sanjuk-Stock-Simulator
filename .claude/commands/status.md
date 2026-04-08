---
description: 시스템 상태 점검 — 서킷 브레이커, 설정 검증, 서비스 상태
user-invocable: true
---

# 시스템 상태 점검

서킷 브레이커 상태, 설정 검증, Task 레지스트리 현황을 확인합니다.

## 지시사항

아래 항목을 순서대로 확인하고 테이블로 보여주세요:

### 1. 서킷 브레이커 상태
```python
from core.recovery import kis_breaker, yfinance_breaker, claude_breaker, gemini_breaker

breakers = [kis_breaker, yfinance_breaker, claude_breaker, gemini_breaker]
for b in breakers:
    print(f"{b.name}: {b.state} (failures: {b._failure_count})")
```

### 2. 설정 검증
```python
from core.config_loader import validate_config

for mode in ["briefing", "monitor", "bot", "server"]:
    v = validate_config(mode)
    status = "OK" if v.valid else f"FAIL: {v.missing_required}"
    print(f"{mode}: {status}")
```

### 3. 현재 운영 모드
```python
from core.permissions import get_policy
policy = get_policy()
print(f"Mode: {policy.mode.value}")
print(f"Allowed: {len(policy.allowed_actions())} actions")
```

### 4. GCP 서비스 상태 (선택)
GCP에 SSH 접속 가능하면:
```bash
ssh ohmil@35.238.77.143 "sudo systemctl is-active stock-bot && sudo journalctl -u stock-bot --no-pager -n 5"
```

## 출력

| 항목 | 상태 | 비고 |
|------|------|------|
