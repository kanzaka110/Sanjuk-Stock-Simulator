---
description: "전체 동기화 푸시 — 커밋 + GitHub push + GCP 배포 + 서비스 재시작 + 메모리 정리"
---

# 푸시 (전체 동기화)

아래 단계를 순서대로 실행해줘. 각 단계 결과를 간결하게 보고해.

**환경 안내:** GCP `sanjuk-project` 인스턴스에서 `kanzaka110` 계정으로 직접 실행. 단일 리포 운영 (`/home/kanzaka110/Sanjuk-Stock-Simulator`). 별도 SSH 접속 불필요.

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
- 원격에 새 커밋이 있으면 `git pull --rebase origin master` 후 충돌 해결 → 다시 push

## 3. GCP 배포 (자동 반영)

현재 작업 디렉토리(`/home/kanzaka110/Sanjuk-Stock-Simulator`)가 곧 GCP 운영 리포이므로 커밋·push로 이미 반영됨. 별도 pull 불필요.

```bash
git log --oneline -3
```

## 4. 서비스 재시작 (코드 변경이 있었을 때만)

```bash
sudo systemctl restart stock-bot &&
  sleep 2 &&
  sudo systemctl status stock-bot --no-pager -l | head -15
```

- 코드 변경이 없었으면 (예: 문서·메모리만 변경) 재시작 스킵

## 5. 메모리 점검

- 이번 작업에서 기억할 만한 새로운 사실이 있으면 메모리 업데이트
  - 위치: `~/.claude-rc/projects/-home-kanzaka110-Sanjuk-Stock-Simulator/memory/`
- 없으면 스킵

## 6. 최종 요약

한 줄로 결과 요약:
```
✅ 푸시 완료: 커밋 [있음/없음] → GitHub [push/최신] → 서비스 [재시작/스킵] → 메모리 [업데이트/스킵]
```
