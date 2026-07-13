# 사내 서버 세팅 검증 절차 (Windows)

사내 컴퓨터를 qa-auto 중앙 서버로 세팅한 뒤, 아래를 순서대로 통과하면 운영 시작 가능.
예상 소요: 30~40분.

## 준비물

- [ ] 서버로 쓸 Windows PC (상시 켜둘 수 있는 공용 PC, 고정 IP 권장)
- [ ] 테스트 폰 1대 (Android, 개발자 옵션 활성화) + USB 케이블
- [ ] LLM Gateway 토큰
- [ ] 같은 사내망의 다른 자리 PC 1대 (테스터 접속 확인용)

---

## 1단계 — 기본 도구 확인

서버 PC의 명령창(cmd)에서:

```bat
python --version    → Python 3.12.x
git --version       → 아무 버전이나 OK
adb version         → Android Debug Bridge version ...
```

| 실패 항목 | 조치 |
|---|---|
| python | https://www.python.org/downloads/release/python-3129/ — 설치 시 "Add python.exe to PATH" 체크 |
| git | https://git-scm.com/download/win |
| adb | https://developer.android.com/tools/releases/platform-tools 압축 해제 후 폴더를 PATH에 추가 |

## 2단계 — 코드 받기 + 서버 세팅

```bat
git clone https://github.com/hyejwon/auto-qa.git qa-auto
cd qa-auto
git checkout dev
```

`setup_server.bat` **우클릭 → 관리자 권한으로 실행**

- [ ] 메모장이 열리면 `LLM_GATEWAY_TOKEN` 채우고 저장 (`ADB_DEVICE`, `UNITY_API_URL`은 빈 값 유지)
- [ ] "방화벽 인바운드 8000 허용 등록 완료" 출력 확인
- [ ] "절전/최대 절전 해제 완료" 출력 확인
- [ ] 자동 시작 등록 여부 선택 (운영용이면 y 권장)
- [ ] 서버 시작 후, 서버 PC 브라우저에서 `http://localhost:8000` → UI가 뜨는지
- [ ] `http://localhost:8000/health` → `{"status":"ok",...}` 응답

**통과 기준**: 로컬에서 UI와 health 응답 확인.

## 3단계 — 네트워크 검증 ★ 가장 중요 (여기서 인프라 성패 판정)

### 3-1. 서버 → 폰

1. 폰: 개발자 옵션 → USB 디버깅 ON → 서버 PC에 USB로 연결 (RSA 팝업 "항상 허용")
2. 서버 PC에서:

```bat
adb tcpip 5555
adb shell ip route     ← 출력 끝 "src" 뒤가 폰 IP (메모)
```

3. USB를 뽑고:

```bat
adb connect 폰IP:5555   → "connected to ..." 이면 통과
adb devices             → 폰IP:5555  device
```

**실패 시**: 폰이 게스트 Wi-Fi인지 확인 → 직원용 Wi-Fi로 변경. 그래도 안 되면 서버↔Wi-Fi 대역 간 5555 차단(VLAN/방화벽) 가능성 → IT에 "서버IP → Wi-Fi대역 TCP 5555 허용" 요청. **이게 안 뚫리면 무선 구조 자체가 불가하므로 여기서 중단하고 IT 협의.**

### 3-2. 테스터 PC → 서버

다른 자리 PC 브라우저에서 `http://서버IP:8000`

- [ ] UI가 뜨면 통과

**실패 시**: 서버 PC에서 `ipconfig`로 IP 재확인, 방화벽 규칙 확인:
`netsh advfirewall firewall show rule name="qa-auto-server-8000"`

## 4단계 — E2E 기능 검증 (테스터 자리 PC에서 진행)

- [ ] 헤더 **"IP로 연결"** → 3-1에서 메모한 폰 IP 입력 → 연결 성공 메시지
- [ ] 디바이스 드롭다운에 폰 모델명 표시 + 선택
- [ ] 게임 선택 화면에 폰에 설치된 게임 목록 표시
- [ ] 템플릿 1개 선택 → 실행 → 실시간 로그 출력 → PASS/FAIL 결과 화면까지 도달
- [ ] (실행 도중) 드롭다운에 "실행 중" 표시 확인
- [ ] (실행 도중) 같은 폰으로 실행 재시도 → "다른 테스트가 사용 중" 거부 확인

**통과 기준**: 테스트 1건이 결과 리포트까지 완주.

## 5단계 — 운영 안정성 검증

- [ ] **자동 재연결**: 폰 Wi-Fi를 껐다가 다시 켬 → 1분 내 디바이스 목록에 자동 복귀
- [ ] **서버 자동 복구**: 작업 관리자에서 python 프로세스 강제 종료 → run_server 창이 5초 후 자동 재시작
- [ ] **재부팅 생존** (자동 시작 등록한 경우): 서버 PC 재부팅 → 로그온 → 서버 자동 실행 → 테스터 PC에서 접속 확인
- [ ] **폰 재부팅 시나리오**: 폰 재부팅 → 무선 디버깅이 풀리는 기종인지 확인. 풀리면 `adb tcpip 5555` 재실행 필요 — 테스터 안내사항에 포함 (USER_SETUP.md 상단 참고)

## 6단계 — 다중 사용자 검증 (팀원 1명과 함께)

- [ ] 두 번째 폰을 각자 자리에서 웹으로 등록
- [ ] 서로 다른 폰으로 **동시에** 테스트 실행 → 둘 다 정상 완료
- [ ] 각자 로그/결과가 섞이지 않는지 확인

---

## 최종 판정

| 단계 | 결과 | 비고 |
|---|---|---|
| 1. 기본 도구 | ☐ | |
| 2. 서버 세팅 | ☐ | |
| 3. 네트워크 | ☐ | 실패 시 IT 협의 필요 |
| 4. E2E | ☐ | |
| 5. 운영 안정성 | ☐ | |
| 6. 다중 사용자 | ☐ | |

6단계까지 통과하면: 서버 IP를 팀에 공유하고 `USER_SETUP.md` 상단 "테스터용" 섹션대로 온보딩 시작.
운영 중 이슈는 `docs/deployment-guide.md` 트러블슈팅 참고.
