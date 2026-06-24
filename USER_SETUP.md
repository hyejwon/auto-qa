# qa-auto 설치 가이드 (각 사용자 PC)

**Docker / Node 불필요.** 본인 PC에서 `git` 으로 받아 Python 으로 바로 실행하고,
본인 PC에 연결된 디바이스/LDPlayer 로 테스트합니다. 웹 UI 도 같은 프로그램이 띄워줍니다.

```
git clone → run_app 실행 → 브라우저 http://localhost:8000 → 내 디바이스로 테스트
```

---

## 0. 사전 준비물 (최초 1회)

| 항목 | 설명 |
|------|------|
| **Python 3.12** | https://www.python.org/downloads/release/python-3129/ — 설치 시 **"Add python.exe to PATH" 체크** |
| **Git** | 코드 받기/업데이트용 |
| **ADB (platform-tools)** | LDPlayer 사용 시 보통 내장. `adb version` 이 되면 OK. 안 되면 platform-tools 의 adb 경로를 PATH 에 추가 |
| **디바이스 / LDPlayer** | 테스트할 에뮬레이터 또는 USB 디바이스 |

> Python 외에 Node.js, Docker 는 **필요 없습니다.** (웹 UI 는 빌드된 상태로 함께 배포됩니다)

---

## 1. 코드 받기 (최초 1회)

```bash
git clone <레포주소> qa-auto
cd qa-auto
```

## 2. 실행

- **Windows**: `run_app.bat` 더블클릭
- **Mac/Linux**: `./run_app.sh`

스크립트가 자동으로 ① 가상환경 생성 ② 의존성 설치 ③ 서버 실행 ④ 브라우저 오픈 까지 합니다.

처음 실행하면 `.env` 가 없어서 `.env.example` 이 복사되고 편집창이 열립니다. 아래만 채우면 됩니다:

```ini
LLM_GATEWAY_TOKEN=발급받은_토큰값        # 사내 LLM gateway 토큰
ADB_DEVICE=127.0.0.1:5555               # LDPlayer 기본. 실디바이스면 adb devices 의 ID
```

저장 후 `run_app` 을 다시 실행하면 브라우저가 **http://localhost:8000** 으로 열립니다.

> 실행 창은 **켜둔 채로** 사용하세요. 창을 닫으면 종료됩니다.

---

## 3. 업데이트 (코드 변경 반영)

exe 재배포 / Docker 재빌드 불필요. 변경이 있을 때:

```bash
git pull
```

후 `run_app` 다시 실행하면 끝. (의존성이 바뀌면 자동 재설치, UI 변경분도 함께 반영)

---

## 4. 문제 해결

| 증상 | 조치 |
|------|------|
| 브라우저가 검정/빈 화면 | `http://localhost:8000` 인지 확인 (https 아님). 실행 창에 에러 없는지 확인 |
| `디바이스 미연결` | `adb devices` 로 디바이스가 `device` 상태인지 확인. LDPlayer 면 `adb connect 127.0.0.1:5555` |
| `adb 못 찾음` 경고 | platform-tools 폴더를 시스템 PATH 에 추가 후 새 터미널 |
| Python 못 찾음 | Python 3.12 설치 시 "Add to PATH" 체크. 이미 설치했으면 재로그인 |
| 포트 8000 사용 중 | 다른 프로그램이 8000 을 쓰는 경우. 해당 프로그램 종료 후 재실행 |

---

## (개발자용) 프론트엔드 수정 시

UI 코드(`frontend/`)를 고쳤다면 빌드 결과물을 함께 커밋해야 사용자에게 반영됩니다:

```bash
cd frontend && npm install && npm run build   # → frontend/dist 갱신
cd .. && git add frontend/dist && git commit   # dist 변경분 커밋
```

사용자는 `git pull` 만 하면 새 UI 가 적용됩니다 (Node 설치 불필요).
