---
description: "전체 동기화 풀 — GitHub pull + GCP 변경사항 확인 + 로컬 동기화"
---

# 풀 (전체 동기화)

아래 단계를 순서대로 실행해줘. 각 단계 결과를 간결하게 보고해.

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

## 3. GCP 변경사항 확인

GCP 서버에 로컬에 없는 커밋이 있는지 확인:

```bash
ssh ohmil@35.238.77.143 "
  echo '=== ohmil 리포 상태 ===' &&
  cd /home/ohmil/Sanjuk-Stock-Simulator && git log --oneline -3 &&
  echo '=== kanzaka110 리포 상태 ===' &&
  sudo -u kanzaka110 git -C /home/kanzaka110/Sanjuk-Stock-Simulator log --oneline -3
"
```

- GCP에만 있는 커밋이 발견되면: GCP에서 먼저 push 후 로컬에서 pull 필요하다고 안내
- 양쪽 동일하면 "동기화 완료" 출력

## 4. GCP 리포도 동기화

GitHub pull 후 GCP도 최신으로:

```bash
ssh ohmil@35.238.77.143 "
  sudo -u kanzaka110 git -C /home/kanzaka110/Sanjuk-Stock-Simulator pull origin master &&
  cd /home/ohmil/Sanjuk-Stock-Simulator && git pull origin master
"
```

## 5. CLAUDE.md 변경 확인

```bash
git diff HEAD~5 --name-only | grep -E "(CLAUDE\.md|\.claude/)" || echo "설정 파일 변경 없음"
```

- CLAUDE.md나 .claude/ 설정이 변경되었으면 내용을 읽고 메모리 업데이트 필요 여부 판단

## 6. 최종 요약

한 줄로 결과 요약:
```
✅ 풀 완료: GitHub [N개 커밋 수신/최신] → GCP [동기화/경고] → 설정 [변경있음/없음]
```
