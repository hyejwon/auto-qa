# Mac 개발팀 - 빌드 가이드

## 빌드 및 배포

### 1. 빌드 실행
```bash
cd deploy/mac
chmod +x build.sh
./build.sh                # latest 버전
./build.sh v1.0.0        # 특정 버전
```

### 2. Google Drive 업로드

1. export 폴더가 자동으로 열림
2. `auto-qa-latest.tar.gz` 파일을 Google Drive에 업로드
3. 업로드 위치: `qa-automation/images/`

### 3. QA 팀 공지

Slack #qa-automation 채널에 업데이트 공지

## 로컬 테스트
```bash
# 프로젝트 루트에서
docker-compose up

# 또는 특정 명령 실행
docker-compose run --rm auto-qa python main.py
docker-compose run --rm auto-qa python gradio_app.py
```

## 문제 해결

### 빌드 실패
```bash
# Docker Desktop 재시작
# buildx 확인
docker buildx ls
```

### 이미지 크기가 너무 큼
```bash
# 불필요한 패키지 제거
# requirements.txt 최적화
```