"""
eval_agent.py — QA Eval 파이프라인 최종본
 
변경사항:
- judge_screenshot_quality() 제거
  → 스텝 실행 중 VisionAgent가 이미 confidence 산출 → 중복 호출 불필요
  → step_results의 vision_confidence 평균을 vision_score로 직접 활용
- judge_flow_completion()에 vision_confidence 컨텍스트 추가
  → LLM Judge가 confidence 낮은 스텝 참고하여 더 정확한 판단 가능
 
의존성:
    pip install langfuse google-generativeai
"""
 
import json
import os
from datetime import datetime
from typing import Optional
 
from langfuse import get_client
from google import genai
from dotenv import load_dotenv
from llm_client import build_genai_client
load_dotenv()   
# ── 설정 ─────────────────────────────────────────────────────────────────────
 
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST       = os.getenv("LANGFUSE_HOST", "http://host.docker.internal:3000")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
 
PASS_THRESHOLD = 0.7
FLOW_WEIGHT    = 0.6
VISION_WEIGHT  = 0.4
 
# ── 초기화 ────────────────────────────────────────────────────────────────────
 
# langfuse = Langfuse(
#     public_key=LANGFUSE_PUBLIC_KEY,
#     secret_key=LANGFUSE_SECRET_KEY,
#     host=LANGFUSE_HOST,
# )

langfuse = get_client()


gemini = build_genai_client()
 
# ── Judge 1: 플로우 완료율 (LLM 호출 1회) ────────────────────────────────────
 
def judge_flow_completion(result: dict, flow_span) -> dict:
    steps_summary = "\n".join(
        "  스텝 {step} ({status}): {label} [vision_confidence={conf}]".format(
            step=s["step"],
            status="PASS" if s["passed"] else "FAIL",
            label=s["label"],
            conf=round(s.get("vision_confidence", 1.0), 2),
        )
        for s in result["step_results"]
    )

    prompt_client = langfuse.get_prompt("judge_flow_completion", label="production")
    prompt = prompt_client.compile(
        title=result["title"],
        status=result["status"],
        steps_summary=steps_summary,
    )
    flow_span.update(input={"prompt": prompt}, prompt=prompt_client)

    response = gemini.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    text = response.text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
 
 
# ── Judge 2: vision confidence 평균 (LLM 호출 없음) ──────────────────────────
 
def calc_vision_score(result: dict) -> dict:
    """VisionAgent가 이미 산출한 confidence를 재활용. 별도 LLM 호출 없음."""
    scores = []
    low_confidence_steps = []
 
    for s in result["step_results"]:
        conf = s.get("vision_confidence", 1.0)
        scores.append(conf)
        if conf < 0.6:
            low_confidence_steps.append({
                "step":       s["step"],
                "label":      s["label"],
                "confidence": conf,
            })
 
    avg = round(sum(scores) / len(scores), 3) if scores else 1.0
    return {"score": avg, "low_confidence_steps": low_confidence_steps}
 
 
# ── 메인 eval 파이프라인 ──────────────────────────────────────────────────────

