from typing import Dict, Any, List, Tuple
import json


# 도구별 한글 액션 설명
TOOL_ACTION_DESCRIPTIONS = {
    "mobile_click_on_screen_at_coordinates": "화면 탭",
    "mobile_type_keys": "텍스트 입력",
    "mobile_swipe_on_screen": "스와이프",
    "mobile_press_button": "버튼 누름",
    "mobile_take_screenshot": "스크린샷 촬영",
    "mobile_list_elements_on_screen": "화면 요소 분석",
    "mobile_launch_app": "앱 실행",
    "mobile_terminate_app": "앱 종료",
    "mobile_open_url": "URL 열기",
    "mobile_long_press_on_screen_at_coordinates": "길게 누름",
    "mobile_double_tap_on_screen": "더블 탭",
    "mobile_get_screen_size": "화면 크기 확인",
    "mobile_list_apps": "앱 목록 조회",
    "mobile_install_app": "앱 설치",
    "mobile_uninstall_app": "앱 삭제",
}


def _safe_str(value) -> str:
    """모든 값을 안전하게 문자열로 변환"""
    if isinstance(value, list):
        return " ".join(_safe_str(item) for item in value)
    elif isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    else:
        return str(value)


def _get_action_summary(tool_name: str, tool_args: dict) -> str:
    """도구 호출을 간결한 액션 설명으로 변환"""
    action = TOOL_ACTION_DESCRIPTIONS.get(tool_name, tool_name)

    if tool_name == "mobile_click_on_screen_at_coordinates":
        x, y = tool_args.get("x", "?"), tool_args.get("y", "?")
        return f"📍 {action} ({x}, {y})"

    elif tool_name == "mobile_type_keys":
        text = tool_args.get("text", "")
        display_text = text[:20] + "..." if len(text) > 20 else text
        return f"⌨️ {action}: \"{display_text}\""

    elif tool_name == "mobile_swipe_on_screen":
        direction = tool_args.get("direction", "?")
        direction_kr = {"up": "위로", "down": "아래로", "left": "왼쪽으로", "right": "오른쪽으로"}.get(direction, direction)
        return f"👆 {action} {direction_kr}"

    elif tool_name == "mobile_press_button":
        button = tool_args.get("button", "?")
        return f"🔘 {action}: {button}"

    elif tool_name == "mobile_take_screenshot":
        return f"📸 {action}"

    elif tool_name == "mobile_list_elements_on_screen":
        return f"🔍 {action}"

    elif tool_name == "mobile_launch_app":
        pkg = tool_args.get("packageName", "?")
        return f"🚀 {action}: {pkg.split('.')[-1]}"

    elif tool_name == "mobile_open_url":
        url = tool_args.get("url", "?")
        return f"🌐 {action}: {url[:30]}..." if len(url) > 30 else f"🌐 {action}: {url}"

    elif tool_name == "mobile_long_press_on_screen_at_coordinates":
        x, y = tool_args.get("x", "?"), tool_args.get("y", "?")
        return f"👇 {action} ({x}, {y})"

    else:
        return f"🔧 {action}"


def _extract_actions_from_trace(trace: List[dict]) -> List[Tuple[int, str, bool]]:
    """trace에서 수행한 액션들을 추출 (스텝번호, 액션설명, 성공여부)"""
    actions = []

    for t in trace:
        step_num = t.get("step", 0)

        if "llm_action" in t:
            llm_action = t["llm_action"]
            if llm_action.get("type") == "tool_calls":
                tool_calls = llm_action.get("tool_calls", [])
                for tc in tool_calls:
                    tool_name = tc.get("name", "unknown")
                    tool_args = tc.get("arguments", {})

                    # 스크린샷과 요소 분석은 실제 '액션'이 아니므로 제외
                    if tool_name in ["mobile_take_screenshot", "mobile_list_elements_on_screen"]:
                        continue

                    action_desc = _get_action_summary(tool_name, tool_args)

                    # 결과에서 성공 여부 확인
                    success = True
                    if "tool_results" in t:
                        for tr in t.get("tool_results", []):
                            if tr.get("name") == tool_name and "error" in tr:
                                success = False
                                break

                    actions.append((step_num, action_desc, success))

    return actions


