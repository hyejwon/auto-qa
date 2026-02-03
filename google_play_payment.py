import asyncio
import json
import subprocess
from typing import List, Dict, Any, Optional

from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _format_elements_for_llm(elements: List[Dict[str, Any]]) -> str:
    """UIAutomator 요소 리스트를 LLM이 읽기 쉬운 텍스트로 포맷."""
    lines = []
    for i, e in enumerate(elements):
        text = e.get("text") or ""
        desc = e.get("content_desc") or ""
        rid = e.get("resource_id") or ""
        cls = e.get("class_name") or ""
        clickable = e.get("clickable", False)
        center = e.get("center", {})
        cx, cy = center.get("x", "?"), center.get("y", "?")

        lines.append(
            f"[{i}] text='{text}' desc='{desc}' "
            f"rid='{rid}' class='{cls}' clickable={clickable} "
            f"center=({cx},{cy})"
        )
    return "\n".join(lines)


def _parse_llm_response(text: str) -> Optional[Dict[str, Any]]:
    """LLM이 준 JSON 문자열을 dict로 파싱 (앞뒤 잡소리 방어)."""
    try:
        if "```" in text:
            parts = text.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("{") and p.endswith("}"):
                    text = p
                    break
        return json.loads(text)
    except Exception:
        return None


async def analyze_and_click_payment_button(
    elements: List[Dict[str, Any]],
    device_id: str,
) -> Dict[str, Any]:
    """Google Play 결제 화면에서 결제/구독 버튼을 LLM으로 찾아 클릭.

    elements: dump_ui_automator_impl() 에서 받은 elements 리스트
    """
    print("\n" + "=" * 60)
    print("GOOGLE PLAY PAYMENT ANALYSIS (LLM)")
    print("=" * 60)

    if not client:
        print("[ERROR] OPENAI_API_KEY not set; skipping LLM payment button selection")
        return {
            "success": False,
            "error": "OPENAI_API_KEY not set",
            "analysis_method": "none",
        }

    clickable_elements = [e for e in elements if e.get("clickable", False)]
    if not clickable_elements:
        print("[ERROR] No clickable elements found")
        return {
            "success": False,
            "error": "No clickable elements on screen",
            "analysis_method": "none",
        }

    print(f"[INFO] Found {len(clickable_elements)} clickable elements")

    elements_description = _format_elements_for_llm(clickable_elements)
    print("\n[LLM] Analyzing elements...")
    print(f"[DEBUG] Elements sent to LLM:\n{elements_description[:500]}...")

    prompt = f"""You are analyzing a Google Play payment screen to identify the payment/purchase/subscribe button.

Here are all the clickable elements on the screen:

{elements_description}

Your task:
1. Identify which element is the PRIMARY payment/purchase/subscribe button
2. Look for buttons with text like: "구매", "구매하기", "확인", "구독", "Subscribe", "Purchase", "Buy", "1-tap buy", "Confirm", etc.
3. Prioritize buttons that clearly indicate payment confirmation
4. Avoid cancel/back buttons

Respond with ONLY a JSON object (no markdown, no extra text):
{{
  "button_index": <index number 0 to {len(clickable_elements)-1}>,
  "confidence": <"high" or "medium" or "low">,
  "reason": "<brief explanation why you selected this button>"
}}

If no suitable payment button found, respond:
{{
  "button_index": -1,
  "confidence": "none",
  "reason": "No clear payment button found"
}}"""

    try:
        def _call_llm() -> str:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0,
            )
            return resp.choices[0].message.content.strip()

        response_text = await asyncio.to_thread(_call_llm)
        print(f"[LLM] Raw response: {response_text[:200]}")

        analysis = _parse_llm_response(response_text)
        if analysis is None:
            return {
                "success": False,
                "error": "Failed to parse LLM response",
                "analysis_method": "llm_parsing_failed",
            }

        button_index = analysis.get("button_index", -1)
        confidence = analysis.get("confidence", "none")
        reason = analysis.get("reason", "Unknown")

        print(f"\n[ANALYSIS] Button Index: {button_index}")
        print(f"[ANALYSIS] Confidence: {confidence}")
        print(f"[ANALYSIS] Reason: {reason}")

        if button_index < 0 or button_index >= len(clickable_elements):
            print(f"[ERROR] Invalid button index: {button_index}")
            return {
                "success": False,
                "error": f"Invalid button index: {button_index}",
                "analysis_method": "llm_invalid_index",
            }

        selected_button = clickable_elements[button_index]
        button_text = selected_button.get("text", "") or selected_button.get(
            "content_desc", ""
        )
        center = selected_button.get("center", {})
        center_x = center.get("x", 0)
        center_y = center.get("y", 0)

        print(f"\n[SELECTED] Button: '{button_text}'")
        print(f"[POSITION] Center: ({center_x}, {center_y})")

        if not center_x or not center_y:
            print("[ERROR] Invalid button coordinates")
            return {
                "success": False,
                "error": "Invalid button coordinates",
                "analysis_method": "llm_invalid_coords",
            }

        print(f"[CLICK] Tapping at ({center_x}, {center_y}) on {device_id}...")
        subprocess.run(
            [
                "adb",
                "-s",
                device_id,
                "shell",
                "input",
                "tap",
                str(int(center_x)),
                str(int(center_y)),
            ]
        )

        return {
            "success": True,
            "button_text": button_text,
            "button_index": button_index,
            "confidence": confidence,
            "reason": reason,
            "position": {"x": center_x, "y": center_y},
            "analysis_method": "llm_analysis",
        }

    except Exception as e:
        print(f"[ERROR] LLM or click failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "analysis_method": "llm_exception",
        }
