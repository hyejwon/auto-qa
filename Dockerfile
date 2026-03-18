FROM python:3.12-slim

LABEL description="QA 자동화 NiceGUI 앱"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# ADB + ffmpeg 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    android-tools-adb \
    ffmpeg \
    curl \
    sqlite3 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 의존성 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드
COPY adb_controller.py \
     config.py \
     element_cache.py \
     eval_agent.py \
     planner_node.py \
     qa_orchestrator.py \
     test_manager.py \
     unity_api_client.py \
     vision_agent.py \
     main_nicegui.py \
     ./

# 데이터 디렉토리
RUN mkdir -p screenshots screenshots_debug test_results testcases recordings templates apks

EXPOSE 7860

CMD ["python", "main_nicegui.py"]
