#!/bin/bash
set -e

# 설정
IMAGE_NAME="auto-qa"
VERSION="${1:-latest}"
EXPORT_DIR="../../export"
TAR_FILE="${EXPORT_DIR}/${IMAGE_NAME}-${VERSION}.tar"
GZ_FILE="${TAR_FILE}.gz"

echo "=========================================="
echo "Auto QA - Docker 이미지 빌드 및 내보내기"
echo "=========================================="
echo "이미지: ${IMAGE_NAME}"
echo "버전: ${VERSION}"
echo "플랫폼: linux/amd64 (Windows 호환)"
echo "=========================================="

# 프로젝트 루트로 이동
cd "$(dirname "$0")/../.."

# 1. 빌드
echo
echo "[1/4] Docker 이미지 빌드 중..."
docker buildx build \
    --platform linux/amd64 \
    -t ${IMAGE_NAME}:${VERSION} \
    --load \
    .

if [ $? -ne 0 ]; then
    echo "[ERROR] 빌드 실패"
    exit 1
fi

# 2. latest 태그 추가
if [ "$VERSION" != "latest" ]; then
    echo
    echo "[2/4] latest 태그 추가..."
    docker tag ${IMAGE_NAME}:${VERSION} ${IMAGE_NAME}:latest
fi

# 3. tar 파일로 내보내기
echo
echo "[3/4] tar 파일로 내보내기..."
mkdir -p ${EXPORT_DIR}
docker save ${IMAGE_NAME}:latest -o ${TAR_FILE}

if [ $? -ne 0 ]; then
    echo "[ERROR] 내보내기 실패"
    exit 1
fi

# 4. gzip 압축
echo
echo "[4/4] gzip 압축 중..."
gzip -f ${TAR_FILE}

# 파일 정보 출력
echo
echo "=========================================="
echo "[SUCCESS] 완료!"
echo "=========================================="
echo "생성된 파일:"
ls -lh ${GZ_FILE}
echo "=========================================="
echo
echo "다음 단계:"
echo "1. ${GZ_FILE} 파일을"
echo "   Google Drive에 업로드"
echo "   → qa-automation/images/ 폴더"
echo "2. QA 팀에게 Slack 공지"
echo

# Finder에서 export 폴더 열기
open ${EXPORT_DIR}