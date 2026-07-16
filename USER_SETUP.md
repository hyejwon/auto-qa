# qa-auto 설치 가이드

## 테스터용 — 중앙 서버 사용 (설치 불필요, 권장)

중앙 서버가 떠 있는 경우 **본인 PC에 아무것도 설치하지 않습니다.** 폰의 무선 디버깅만 켜고 브라우저로 사용합니다.

### 최초 1회: 폰 무선 디버깅 켜기

1. 폰: 설정 → 개발자 옵션 → **USB 디버깅** ON
2. 폰을 아무 PC에나 USB로 꽂고 (RSA 허용 팝업 → "항상 허용" 체크):

```bash
adb tcpip 5555
```

> 이후에는 USB를 뽑아도 됩니다. 단, **폰을 재부팅하면 풀리므로** 이 명령을 다시 실행해야 합니다.

### 폰 IP 확인 (셋 중 편한 방법)

| 방법 | 위치 |
|------|------|
| 무선 디버깅 화면 | 설정 → 개발자 옵션 → 무선 디버깅 — `IP 주소 및 포트` 표시 (**IP만 사용**, 표시된 포트는 무시) |
| Wi-Fi 설정 | 설정 → Wi-Fi → 연결된 네트워크 상세 → IP 주소 |
| USB 연결 상태에서 | `adb shell ip route` → 출력 끝의 `src` 뒤가 폰 IP |

### 사용

1. 브라우저 → `http://서버IP:8000`
2. 헤더의 **"IP로 연결"** → 폰 IP 입력 (포트 생략 시 5555)
3. 디바이스 드롭다운에서 내 폰 선택 → 게임 선택 → 테스트 실행

주의사항:
- 폰이 **사내 Wi-Fi**(게스트망 ❌)에 연결되어 있어야 서버가 접근할 수 있습니다.
- 연결이 끊기면 서버가 30초마다 자동 재연결을 시도합니다. 계속 오프라인이면 폰 재부팅 후 무선 디버깅이 풀렸는지 확인하세요 (`adb tcpip 5555` 재실행).
- 자주 쓰는 QA 폰은 공유기에서 **MAC 기반 고정 IP**를 걸어두면 IP가 바뀌지 않아 재등록이 필요 없습니다.
  단, 폰 Wi-Fi 설정이 **"랜덤 MAC"이면 예약이 무의미** — 해당 네트워크에 대해 "기기 MAC"으로 바꾼 뒤 예약하세요 (상세: docs/deployment-guide.md "폰 IP 고정").
- 다른 사람이 내 폰으로 테스트 중이면 드롭다운에 "실행 중"으로 표시되고 실행이 거부됩니다. 테스트 중에는 폰 화면을 만지지 마세요.

---

## 로컬 실행용 — 본인 PC에서 직접 띄우기 (중앙 서버 없이)

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

## 3. PC 재부팅 후 다시 시작 (Windows)

Windows 를 재부팅하면 **실행 창(서버)이 닫히고 에뮬레이터/디바이스 연결도 끊깁니다.** 재부팅 후에는 **디바이스를 먼저 연결한 뒤** `run_app.bat` 을 실행하세요.

### A. LDPlayer(에뮬레이터) 를 쓰는 경우

1. **LDPlayer 를 먼저 실행** — 홈 화면이 완전히 뜰 때까지 기다립니다.
2. **`run_app.bat` 더블클릭** — 이미 설치가 끝난 상태라 가상환경·의존성은 건너뛰고 바로 서버가 뜹니다.
3. 상단에 `디바이스 미연결` 이 뜨면 명령창(cmd)에서 다시 연결:

   ```bash
   adb connect 127.0.0.1:5555
   ```

   그래도 안 되면 `adb kill-server && adb start-server` 후 다시 `adb connect`.

### B. 실제 디바이스(USB) 를 쓰는 경우

USB 디바이스는 `adb connect` 가 **필요 없습니다.** (`adb connect` 는 에뮬레이터/Wi-Fi 용)

1. **USB 케이블을 다시 꽂습니다.** (재부팅 중 뽑혔거나 인식이 끊겼을 수 있음)
2. 디바이스 화면에 **"USB 디버깅을 허용하시겠습니까?"** 팝업이 뜨면 **"이 컴퓨터에서 항상 허용"** 체크 후 **허용** 을 누릅니다.
3. 명령창(cmd)에서 인식됐는지 확인:

   ```bash
   adb devices
   # List of devices attached
   # ABCD1234   device      ← 이 상태여야 정상 (unauthorized / offline 이면 아래 참고)
   ```

4. `device` 상태를 확인했으면 **`run_app.bat` 더블클릭.**
5. 안 잡히면 케이블/포트를 바꿔 꽂거나, `adb kill-server && adb start-server` 후 다시 `adb devices`.

> - `unauthorized` : 디바이스 화면의 USB 디버깅 허용 팝업을 아직 안 눌렀거나 취소한 상태 → 케이블 다시 꽂고 팝업에서 허용.
> - `offline` : `adb kill-server && adb start-server` 후 재확인.
> - `.env` 의 `ADB_DEVICE` 값은 `adb devices` 에 나오는 디바이스 ID(예: `ABCD1234`) 로 되어 있어야 합니다. (USB 는 IP:포트가 아님)

> **공통 주의:** 반드시 디바이스(LDPlayer/USB)를 먼저 연결하고 `run_app.bat` 을 실행하세요. 서버부터 켜면 디바이스가 안 잡힐 수 있습니다.
> 재부팅 후에는 `git pull` 이 필요 없습니다. 코드 변경분을 받고 싶을 때만 아래 4번을 참고하세요.

---

## 4. 업데이트 (코드 변경 반영)

exe 재배포 / Docker 재빌드 불필요. 변경이 있을 때:

```bash
git pull
```

후 `run_app` 다시 실행하면 끝. (의존성이 바뀌면 자동 재설치, UI 변경분도 함께 반영)

---

## 5. 문제 해결

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
