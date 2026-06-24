"""Vision agent 프롬프트 모음.

vision_agent.py 에서 import 하여 사용한다. 프롬프트 수정은 이 파일에서만.
"""

FIND_ELEMENT_PROMPT = """이 게임 화면에서 '{target_description}'을(를) 찾아주세요.
box_2d는 [ymin, xmin, ymax, xmax] 형식으로, 0~1000 범위로 정규화해서 반환하세요.

**규칙:**
1. 요소가 여러 개면 가장 중앙/명확한 것 선택
2. 찾을 수 없으면 box_2d를 null로 반환
3. target에서 "버튼", "클릭", "탭" 같은 역할 단어를 제거한 실제 화면 표시 텍스트를 우선 매칭
4. target이 버튼/로그인/클릭 대상이면 배경 로고, 제목 이미지, 장식 텍스트가 아니라 실제 터치 가능한 UI 버튼/카드 전체를 선택
5. 버튼 안에 아이콘과 텍스트가 함께 있으면 아이콘+텍스트를 포함한 흰색/컬러 버튼 영역 전체를 선택
6. "Google 로그인 버튼"은 Google 로고 아이콘과 "Google 로그인" 텍스트가 들어있는 흰색 직사각형 버튼 전체
7. "Apple 로그인 버튼"은 Apple 아이콘과 "Apple 로그인" 텍스트가 들어있는 흰색 직사각형 버튼 전체
8. "게스트 로그인 버튼"은 사람 아이콘과 "게스트 로그인" 텍스트가 들어있는 흰색 직사각형 버튼 전체
9. 게임 로고, 배경 일러스트, 제목 이미지, 장식용 텍스트는 target 문구와 일부 단어가 겹쳐도 선택하지 않음

**반환 형식 (JSON만):**
{{
  "found": true/false,
  "box_2d": [ymin, xmin, ymax, xmax] or null,
  "description": "찾은 요소 설명",
  "confidence": 0.0~1.0
}}"""

ANALYZE_SCREEN_STATE_PROMPT = """이 게임 화면을 분석해줘.

screen_type은 반드시 아래 값 중 하나만 사용해:
- "title"    : 타이틀/스플래시/로그인 화면 (게스트·Google·Apple 로그인 버튼 등)
- "lobby"    : 메인 로비/홈 화면 (햄버거 메뉴, 전투 시작 등 주요 HUD 포함)
- "settings" : 설정 팝업 또는 설정 화면 (진동·효과음·이용약관·계정연동 등)
- "account"  : 계정 연동 팝업 (Google/Apple 연동·로그아웃·계정삭제 버튼 등)
- "shop"     : 상점/구매 화면
- "battle"   : 전투/게임플레이 화면
- "ranking"  : 랭킹 화면
- "unknown"  : 위 항목에 해당하지 않는 경우

JSON 형식으로 반환:
{{
  "screen_type": "위 목록 중 하나",
  "ui_elements": ["요소1", "요소2", ...],
  "popups": ["팝업1", ...] or [],
  "suggested_actions": ["액션1", "액션2", ...]
}}"""

READ_TEXT_PROMPT = """이 게임 화면에서 '{region_description}'에 해당하는 텍스트 값을 읽어줘.

**규칙:**
1. 해당 영역의 텍스트만 정확히 반환한다.
2. 찾을 수 없으면 value를 null로 반환한다.

**반환 형식 (JSON만):**
{{
  "found": true/false,
  "value": "읽은 텍스트" or null
}}"""
