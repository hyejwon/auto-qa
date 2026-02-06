# qa_agent/planner/planner.py
import json
from typing import Any, Dict, List
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from .prompts import planner_system_prompt
import logging
logger = logging.getLogger(__name__)

class StepArgs(BaseModel):
    """Step의 arguments"""
    pass  # 동적 필드를 허용하기 위해 추가 필드는 런타임에 처리


class PlanStep(BaseModel):
    """계획의 각 단계"""
    goal: str = Field(description="이 단계에서 달성하려는 목표 (한글로 간단히)")
    tool: str = Field(description="사용할 tool 이름")
    args: Dict[str, Any] = Field(default_factory=dict, description="tool에 전달할 arguments")


class Plan(BaseModel):
    """전체 계획"""
    task: str = Field(description="작업 설명")
    steps: List[PlanStep] = Field(description="실행할 단계들의 리스트")
    success_criteria: List[str] = Field(default_factory=list, description="성공 기준")


async def make_plan(llm: ChatGoogleGenerativeAI, goal: str, tools_schema: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    goal을 달성하기 위한 계획을 생성합니다.
    
    Args:
        llm: LLM 인스턴스
        goal: 달성할 목표
        tools_schema: tool들의 스키마 리스트 (OpenAI tool format)
    """
    # structured output을 사용하도록 llm 설정
    structured_llm = llm.with_structured_output(Plan)
    
    # tools_schema를 읽기 쉬운 형식으로 변환
    tools_info = []
    for tool_schema in tools_schema:
        func = tool_schema.get("function", {})
        name = func.get("name", "")
        description = func.get("description", "")
        parameters = func.get("parameters", {})
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        
        # 각 파라미터의 정보 추출
        param_info = {}
        for param_name, param_schema in properties.items():
            param_type = param_schema.get("type", "string")
            param_desc = param_schema.get("description", "")
            is_required = param_name in required
            param_info[param_name] = {
                "type": param_type,
                "description": param_desc,
                "required": is_required
            }
        
        tools_info.append({
            "name": name,
            "description": description,
            "parameters": param_info,
            "required": required
        })
    
    # tools 정보를 읽기 쉬운 텍스트 형식으로 변환
    tools_text = []
    for tool in tools_info:
        tool_text = f"Tool: {tool['name']}\n"
        tool_text += f"  Description: {tool['description']}\n"
        if tool['parameters']:
            tool_text += "  Parameters:\n"
            for param_name, param_info in tool['parameters'].items():
                required_mark = " (required)" if param_name in tool['required'] else " (optional)"
                tool_text += f"    - {param_name}: {param_info['type']}{required_mark}\n"
                if param_info.get('description'):
                    tool_text += f"      {param_info['description']}\n"
        else:
            tool_text += "  Parameters: None (no arguments needed)\n"
        tools_text.append(tool_text)
    
    tools_info_str = "\n".join(tools_text)
    
    system = {"role": "system", "content": planner_system_prompt()}
    user = {
        "role": "user",
        "content": (
            f"goal:\n{goal}\n\n"
            f"available_tools:\n{tools_info_str}\n\n"
            "위 goal을 달성하기 위한 계획을 JSON 형식으로 만들어줘.\n"
            "중요: 각 step에는 goal, tool, args가 모두 포함되어야 합니다.\n"
            "- goal: 해당 단계의 목표를 한글로 간단히 (예: '앱 실행', '설정 버튼 클릭')\n"
            "- tool: 사용할 도구 이름\n"
            "- args: 도구에 전달할 인자 (반드시 해당 tool의 parameters에 정의된 정확한 인자 이름 사용)\n\n"
            "예시:\n"
            '{"goal": "화면 캡처", "tool": "take_screenshot", "args": {"save_debug": true}}\n'
            '{"goal": "설정 클릭", "tool": "smart_click", "args": {"target": "설정"}}\n'
            '{"goal": "앱 실행", "tool": "mobile_launch_app", "args": {"package_name": "com.example.app"}}'
        )
    }

    try:
        plan_obj = await structured_llm.ainvoke([system, user])
        # Pydantic 모델을 dict로 변환
        plan = plan_obj.model_dump()
        
        print(f"PLANNER STRUCTURED OUTPUT:\n{json.dumps(plan, ensure_ascii=False, indent=2)[:2000]}")
        
        if not isinstance(plan, dict) or "steps" not in plan:
            raise ValueError("bad plan format")
        return plan
    except Exception as e:
        print(f"PLANNER ERROR: {e}")
        # fallback plan
        return {
            "task": "fallback",
            "steps": [{"goal": "화면 캡처", "tool": "take_screenshot", "args": {"save_debug": True}}],
            "success_criteria": []
        }