def run_eval(result: dict) -> dict:
    test_id = result.get("test_id", "unknown")
    title   = result.get("title", "")

    with langfuse.start_as_current_observation(
        as_type="span",
        name="qa_eval",
        input={"test_id": test_id, "title": title, "status": result.get("status")},
        metadata={
            "test_id":        test_id,
            "title":          title,
            "start_time":     result.get("start_time", ""),
            "steps_executed": result.get("steps_executed", 0),
            "steps_passed":   result.get("steps_passed", 0),
        },
    ) as eval_span:

        # Judge 1 — 플로우 (LLM 호출)
        with langfuse.start_as_current_observation(
            as_type="span",
            name="judge_flow_completion",
        ) as flow_span:
            flow_result = judge_flow_completion(result, flow_span)
            flow_span.update(output=flow_result)

        # Judge 2 — vision confidence (LLM 호출 없음)
        with langfuse.start_as_current_observation(
            as_type="span",
            name="calc_vision_score",
        ) as vision_span:
            vision_result = calc_vision_score(result)
            vision_span.update(output=vision_result)

        # 종합 점수
        final_score = round(
            flow_result["score"]   * FLOW_WEIGHT +
            vision_result["score"] * VISION_WEIGHT,
            3,
        )

        eval_output = {
            "test_id":   test_id,
            "title":     title,
            "eval_time": datetime.now().isoformat(),
            "flow": {
                "score":        flow_result["score"],
                "reason":       flow_result["reason"],
                "failed_steps": flow_result.get("failed_steps", []),
                "severity":     flow_result.get("severity", "OK"),
            },
            "vision": {
                "score":                vision_result["score"],
                "low_confidence_steps": vision_result["low_confidence_steps"],
            },
            "final_score":       final_score,
            "needs_alert":       final_score < PASS_THRESHOLD,
        }

        eval_span.update(output=eval_output)

        # Langfuse evaluation scores 등록
        trace_id = eval_span.trace_id
        langfuse.create_score(
            trace_id=trace_id,
            name="final_score",
            value=final_score,
            comment=flow_result.get("reason", ""),
        )
        langfuse.create_score(
            trace_id=trace_id,
            name="flow_score",
            value=flow_result["score"],
            comment=f"severity: {flow_result.get('severity', 'OK')}",
        )
        langfuse.create_score(
            trace_id=trace_id,
            name="vision_score",
            value=vision_result["score"],
        )

    return eval_output
 
 
# ── Slack 알림 ────────────────────────────────────────────────────────────────
 
def send_slack_alert(eval_output: dict, webhook_url: str):
    import urllib.request
 
    low_conf = eval_output["vision"]["low_confidence_steps"]
    low_conf_text = (
        "\n".join(
            f"  • 스텝{s['step']} {s['label']} (confidence: {s['confidence']})"
            for s in low_conf
        ) if low_conf else "  • 없음"
    )
    trace_url = f"{LANGFUSE_HOST}/trace/{eval_output['langfuse_trace_id']}"
 
    message = {
        "text": (
            f":warning: *QA Eval 경고* — {eval_output['title']}\n"
            f">종합 점수: *{eval_output['final_score']}* (기준: {PASS_THRESHOLD})\n"
            f">심각도: *{eval_output['flow']['severity']}*\n"
            f">판단 근거: {eval_output['flow']['reason']}\n"
            f">낮은 confidence 스텝:\n{low_conf_text}\n"
            f">Langfuse: {trace_url}"
        )
    }
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(message).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)
 
 
# ── 진입점 ────────────────────────────────────────────────────────────────────
 
def evaluate_result_dict(
    result: dict,
    slack_webhook_url: Optional[str] = None,
) -> dict:
    """QAOrchestrator에서 직접 호출."""
    eval_output = run_eval(result)
    if eval_output["needs_alert"] and slack_webhook_url:
        send_slack_alert(eval_output, slack_webhook_url)
    langfuse.flush()
    return eval_output
 
 
def evaluate_result_file(
    path: str,
    slack_webhook_url: Optional[str] = None,
) -> dict:
    """기존 test_results/*.json 파일로 직접 테스트할 때."""
    with open(path, encoding="utf-8") as f:
        result = json.load(f)
    return evaluate_result_dict(result, slack_webhook_url)
 
 
# ── 직접 실행 테스트 ──────────────────────────────────────────────────────────
 
if __name__ == "__main__":
    sample = {
        "test_id": "TPL_RUN",
        "title": "Guest 로그인 + 계정 연동",
        "status": "PASS",
        "start_time": "2026-03-17 06:24:22.760005",
        "end_time": "2026-03-17 06:24:29.896987",
        "steps_executed": 2,
        "steps_passed": 2,
        "error_message": None,
        "screenshots": [
            "/app/screenshots/screenshot_20260317_062426_183.png",
            "/app/screenshots/screenshot_20260317_062429_720.png",
        ],
        "step_results": [
            {"step": 1, "label": "앱을 실행한다.",         "passed": True,  "vision_confidence": 0.95},
            {"step": 2, "label": "튜토리얼 스킵(치트)을 실행한다.", "passed": True,  "vision_confidence": 0.88},
        ],
        "context": {},
    }
    output = evaluate_result_dict(sample)
    print(json.dumps(output, ensure_ascii=False, indent=2))