def _calculate_stats(trace: List[dict]) -> dict:
    """trace에서 통계 정보 계산"""
    tool_call_count = 0
    tool_calls_by_name = {}
    errors = []

    for t in trace:
        if "tool_results" in t:
            tool_results = t.get("tool_results", [])
            tool_call_count += len(tool_results)
            for tr in tool_results:
                tool_name = tr.get("name", "unknown")
                tool_calls_by_name[tool_name] = tool_calls_by_name.get(tool_name, 0) + 1
                if "error" in tr:
                    errors.append({
                        "step": t.get("step", "?"),
                        "tool": tool_name,
                        "error": tr.get("error", "Unknown error")
                    })

    return {
        "tool_call_count": tool_call_count,
        "tool_calls_by_name": tool_calls_by_name,
        "errors": errors,
        "success_rate": ((tool_call_count - len(errors)) / tool_call_count * 100) if tool_call_count > 0 else 100
    }


def generate_report(state: Dict[Any, str]) -> str:
    """
    실행 결과를 QA 실무자가 보기 편한 마크다운 형식의 리포트로 생성

    Args:
        state: AgentState 딕셔너리

    Returns:
        마크다운 형식의 리포트 문자열
    """
    from datetime import datetime

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    goal = state.get("goal", "N/A")
    status = state.get("status", "unknown")
    step = state.get("step", 0)
    error = state.get("error", "")
    package_name = state.get("package_name", "N/A")
    device_id = state.get("device_id", "N/A")

    trace = state.get("trace", [])
    stats = _calculate_stats(trace)
    actions = _extract_actions_from_trace(trace)

    goal_steps = state.get("goal_steps", [])
    current_step_idx = state.get("current_step_idx", 0)

    md = []

    # ============ 헤더 & 결과 뱃지 ============
    md.append("# 📊 QA 자동화 테스트 리포트")
    md.append("")

    # 결과 뱃지
    if status == "done":
        md.append("> ### ✅ **테스트 통과 (PASS)**")
    elif status == "error":
        md.append("> ### ❌ **테스트 실패 (FAIL)**")
    else:
        md.append(f"> ### ⚠️ **테스트 상태: {status}**")
    md.append("")

    # ============ 테스트 개요 ============
    md.append("## 📋 테스트 개요")
    md.append("")
    md.append("| 항목 | 내용 |")
    md.append("|------|------|")
    md.append(f"| 📅 실행 시간 | {timestamp} |")
    md.append(f"| 📱 디바이스 | `{_safe_str(device_id)}` |")
    md.append(f"| 📦 패키지 | `{_safe_str(package_name)}` |")
    md.append(f"| 🔄 총 스텝 | {step} |")
    md.append(f"| ✅ 성공률 | {stats['success_rate']:.1f}% |")
    if error:
        md.append(f"| ⚠️ 에러 | {_safe_str(error)[:100]} |")
    md.append("")

    # ============ 테스트 목표 & 진행 상황 ============
    md.append("## 🎯 테스트 목표")
    md.append("")

    if goal_steps:
        completed = current_step_idx
        total = len(goal_steps)
        progress_pct = (completed / total * 100) if total > 0 else 0

        # 프로그레스 바
        filled = int(progress_pct / 10)
        progress_bar = "█" * filled + "░" * (10 - filled)
        md.append(f"**진행률:** `[{progress_bar}]` {completed}/{total} ({progress_pct:.0f}%)")
        md.append("")

        for idx, goal_step in enumerate(goal_steps):
            if idx < current_step_idx:
                md.append(f"- [x] ~~{goal_step}~~")
            elif idx == current_step_idx:
                md.append(f"- [ ] **{goal_step}** ← 현재")
            else:
                md.append(f"- [ ] {goal_step}")
    else:
        md.append(f"> {_safe_str(goal)}")
    md.append("")

    # ============ 실행 흐름 (간결하게) ============
    md.append("## 🚀 실행 흐름")
    md.append("")

    if actions:
        md.append("| # | 액션 | 결과 |")
        md.append("|---|------|------|")
        for idx, (step_num, action_desc, success) in enumerate(actions, 1):
            result = "✅" if success else "❌"
            md.append(f"| {idx} | {action_desc} | {result} |")
    else:
        md.append("_실행된 액션이 없습니다._")
    md.append("")

    # ============ 발견된 이슈 ============
    if stats["errors"]:
        md.append("## 🐛 발견된 이슈")
        md.append("")

        for idx, err in enumerate(stats["errors"], 1):
            error_msg = err.get('error', 'Unknown error')
            if isinstance(error_msg, list):
                error_msg = " ".join(str(item) for item in error_msg)
            elif not isinstance(error_msg, str):
                error_msg = str(error_msg)

            tool_name_kr = TOOL_ACTION_DESCRIPTIONS.get(err['tool'], err['tool'])
            md.append(f"### 이슈 #{idx}: {tool_name_kr} 실패")
            md.append("")
            md.append(f"- **발생 스텝:** {err['step']}")
            md.append(f"- **도구:** `{err['tool']}`")
            md.append(f"- **에러 메시지:**")
            md.append("```")
            md.append(error_msg[:500])
            md.append("```")
            md.append("")

    # ============ 최종 결과 ============
    msgs = state.get("messages", [])
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            content = m.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        text_parts.append(item)
                content = " ".join(text_parts) if text_parts else str(content)
            elif not isinstance(content, str):
                content = str(content)

            if content and content != f"Calling {stats['tool_call_count']} tool(s)":
                md.append("## 💬 테스트 결과 요약")
                md.append("")
                md.append(content)
                md.append("")
            break

    # ============ 상세 로그 (접을 수 있게) ============
    md.append("<details>")
    md.append("<summary><b>📝 상세 실행 로그 (클릭하여 펼치기)</b></summary>")
    md.append("")

    for idx, t in enumerate(trace, 1):
        step_num = t.get("step", idx)
        md.append(f"#### Step {step_num}")

        if "llm_action" in t:
            llm_action = t["llm_action"]
            action_type = llm_action.get("type", "unknown")

            if action_type == "tool_calls":
                tool_calls = llm_action.get("tool_calls", [])
                for tc in tool_calls:
                    tool_name = tc.get("name", "unknown")
                    tool_args = tc.get("arguments", {})
                    md.append(f"- **호출:** `{tool_name}`")
                    if tool_args:
                        try:
                            args_str = json.dumps(tool_args, ensure_ascii=False, indent=2)
                            if len(args_str) < 300:
                                md.append(f"  ```json")
                                md.append(f"  {args_str}")
                                md.append(f"  ```")
                        except Exception:
                            pass

            elif action_type == "done":
                md.append("- **완료**")

            elif action_type == "no_tool_calls":
                md.append("- **도구 호출 없음**")

        if "tool_results" in t:
            for tr in t.get("tool_results", []):
                tool_name = tr.get("name", "unknown")
                if "error" in tr:
                    error_msg = tr.get('error', 'Unknown error')
                    if isinstance(error_msg, list):
                        error_msg = " ".join(str(item) for item in error_msg)
                    elif not isinstance(error_msg, str):
                        error_msg = str(error_msg)
                    md.append(f"- ❌ `{tool_name}`: {error_msg[:100]}")
                else:
                    md.append(f"- ✅ `{tool_name}`: 성공")
        md.append("")

    md.append("</details>")
    md.append("")

    # ============ 도구 사용 통계 (접을 수 있게) ============
    if stats["tool_calls_by_name"]:
        md.append("<details>")
        md.append("<summary><b>🔧 도구 사용 통계</b></summary>")
        md.append("")
        md.append("| 도구 | 호출 횟수 |")
        md.append("|------|----------|")
        for tool_name, count in sorted(stats["tool_calls_by_name"].items(), key=lambda x: x[1], reverse=True):
            tool_name_kr = TOOL_ACTION_DESCRIPTIONS.get(tool_name, tool_name)
            md.append(f"| {tool_name_kr} | {count} |")
        md.append("")
        md.append("</details>")
        md.append("")

    # ============ Footer ============
    md.append("---")
    md.append("")
    md.append("*QA 자동화 시스템에서 생성된 리포트입니다.*")
    md.append("")

    return "\n".join(md)


