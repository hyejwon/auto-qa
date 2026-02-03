FROM python:3.12-slim

# -------------------------------------------------------------------
# 1. 시스템 의존성 설치 (ADB + 기본 도구)
# -------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        android-tools-adb \
        wget \
        curl \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------------------
# 2. 작업 디렉토리 설정
# -------------------------------------------------------------------
WORKDIR /app

# -------------------------------------------------------------------
# 3. Python 의존성 설치 (레이어 캐싱 활용)
# -------------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# -------------------------------------------------------------------
# 4. 소스 코드 복사
# -------------------------------------------------------------------
COPY docker_entrypoint.sh /usr/local/bin/docker_entrypoint.sh
COPY qa_agent/ qa_agent/
COPY main.py .
COPY gradio_app.py .
COPY mcp_server.py .
COPY test_cases.yaml .

# -------------------------------------------------------------------
# 5. 런타임 디렉토리 생성
# -------------------------------------------------------------------
RUN mkdir -p /app/reports /app/screenshots /app/screenshots_debug

# -------------------------------------------------------------------
# 6. 환경 변수 기본값 (런타임에 오버라이드 가능)
# -------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1
ENV QA_DEVICE=host.docker.internal:5555
ENV QA_PACKAGE=com.percent.aos.cooptd
ENV QA_MAX_STEPS=100
ENV QA_MCP_DEBUG=0

# -------------------------------------------------------------------
# 7. Gradio 포트 노출
# -------------------------------------------------------------------
EXPOSE 7860

# -------------------------------------------------------------------
# 8. 엔트리포인트
# -------------------------------------------------------------------
RUN chmod +x /usr/local/bin/docker_entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker_entrypoint.sh"]
CMD ["python", "gradio_app.py"]
