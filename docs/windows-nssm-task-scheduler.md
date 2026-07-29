# Windows NSSM + Task Scheduler Setup

이 문서는 Windows PC 재부팅 후 qa-auto를 자동 복구하는 운영 절차입니다.

## 구성

- `qa-auto-backend`: NSSM 서비스. `run_server.bat`을 실행해 Python API 서버를 유지합니다.
- `qa-auto-frontend-dev`: 선택 NSSM 서비스. 프론트엔드 개발 서버가 필요할 때만 `npm run dev`를 실행합니다.
- `qa-auto-adb-startup`: 작업 스케줄러. Windows 로그온 1분 후 `adb_start.bat`을 실행합니다.

운영용으로는 보통 프론트엔드 dev 서버가 필요 없습니다. `frontend/dist`가 최신이면 Python 서버가 `http://localhost:8000`에서 UI까지 함께 제공합니다.

## 사전 준비

관리자 CMD 또는 PowerShell에서 확인합니다.

```bat
python --version
adb version
nssm version
```

`nssm`이 PATH에 없다면 실행 전에 지정합니다.

```bat
set NSSM_EXE=C:\path\to\nssm.exe
```

## 등록

백엔드 서비스와 ADB 작업 스케줄러만 등록:

```bat
cd /d C:\path\to\auto-qa
install_nssm_services.bat
```

프론트엔드 개발 서버까지 NSSM으로 등록:

```bat
cd /d C:\path\to\auto-qa
install_nssm_services.bat --frontend-dev
```

## 확인

```bat
nssm status qa-auto-backend
sc query qa-auto-backend
schtasks /Query /TN "qa-auto-adb-startup" /V /FO LIST
```

프론트 dev 서버를 등록한 경우:

```bat
nssm status qa-auto-frontend-dev
sc query qa-auto-frontend-dev
```

브라우저 확인:

```text
http://localhost:8000
http://localhost:8000/health
```

프론트 dev 서버를 등록한 경우:

```text
http://localhost:3001
```

## 로그

```text
logs\backend.out.log
logs\backend.err.log
logs\frontend.out.log
logs\frontend.err.log
logs\adb_start.log
```

## 기존 작업 스케줄러와 충돌 주의

기존 `setup_server.bat`으로 `qa-auto-server` 작업을 등록해둔 상태라면, NSSM의 `qa-auto-backend`와 동시에 서버를 띄워 포트 `8000`이 충돌할 수 있습니다. NSSM 구성을 사용할 때는 기존 `qa-auto-server` 자동 시작 작업을 비활성화하거나 삭제하세요.

```bat
schtasks /Change /TN "qa-auto-server" /DISABLE
```

기존 watchdog 작업도 계속 사용할 필요가 낮습니다. NSSM이 프로세스 종료 시 재시작을 담당합니다.

```bat
schtasks /Change /TN "qa-auto-watchdog" /DISABLE
```
