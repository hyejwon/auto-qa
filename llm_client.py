"""사내 LLM Gateway 또는 Vertex AI 용 genai.Client 팩토리"""
import os
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


def build_genai_client() -> genai.Client:
    """
    환경변수 우선순위:
      1. LLM_GATEWAY_URL + LLM_GATEWAY_TOKEN  → 사내 게이트웨이
      2. GOOGLE_CLOUD_PROJECT (또는 GCP_PROJECT) → Vertex AI 직접 연결
    """
    gateway_url = os.getenv("LLM_GATEWAY_URL", "https://llm-gateway.111percent.net/llm/google")
    gateway_token = os.getenv("LLM_GATEWAY_TOKEN", "")

    if gateway_token:
        logger.info(f"[LLM] 사내 게이트웨이 사용: {gateway_url}")
        return genai.Client(
            api_key=gateway_token,
            http_options=types.HttpOptions(base_url=gateway_url),
        )

    # fallback: Vertex AI
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT", "percent-vertex-test")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    logger.info(f"[LLM] Vertex AI 사용: project={project}, location={location}")
    return genai.Client(vertexai=True, project=project, location=location)
