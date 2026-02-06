FROM python:3.12-slim

LABEL maintainer="automation-team@company.com"
LABEL description="Auto QA Agent with LLM"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# 필수 패키지 설치
RUN apt-get update && apt-get install -y \
    android-tools-adb \
    aapt \
    ffmpeg \
    wget \
    curl \
    unzip \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사
COPY qa_agent/ ./qa_agent/
COPY main.py .
COPY mcp_server.py .
COPY gradio_app.py .

# Entrypoint 스크립트 복사
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 결과 저장 디렉토리 생성
RUN mkdir -p /app/report /app/screenshots /app/logs /app/apks /app/recordings

# 실행 권한
RUN chmod +x main.py mcp_server.py gradio_app.py 

# Entrypoint 설정
ENTRYPOINT ["/entrypoint.sh"]

# 기본 명령어 (main.py 실행)
CMD ["python", "main.py"]
