"""
eval_agent.py — QA Eval 파이프라인 최종본
 
변경사항:
- judge_screenshot_quality() 제거
  → 스텝 실행 중 VisionAgent가 이미 confidence 산출 → 중복 호출 불필요
  → step_results의 vision_confidence 평균을 vision_score로 직접 활용
- judge_flow_completion()에 vision_confidence 컨텍스트 추가
  → LLM Judge가 confidence 낮은 스텝 참고하여 더 정확한 판단 가능
 
의존성:
    pip install google-generativeai
"""
 
import json
from datetime import datetime
from typing import Optional
 
from langfuse_disabled import get_client
from google import genai
from dotenv import load_dotenv
from llm_client import build_genai_client
load_dotenv()   
PASS_THRESHOLD = 0.7
FLOW_WEIGHT    = 0.6
VISION_WEIGHT  = 0.4
 
langfuse = get_client()


gemini = build_genai_client()
 
# ── Judge 1: 플로우 완료율 (LLM 호출 1회) ────────────────────────────────────

def step_outcome(step: dict) -> str:
    """리포트와 동일한 우선순위로 스텝 상태를 삼분화한다."""
    if step.get("skipped"):
        return "SKIP"
    return "PASS" if step.get("passed") else "FAIL"


def build_steps_summary(result: dict) -> str:
    return "\n".join(
        "  스텝 {step} ({status}): {label} [vision_confidence={conf}]".format(
            step=step["step"],
            status=step_outcome(step),
            label=step["label"],
            conf=round(step.get("vision_confidence", 1.0), 2),
        )
        for step in result["step_results"]
    )


def judge_flow_completion(result: dict, flow_span) -> dict:
    steps_summary = build_steps_summary(result)

    prompt = f"""당신은 모바일 게임 QA 전문가입니다.
아래는 테스트 시나리오 실행 결과입니다.

시나리오 제목: {result["title"]}
전체 상태: {result["status"]}
실행 스텝 (vision_confidence는 UI 요소 탐지 신뢰도):
{steps_summary}

채점 기준:
- SKIP은 선택·조건부 스텝이 현재 화면에 필요하지 않아 정상 건너뛴 상태다. 실패로
  세거나 감점하거나 failed_steps에 넣지 않는다.
- 1.0 : 모든 스텝 완료, vision_confidence 전반적으로 높음
- 0.7~0.9 : 핵심 플로우 완료, 일부 스텝 실패 또는 confidence 낮음
- 0.4~0.6 : 핵심 플로우 중 중요 스텝 실패
- 0.0~0.3 : 시나리오 목적 달성 불가 수준

반드시 아래 JSON 형식으로만 응답하세요:
{{
  "score": 0.0~1.0,
  "reason": "판단 근거 1~2문장",
  "failed_steps": [실패한 스텝 번호 리스트],
  "severity": "CRITICAL|WARNING|OK"
}}"""
    flow_span.update(input={"prompt": prompt})

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
        if s.get("skipped"):
            continue
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
            actual_failed_steps = {
                int(step["step"])
                for step in result["step_results"]
                if not step.get("passed")
                and not step.get("skipped")
                and isinstance(step.get("step"), int)
            }
            skipped_steps = {
                int(step["step"])
                for step in result["step_results"]
                if step.get("skipped") and isinstance(step.get("step"), int)
            }
            reported_failed_steps: set[int] = set()
            for value in flow_result.get("failed_steps") or []:
                try:
                    reported_failed_steps.add(int(value))
                except (TypeError, ValueError):
                    continue

            skip_only_misclassification = (
                result.get("status") == "PASS"
                and not actual_failed_steps
                and bool(reported_failed_steps)
                and reported_failed_steps.issubset(skipped_steps)
            )
            flow_result["failed_steps"] = sorted(
                reported_failed_steps & actual_failed_steps
            )
            if skip_only_misclassification:
                flow_result.update(
                    {
                        "score": 1.0,
                        "reason": (
                            "필수 스텝 모두 통과, 선택·조건부 스텝 "
                            f"{len(skipped_steps)}건 정상 건너뜀"
                        ),
                        "severity": "OK",
                    }
                )
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
    message = {
        "text": (
            f":warning: *QA Eval 경고* — {eval_output['title']}\n"
            f">종합 점수: *{eval_output['final_score']}* (기준: {PASS_THRESHOLD})\n"
            f">심각도: *{eval_output['flow']['severity']}*\n"
            f">판단 근거: {eval_output['flow']['reason']}\n"
            f">낮은 confidence 스텝:\n{low_conf_text}"
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
