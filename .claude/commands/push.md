---
description: "전체 동기화 푸시 — 커밋 + GitHub push + GCP 배포 + 서비스 재시작 + 메모리 정리"
---

# 푸시 (전체 동기화)

아래 단계를 순서대로 실행해줘. 각 단계 결과를 간결하게 보고해.

## 1. 로컬 변경사항 확인 및 커밋

```bash
git status
git diff --stat
```

- 변경사항이 있으면: 변경 내용을 분석하고, conventional commit 메시지로 커밋
- 변경사항이 없으면: "커밋할 변경사항 없음" 출력 후 다음 단계로

## 2. GitHub Push

```bash
git push origin master
```

- 이미 최신이면 "이미 최신" 출력

## 3. GCP 배포

SSH로 GCP 서버에 접속하여 양쪽 리포 모두 업데이트:

```bash
ssh ohmil@35.238.77.143 "
  echo '=== kanzaka110 리포 업데이트 ===' &&
  sudo -u kanzaka110 git -C /home/kanzaka110/Sanjuk-Stock-Simulator pull origin master &&
  echo '=== ohmil 리포 업데이트 ===' &&
  cd /home/ohmil/Sanjuk-Stock-Simulator && git pull origin master
"
```

## 4. 서비스 재시작 (코드 변경이 있었을 때만)

```bash
ssh ohmil@35.238.77.143 "
  sudo systemctl restart stock-bot &&
  sleep 2 &&
  sudo systemctl status stock-bot --no-pager -l | head -15
"
```

- 코드 변경이 없었으면 재시작 스킵

## 5. 메모리 점검

- 이번 작업에서 기억할 만한 새로운 사실이 있으면 메모리 업데이트
- 없으면 스킵

## 6. 최종 요약

한 줄로 결과 요약:
```
✅ 푸시 완료: 커밋 [있음/없음] → GitHub [push/최신] → GCP [배포/최신] → 서비스 [재시작/스킵]
```
