---
description: "전체 동기화 풀 — GitHub pull + 로컬 정리"
---

# 풀 (전체 동기화)

아래 단계를 순서대로 실행해줘. 각 단계 결과를 간결하게 보고해.

**환경 안내:** GCP `sanjuk-project` 인스턴스에서 `kanzaka110` 계정으로 직접 실행. 단일 리포 운영 (`/home/kanzaka110/Sanjuk-Stock-Simulator`).

## 1. 로컬 상태 확인

```bash
git status
git log --oneline -3
```

- 커밋되지 않은 로컬 변경사항이 있으면 경고 후 stash 여부 물어보기

## 2. GitHub에서 Pull

```bash
git pull origin master
```

- 새로운 커밋이 있으면 변경 내용 요약
- 이미 최신이면 "이미 최신" 출력
- 충돌 발생 시 사용자에게 어느 쪽 데이터를 유지할지 확인

## 3. CLAUDE.md / 설정 변경 확인

```bash
git diff HEAD~5 --name-only | grep -E "(CLAUDE\.md|\.claude/)" || echo "설정 파일 변경 없음"
```

- CLAUDE.md나 .claude/ 설정이 변경되었으면 내용을 읽고 메모리 업데이트 필요 여부 판단

## 4. 서비스 재시작 (코드 변경이 있었을 때만)

```bash
sudo systemctl restart stock-bot &&
  sleep 2 &&
  sudo systemctl is-active stock-bot
```

- 문서·메모리만 변경됐으면 스킵

## 5. 최종 요약

한 줄로 결과 요약:
```
✅ 풀 완료: GitHub [N개 커밋 수신/최신] → 설정 [변경있음/없음] → 서비스 [재시작/스킵]
```
