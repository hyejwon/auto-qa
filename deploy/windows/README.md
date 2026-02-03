# Windows QA 팀 - 사용 가이드

## ✅ 필수 요구사항

1. **Docker Desktop** - https://www.docker.com/products/docker-desktop
2. **BlueStacks 5** - https://www.bluestacks.com/ko/index.html
3. **ADB** - https://developer.android.com/studio/releases/platform-tools
4. **7-Zip** - https://www.7-zip.org/

## 📥 초기 설치

### 1. 파일 다운로드
Google Drive에서 다음 파일 다운로드:
- `auto-qa-latest.tar.gz` → `images/` 폴더에 저장

### 2. 설치 실행
```
install.bat 더블클릭
```

### 3. 환경 설정
`.env` 파일 열어서 API 키 입력:
```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. APK 준비
`cooptd.apk` 파일을 `apks/` 폴더에 복사

## 🚀 사용 방법

### QA 테스트 실행
```
1. BlueStacks 실행
2. run_qa.bat 더블클릭
3. 결과 확인: report/ 폴더
```

### Gradio 시각화 앱 실행
```
1. run_gradio.bat 더블클릭
2. 브라우저에서 http://localhost:7860 접속
```

## 🔄 업데이트
```
1. Google Drive에서 최신 auto-qa-latest.tar.gz 다운로드
2. images/ 폴더에 저장 (덮어쓰기)
3. update.bat 더블클릭
```

## 📁 폴더 구조
```
deploy/windows/
├── install.bat           # 초기 설치
├── update.bat            # 업데이트
├── run_qa.bat            # QA 테스트 실행
├── run_gradio.bat        # Gradio 앱 실행
├── docker-compose.yml    # Docker 설정
├── .env                  # 환경 변수 (API 키)
├── images/               # Docker 이미지
├── apks/                 # APK 파일
│   └── cooptd.apk
├── report/               # QA 리포트
├── screenshots/          # 스크린샷
└── logs/                 # 로그
```

## ❓ 문제 해결

### "Docker Desktop이 실행되지 않았습니다"
→ Docker Desktop 아이콘 클릭

### "BlueStacks가 실행되지 않았습니다"
→ BlueStacks 실행

### "APK 파일이 없습니다"
→ cooptd.apk를 apks 폴더에 복사

### "API 키 오류"
→ .env 파일에서 API 키 확인

---

## 4. .gitignore 업데이트
```
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
*.egg-info/

# 환경 변수
.env
.env.local

# 결과물
report/*.md
screenshots/*.png
logs/*.log

# Docker 빌드 결과
export/

# APK 파일
*.apk

# IDE
.vscode/
.idea/
*.swp

# OS
.DS_Store
Thumbs.db

# Windows 배포 폴더의 생성된 파일들
deploy/windows/apks/*
deploy/windows/report/*
deploy/windows/screenshots/*
deploy/windows/logs/*
deploy/windows/images/*.tar
deploy/windows/images/*.tar.gz
deploy/windows/.env

# 배포 폴더 자체는 포함하되, 내용물만 제외
!deploy/windows/.env.example
!deploy/windows/images/README.txt