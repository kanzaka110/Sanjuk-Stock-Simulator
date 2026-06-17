# 웹 대시보드 접속 가이드

읽기 전용 대시보드 — 브리핑 추천/적중률/시스템 상태 조회. 주문/DB 수정 불가.

## 1. 로컬 접속 (SSH 터널)

가장 안전. 인증 불필요.

```bash
# GCP 서버에서 대시보드 실행
python main.py dashboard   # 127.0.0.1:8787 바인드

# 로컬 PC에서 SSH 터널
ssh -L 8787:localhost:8787 ohmil@35.238.77.143
# 브라우저: http://localhost:8787
```

## 2. 임시 공개 터널 (Cloudflare Quick Tunnel)

모바일 등 SSH 불가 환경용. 계정 없이 즉시 사용.

```bash
# 대시보드 + Basic Auth 실행
DASHBOARD_USER=sanjuk DASHBOARD_PASS=<강력한비밀번호> python main.py dashboard &

# 터널 시작
cloudflared tunnel --url http://localhost:8787
# → https://xxxx-xxxx.trycloudflare.com URL 출력
```

**주의사항:**
- URL이 매번 바뀜 (재실행 시)
- Basic Auth 필수 설정 (`DASHBOARD_USER` + `DASHBOARD_PASS`)
- 종료: `kill $(pgrep cloudflared)`

## 3. 권장 운영: Cloudflare Access (고정 도메인)

프로덕션 운영 시 권장. 고정 URL + 이메일 OTP 인증.

```bash
# 1. Cloudflare 계정에서 Zero Trust 대시보드 접속
#    https://one.dash.cloudflare.com

# 2. Access > Tunnels > Create Tunnel
#    - 이름: sanjuk-dashboard
#    - 도메인: dashboard.yourdomain.com → http://localhost:8787

# 3. Access > Applications > Add
#    - 이름: Sanjuk Dashboard
#    - 정책: 본인 이메일만 허용 (이메일 OTP)

# 4. GCP에서 커넥터 실행
cloudflared service install <TOKEN>
sudo systemctl enable --now cloudflared
```

**장점:**
- 고정 HTTPS 도메인
- 이메일 OTP 인증 (비밀번호 관리 불필요)
- GCP 방화벽 포트 개방 불필요
- 무료 플랜으로 충분

## 보안 체크리스트

- [x] DB 읽기 전용 연결 (`mode=ro`)
- [x] POST/PUT/DELETE 엔드포인트 없음
- [x] API 응답에 계좌번호/토큰/env 값 미노출
- [x] FastAPI docs/redoc 비활성화
- [x] 기본 바인드 127.0.0.1 (외부 직접 접근 불가)
- [x] Basic Auth 지원 (`DASHBOARD_USER` + `DASHBOARD_PASS`)
- [x] 시각 KST 통일

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DASHBOARD_HOST` | `127.0.0.1` | 바인드 주소 |
| `DASHBOARD_PORT` | `8787` | 포트 |
| `DASHBOARD_USER` | (없음) | Basic Auth 사용자명 |
| `DASHBOARD_PASS` | (없음) | Basic Auth 비밀번호 |

`DASHBOARD_USER`와 `DASHBOARD_PASS` 모두 설정해야 인증 활성화.
