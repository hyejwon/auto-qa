"""사내 LLM Gateway genai.Client 팩토리"""
import os
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

def build_genai_client() -> genai.Client:
    gateway_url = os.getenv("LLM_GATEWAY_URL", "https://llm-gateway.111percent.net/llm/google")
    gateway_token = os.getenv("LLM_GATEWAY_TOKEN", "")

    return genai.Client(
            api_key=gateway_token,
            http_options=types.HttpOptions(base_url=gateway_url),
        )