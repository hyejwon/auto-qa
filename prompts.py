"""Vision agent 프롬프트 모음.

vision_agent.py 에서 import 하여 사용한다. 프롬프트 수정은 이 파일에서만.
"""

FIND_ELEMENT_PROMPT = """이 게임 화면에서 '{target_description}'을(를) 찾아주세요.
{state_context}box_2d는 [ymin, xmin, ymax, xmax] 형식으로, 0~1000 범위로 정규화해서 반환하세요.

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
10. target에 섹션/카테고리명이 포함되면 (예: "마신석 50 상품") 반드시 그 섹션 헤더 아래의
   항목만 선택. 다른 섹션에 수량·가격이 같은 비슷한 상품이 있어도 절대 선택하지 않는다.
   지정된 섹션이나 그 상품이 화면에 안 보이면 found=false로 반환 (스크롤하면 나올 수 있음)

**반환 형식 (JSON만):**
{{
  "found": true/false,
  "box_2d": [ymin, xmin, ymax, xmax] or null,
  "description": "찾은 요소 설명",
  "confidence": 0.0~1.0
}}"""

FIND_ELEMENTS_PROMPT = """이 게임 화면에서 아래 UI 요소를 각각 독립적으로 찾아주세요.

{targets_json}

box_2d는 [ymin, xmin, ymax, xmax] 형식으로, 0~1000 범위로 정규화해서 반환하세요.

**규칙:**
1. 모든 index를 반드시 한 번씩 반환
2. 화면에 실제로 보이는 요소만 found=true
3. 찾을 수 없으면 found=false, box_2d=null
4. 버튼은 장식/로고가 아닌 실제 터치 가능한 영역 전체를 선택
5. 서로 유사한 요소도 target 설명과 일치하는지 각각 판정

**반환 형식 (JSON만):**
{{
  "elements": [
    {{
      "index": 0,
      "found": true,
      "box_2d": [ymin, xmin, ymax, xmax] or null,
      "description": "찾은 요소 설명",
      "confidence": 0.0~1.0
    }}
  ]
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

DETECT_INTERRUPT_PROMPT = """이 게임 화면이 테스트 진행을 가로막는 '인터럽트 팝업'인지 판단해줘.

인터럽트 팝업인 것 (테스트와 무관하게 갑자기 뜨는 것):
- 이벤트/광고/프로모션 팝업, 공지사항, 출석 체크, 업데이트 안내
- 네트워크 오류/재연결 팝업, 점검 안내
- 로그인/로비 진입 직후 뜨는 퀘스트·이벤트 안내 팝업 —
  "계속하려면 화면을 눌러주세요"/"터치하세요" 류 문구가 있는 경우 포함

인터럽트가 아닌 것 (절대 닫으면 안 됨):
- 일반 게임 화면, 상점, 로비, 전투, 로딩 화면
- 상점의 일일 상점/특가/패키지 상품 목록 — 할인·프로모션처럼 보여도
  화면 전체가 상점 UI(재화 표시, 상품 목록, 탭)면 인터럽트가 아님
- Google Play 결제 시트, 구매 인증/확인 팝업
- 구매 직후의 아이템/재화 획득 결과 화면 (방금 구매한 상품의 지급 내역 표시)

인터럽트 판정 기준: 배경 화면 위에 떠 있는 **오버레이 대화상자**(뒷배경이 어둡게 깔리고
그 위에 별도 창이 뜬 형태)일 때만 인터럽트로 판단. 화면 전체를 차지하는 일반 UI는
프로모션 내용이라도 인터럽트가 아님. 확신이 없으면 인터럽트가 아니라고 판단해.

닫는 방법은 아래 세 가지만 허용:
- "tap"        : 닫기(X)/"닫기"/"확인" 버튼 탭 — close_box_2d에 그 버튼 위치를 [ymin, xmin, ymax, xmax] (0~1000 정규화)로 반환
- "tap_center" : "계속하려면 화면을 눌러주세요"/"터치하세요"/"Touch to continue" 같은
                 안내 문구가 있어 화면 아무 곳이나 눌러 닫는 형식일 때
- "back"       : 명확한 닫기 버튼도 안내 문구도 없을 때 안드로이드 뒤로가기

**반환 형식 (JSON만):**
{
  "is_interrupt": true/false,
  "kind": "event|notice|reward|error|network|other",
  "close_method": "tap" | "tap_center" | "back" | null,
  "close_box_2d": [ymin, xmin, ymax, xmax] or null,
  "description": "화면 설명 한 줄"
}"""

EXTRACT_ITEMS_PROMPT = """이 화면에서 '{items_description}'에 해당하는 항목들을 위에서부터 순서대로 모두 추출해줘.

**규칙:**
1. 화면에 실제로 보이는 항목만 (추측 금지)
2. 각 항목의 대표 텍스트를 그대로 반환 (버전 번호, 날짜 등 부가 정보가 있으면 info에)
3. 항목이 없으면 빈 배열

**반환 형식 (JSON만):**
{{
  "items": [
    {{"text": "항목 대표 텍스트", "info": "부가 정보 (없으면 빈 문자열)"}}
  ]
}}"""

READ_ITEM_STATES_PROMPT = """이 화면에서 '{items_description}'에 해당하는 항목들을 순서대로 모두 추출하고,
각 항목의 보유 여부를 판단해줘.

**규칙:**
1. 화면에 실제로 보이는 항목만 (추측 금지)
2. 각 항목의 이름(name)을 그대로 반환
3. 보유 여부(owned)는 설명에 제시된 시각적 기준(색상/자물쇠 아이콘/흐림 처리/배지 등)으로 판단
4. 판단 근거가 애매하면 info에 이유를 남기고 owned는 false로 처리
5. 항목이 없으면 빈 배열

**반환 형식 (JSON만):**
{{
  "items": [
    {{"name": "항목 이름", "owned": true/false, "info": "판단 근거"}}
  ]
}}"""

READ_SCREEN_BATCH_PROMPT = """이 게임 화면에서 아래 항목들을 한 번에 각각 확인해줘.
{state_context}
**확인할 항목 목록:**
{items_block}

**항목별 규칙:**
1. 각 항목은 "값을 읽어야 하는 항목"(숫자/텍스트)이거나 "보이는지만 확인하는 항목"(조건부 존재 확인)이다 —
   항목 설명에 구체적인 판단 기준이 있으면 그대로 따른다.
2. 값을 읽는 항목은 found=true로, value에 화면에 보이는 텍스트를 그대로 담는다.
3. 존재만 확인하는 항목은 조건이 충족되면 found=true, value는 null로 둔다.
4. 화면에서 확인할 수 없거나 조건이 충족되지 않으면 found=false, value는 null.
5. 각 항목의 name은 입력받은 그대로 정확히 반환한다 (누락/오타 금지).

**반환 형식 (JSON만):**
{{
  "items": [
    {{"name": "항목 name", "found": true/false, "value": "값 또는 null"}}
  ]
}}"""

READ_TEXT_PROMPT = """이 게임 화면에서 '{region_description}'에 해당하는 텍스트 값을 읽어줘.
{state_context}
**규칙:**
1. 해당 영역의 텍스트만 정확히 반환한다.
2. 찾을 수 없으면 value를 null로 반환한다.

**반환 형식 (JSON만):**
{{
  "found": true/false,
  "value": "읽은 텍스트" or null
}}"""
