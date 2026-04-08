FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY orchestrator_server.py \
     config.py \
     planner_node.py \
     llm_client.py \
     setup_prompts.py \
     ./

# React 빌드 결과물
COPY frontend/dist ./frontend/dist

# 데이터 디렉토리
RUN mkdir -p templates pipelines test_results recordings

EXPOSE 8000

CMD ["python", "orchestrator_server.py"]
