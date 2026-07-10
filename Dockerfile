FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# adb: 무선(Wi-Fi) 디바이스를 서버가 직접 제어하기 위해 필요
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    adb \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# api_server 의존 모듈 (로컬 import 클로저)
COPY api_server.py \
     adb_controller.py \
     config.py \
     csv_reporter.py \
     element_cache.py \
     eval_agent.py \
     eval_platform.py \
     langfuse_disabled.py \
     llm_client.py \
     planner_node.py \
     prompts.py \
     setup_prompts.py \
     qa_orchestrator.py \
     sr_debugger.py \
     test_manager.py \
     unity_api_client.py \
     vision_agent.py \
     package_apk_map.json \
     ./

# React 빌드 결과물
COPY frontend/dist ./frontend/dist

# 번들 템플릿 (볼륨 마운트로 덮어써서 영속화 가능)
COPY templates ./templates
COPY game_testcases ./game_testcases

# 데이터 디렉토리
RUN mkdir -p pipelines test_results recordings screenshots screenshots_debug \
    testcases apks reports state

EXPOSE 8000

CMD ["python", "api_server.py"]
