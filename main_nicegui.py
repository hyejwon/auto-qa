# main_nicegui.py — NiceGUI 기반 QA 자동화 UI
import asyncio
import logging
import queue
import threading
import yaml
from datetime import datetime

from nicegui import ui, app, events, run

from adb_controller import ADBController
from config import Config
from planner_node import PlannerNode
from qa_orchestrator import QAOrchestrator

_root = logging.getLogger()
_root.setLevel(logging.INFO)
if not _root.handlers:
    _console = logging.StreamHandler()
    _console.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    _root.addHandler(_console)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 상수 & 헬퍼
# ─────────────────────────────────────────────

SAMPLE_SCENARIOS = [
    (
        "로그인 이용약관 확인",
        "com.percent.aos.cooptd",
        "앱을 실행한다.\n→ 구글 로그인 버튼을 클릭한다.\n→ 이용약관 버튼을 클릭한다.\n→ 슈퍼매직 이용약관이 화면에 나오는지 확인.\n→ 뒤로가기를 누른다.\n→ 개인정보 처리방침 버튼을 클릭한다.\n→ 화면에서 개인정보 처리방침 텍스트를 확인한다.\n→ 앱을 종료한다.",
    ),
    (
        "진동 설정 ON 테스트",
        "com.percent.aos.cooptd",
        "앱을 실행한다.\n→ 햄버거 메뉴를 클릭한다.\n→ 설정 메뉴로 진입한다.\n→ 진동 ON 버튼을 클릭한다.\n→ 앱을 재실행한다.\n→ 진동 OFF 표시가 보이는지 확인한다.\n→ 앱을 종료한다.",
    ),
    (
        "게스트 계정 삭제",
        "com.percent.aos.cooptd",
        "앱을 실행한다.\n→ 햄버거 메뉴를 클릭한다.\n→ 설정 메뉴로 진입한다.\n→ 계정 연동을 클릭한다.\n→ 계정 삭제 버튼을 클릭한다.\n→ 앱을 재실행한다.\n→ 게스트 로그인 버튼이 보이는지 확인한다.\n→ 앱을 종료한다.",
    ),
    (
        "스태미너 충전 구매",
        "com.percent.aos.cooptd",
        "앱을 실행한다.\n→ 10초 대기한다.\n→ 상단 번개 모양의 스태미너 충전 버튼을 클릭한다.\n→ 구매하기 버튼을 클릭한다.\n→ 구매 완료까지 대기한다.\n→ 스태미너가 정상 지급됐는지 확인한다.\n→ 다이아가 차감됐는지 확인한다.\n→ 앱을 종료한다.",
    ),
]

ACTION_CHOICES = [
    "find_and_tap", "verify", "read_text",
    "wait", "back", "home",
    "launch_app", "close_app", "skip_tutorial", "swipe",
]

TARGET_ACTIONS = {"find_and_tap", "verify", "read_text"}

INSTALLED_PACKAGES = [
    "com.percent.aos.cooptd",
    "com.percent.aos.rollinghero",
    "com.supermagic.aos.statusman",
    "com.percent.aos.luckydefense",
    "com.percent.aos.arenago2",
]


def _config() -> Config:
    return Config()



def _get_template_choices() -> list[str]:
    try:
        tpl_dir = _config().paths.templates_dir
        files = sorted(tpl_dir.glob("*.yaml"))
        return [f.stem for f in files]
    except Exception:
        return []


def _load_template(name: str) -> dict | None:
    if not name:
        return None
    try:
        path = _config().paths.templates_dir / f"{name}.yaml"
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _get_recording_choices() -> list[str]:
    try:
        rec_dir = _config().paths.recordings_dir
        files = sorted(rec_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [f.name for f in files]
    except Exception:
        return []


def _get_apk_choices() -> list[str]:
    try:
        apks_dir = _config().paths.apks_dir
        files = sorted(apks_dir.glob("*.apk"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [f.name for f in files]
    except Exception:
        return []


def _format_summary(result) -> str:
    icon = "✅" if result.status == "PASS" else "❌"
    rows = [
        f"## {icon} {result.status} — {result.title}",
        "",
        "| 항목 | 내용 |",
        "|:---|:---|",
        f"| 테스트 ID | `{result.test_id}` |",
        f"| 스텝 통과 | **{result.steps_passed} / {result.steps_executed}** |",
    ]
    if result.end_time and result.start_time:
        sec = (result.end_time - result.start_time).total_seconds()
        rows.append(f"| 소요 시간 | {sec:.1f}초 |")
    if result.screenshots:
        rows.append(f"| 스크린샷 | {len(result.screenshots)}장 저장 |")
    if result.error_message:
        rows += ["", f"> ⚠️ **오류**: {result.error_message}"]
    if result.step_results:
        rows += ["", "### 스텝별 결과"]
        for sr in result.step_results:
            si = "✅" if sr["passed"] else "❌"
            rows.append(f"- {si} **Step {sr['step']}** — {sr['label']}")
    return "\n".join(rows)


_STEP_MARKERS = ("━", "▶ ", "┌─", "│", "└─", "⏹️", "🔴", "  결과:", "  테스트 시작:", "  패키지:")

# ─────────────────────────────────────────────
# 글로벌 상태
# ─────────────────────────────────────────────

_rec_adb: ADBController | None = None
_test_stop_event: threading.Event | None = None


# ─────────────────────────────────────────────
# 정적 파일 서빙 (녹화 영상)
# ─────────────────────────────────────────────

app.add_static_files('/recordings', str(_config().paths.recordings_dir))


# ─────────────────────────────────────────────
# NiceGUI 페이지
# ─────────────────────────────────────────────

@ui.page('/')
def main_page():
    # ── 페이지 레벨 상태 ──
    template_steps: list[dict] = []

    ui.add_head_html('''
    <script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>
    <style>
        .step-card {
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 8px 12px;
            margin: 4px 0;
            background: #fafafa;
            transition: background 0.15s;
        }
        .step-card:hover { background: #f0f0f0; }
        .sortable-ghost { opacity: 0.4; background: #e3f2fd !important; }
        .sortable-drag { box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
        .drag-handle { cursor: grab; color: #999; font-size: 18px; }
        .drag-handle:active { cursor: grabbing; }
        .q-tab-panel { padding: 16px !important; }
        .log-area textarea { font-family: monospace !important; font-size: 12px !important; }
    </style>
    ''')

    ui.markdown('# 📱 QA 자동화 테스트 도구')
    ui.label('자연어 시나리오를 입력하면 AI가 테스트 플랜을 생성하고 디바이스에서 자동 실행합니다.').classes('text-grey-7')

    with ui.tabs().classes('w-full') as tabs:
        tab_apk = ui.tab('📦 APK 관리')
        tab_create = ui.tab('📝 템플릿 생성')
        tab_run = ui.tab('▶️ 템플릿 실행')
        tab_video = ui.tab('🎬 녹화 영상')

    with ui.tab_panels(tabs, value=tab_apk).classes('w-full'):

        # ═══════════════════════════════════════
        # Tab 1 — APK 관리
        # ═══════════════════════════════════════
        with ui.tab_panel(tab_apk):
            ui.markdown('### APK 설치')
            ui.label('apks/ 폴더에 있는 APK 파일을 선택하여 연결된 디바이스에 설치합니다.').classes('text-grey-7')

            with ui.row().classes('w-full items-end'):
                apk_select = ui.select(
                    _get_apk_choices(), label='APK 파일 선택',
                ).classes('flex-grow')

                def refresh_apks():
                    apk_select.options = _get_apk_choices()
                    apk_select.update()

                ui.button('🔄 새로고침', on_click=refresh_apks).props('flat')

            apk_status = ui.textarea('설치 상태').props('readonly outlined').classes('w-full')

            async def do_install_apk():
                filename = apk_select.value
                if not filename:
                    apk_status.value = '⚠️ APK 파일을 선택해주세요.'
                    return
                apk_status.value = f'📦 설치 중: {filename} ...'
                try:
                    adb = ADBController()
                    apk_path = _config().paths.apks_dir / filename
                    _, msg = adb.install_apk(apk_path)
                    apk_status.value = msg
                except Exception as e:
                    apk_status.value = f'❌ ADB 연결 오류: {e}'

            ui.button('📲 설치', on_click=do_install_apk, color='primary').classes('w-full')

            ui.separator()
            ui.markdown('### 앱 삭제')

            uninstall_select = ui.select(
                INSTALLED_PACKAGES, label='패키지 선택',
            ).classes('w-full')

            uninstall_status = ui.textarea('삭제 상태').props('readonly outlined').classes('w-full')

            async def do_uninstall():
                pkg = uninstall_select.value
                if not pkg:
                    uninstall_status.value = '⚠️ 패키지를 선택해주세요.'
                    return
                uninstall_status.value = f'🗑️ 삭제 중: {pkg} ...'
                try:
                    adb = ADBController()
                    _, msg = adb.uninstall_app(pkg)
                    uninstall_status.value = msg
                except Exception as e:
                    uninstall_status.value = f'❌ ADB 연결 오류: {e}'

            ui.button('🗑️ 삭제', on_click=do_uninstall, color='negative').classes('w-full')

        # ═══════════════════════════════════════
        # Tab 2 — 템플릿 생성
        # ═══════════════════════════════════════
        with ui.tab_panel(tab_create):
            ui.markdown('### 템플릿 생성')
            ui.label('자연어 시나리오를 입력하면 AI가 테스트 플랜을 생성합니다. 생성된 플랜을 템플릿으로 저장할 수 있습니다.').classes('text-grey-7')

            ui.markdown('#### 샘플 시나리오')

            pkg_input = ui.select(
                INSTALLED_PACKAGES, label='패키지명',
                value='com.percent.aos.cooptd',
            ).classes('w-full')
            scenario_input = ui.textarea(
                '테스트 시나리오 (자연어)',
            ).props('outlined rows=14').classes('w-full')

            with ui.row():
                for label, pkg, scenario in SAMPLE_SCENARIOS:
                    def _make_loader(p=pkg, s=scenario):
                        def load():
                            pkg_input.value = p
                            scenario_input.value = s
                        return load
                    ui.button(label, on_click=_make_loader()).props('flat dense')

            plan_status = ui.label('').classes('text-bold')

            yaml_editor = ui.textarea('생성된 테스트 플랜 (YAML)').props(
                'outlined rows=22'
            ).classes('w-full').style('font-family: monospace; font-size: 13px;')

            async def do_generate_plan():
                scenario = scenario_input.value
                package = pkg_input.value
                if not scenario or not scenario.strip():
                    plan_status.text = '⚠️ 시나리오를 입력해주세요.'
                    return
                plan_status.text = '🔄 플랜 생성 중...'
                try:
                    cfg = _config()
                    planner = PlannerNode(
                        project=cfg.gemini.project,
                        location=cfg.gemini.location,
                        model=cfg.gemini.model,
                    )
                    plan = await run.io_bound(
                        planner.create_test_plan, scenario.strip(), (package or '').strip()
                    )
                    yaml_data = {
                        "title": plan.title,
                        "description": plan.description,
                        "package": plan.package,
                        "steps": [s.model_dump() for s in plan.steps],
                        "expected_results": plan.expected_results,
                    }
                    yaml_str = yaml.dump(yaml_data, allow_unicode=True, sort_keys=False)
                    yaml_editor.value = yaml_str
                    plan_status.text = f'✅ 플랜 생성 완료 — {plan.title} ({len(plan.steps)}개 스텝)'
                except Exception as e:
                    logger.exception("generate_plan failed")
                    plan_status.text = f'❌ 오류: {e}'

            async def do_save_template():
                yaml_preview = yaml_editor.value
                if not yaml_preview or not yaml_preview.strip():
                    plan_status.text = '⚠️ 먼저 플랜을 생성해주세요.'
                    return
                try:
                    cfg = _config()
                    tpl_dir = cfg.paths.templates_dir
                    data = yaml.safe_load(yaml_preview)
                    title = data.get('title', '').strip()
                    if not title:
                        plan_status.text = '⚠️ 플랜에 title이 없습니다.'
                        return
                    safe_name = title.replace(' ', '_')
                    data.setdefault("preconditions", [])
                    data["source_scenario"] = (scenario_input.value or '').strip()
                    out_path = tpl_dir / f"{safe_name}.yaml"
                    with open(out_path, "w", encoding="utf-8") as f:
                        yaml.dump(data, f, allow_unicode=True, sort_keys=False)
                    plan_status.text = f'✅ 템플릿 저장 완료 → {out_path.name}'
                    tpl_select.options = _get_template_choices()
                    tpl_select.update()
                except Exception as e:
                    logger.exception("save_template failed")
                    plan_status.text = f'❌ 오류: {e}'

            with ui.row():
                ui.button('🔍 플랜 생성', on_click=do_generate_plan, color='primary')
                ui.button('💾 템플릿으로 저장', on_click=do_save_template)

        # ═══════════════════════════════════════
        # Tab 3 — 템플릿 실행
        # ═══════════════════════════════════════
        with ui.tab_panel(tab_run):
            ui.markdown('### 템플릿 실행')
            ui.label(
                '템플릿을 로드한 후 스텝을 편집·추가·삭제하고 바로 실행할 수 있습니다. '
                '≡ 핸들을 드래그하여 순서를 변경할 수 있습니다.'
            ).classes('text-grey-7')

            with ui.row().classes('w-full items-end'):
                tpl_select = ui.select(
                    _get_template_choices(), label='템플릿 선택',
                ).classes('flex-grow')

                def refresh_templates():
                    tpl_select.options = _get_template_choices()
                    tpl_select.update()

                ui.button('🔄', on_click=refresh_templates).props('flat')
                tpl_load_btn = ui.button('📂 로드', color='primary')

            tpl_status = ui.label('').classes('text-bold')

            with ui.row().classes('w-full'):
                tpl_title = ui.input('테스트 제목 (수정 가능)').classes('flex-grow')
                tpl_package = ui.select(
                    INSTALLED_PACKAGES, label='패키지 (수정 가능)',
                ).classes('w-64')

            ui.markdown('#### 스텝 목록  *(≡ 드래그하여 순서 변경)*')

            # 스텝 카드를 렌더링할 컨테이너
            step_list_container = ui.column().classes('w-full gap-1')
            # sortable 초기화를 위한 고유 ID
            step_list_container._props['id'] = 'step-sortable-list'

            def _render_steps():
                """template_steps 리스트를 기반으로 스텝 카드를 다시 그린다."""
                step_list_container.clear()
                with step_list_container:
                    for i, step in enumerate(template_steps):
                        _build_step_card(i, step)
                    # SortableJS 초기화 (must be inside the container context)
                    _init_sortable()

            def _init_sortable():
                """SortableJS를 컨테이너에 적용한다."""
                ui.run_javascript('''
                    setTimeout(() => {
                        const el = document.getElementById("step-sortable-list");
                        if (!el) return;
                        if (el._sortableInstance) {
                            el._sortableInstance.destroy();
                        }
                        el._sortableInstance = new Sortable(el, {
                            handle: ".drag-handle",
                            animation: 200,
                            ghostClass: "sortable-ghost",
                            dragClass: "sortable-drag",
                            onEnd: function(evt) {
                                const cards = el.querySelectorAll("[data-step-idx]");
                                const newOrder = Array.from(cards).map(c => parseInt(c.getAttribute("data-step-idx")));
                                emitEvent("sort_end", {order: newOrder});
                            }
                        });
                    }, 100);
                ''')

            # SortableJS onEnd 이벤트를 Python에서 처리
            ui.on('sort_end', lambda e: _handle_sort(e))

            def _handle_sort(e):
                """드래그 완료 시 스텝 순서를 변경한다."""
                args = e.args if isinstance(e.args, dict) else {}
                new_order = args.get('order')
                if not new_order or len(new_order) != len(template_steps):
                    logger.warning("sort_end: invalid order %s (steps=%d)", new_order, len(template_steps))
                    return
                reordered = [template_steps[i] for i in new_order]
                template_steps.clear()
                template_steps.extend(reordered)
                tpl_status.text = f'↕️ 순서 변경됨 (총 {len(template_steps)}스텝)'
                _render_steps()

            def _build_step_card(idx: int, step: dict):
                """개별 스텝 카드 UI."""
                action = step.get('action', '')
                target = step.get('target') or ''
                desc = step.get('description', '')
                params = step.get('params') or {}
                expect_vis = params.get('expect_visible') or ''
                expect_hid = params.get('expect_hidden') or ''
                has_target = action in TARGET_ACTIONS

                with ui.card().classes('step-card w-full p-2') as card:
                    card._props['data-step-idx'] = str(idx)
                    with ui.row().classes('w-full items-center no-wrap gap-2'):
                        # 드래그 핸들
                        ui.icon('drag_indicator').classes('drag-handle')

                        # 스텝 번호
                        ui.badge(str(idx + 1), color='blue-grey').props('rounded')

                        # 액션 선택
                        action_sel = ui.select(
                            ACTION_CHOICES, value=action, label='액션',
                        ).classes('w-36').props('dense outlined')

                        # 타겟 입력
                        target_inp = ui.input(
                            'target', value=target,
                        ).classes('flex-grow').props(
                            f'dense outlined {"readonly" if not has_target else ""}'
                        )

                        # 설명
                        desc_inp = ui.input(
                            '설명', value=desc,
                        ).classes('flex-grow').props('dense outlined')

                        # 위로 이동
                        def _make_move_up(i=idx):
                            def move():
                                if i > 0:
                                    template_steps[i], template_steps[i-1] = template_steps[i-1], template_steps[i]
                                    tpl_status.text = f'⬆️ Step {i+1} → Step {i} 이동됨'
                                    _render_steps()
                            return move
                        ui.button('⬆', on_click=_make_move_up()).props(
                            'flat dense round size=sm'
                        ).tooltip('위로 이동')

                        # 아래로 이동
                        def _make_move_down(i=idx):
                            def move():
                                if i < len(template_steps) - 1:
                                    template_steps[i], template_steps[i+1] = template_steps[i+1], template_steps[i]
                                    tpl_status.text = f'⬇️ Step {i+1} → Step {i+2} 이동됨'
                                    _render_steps()
                            return move
                        ui.button('⬇', on_click=_make_move_down()).props(
                            'flat dense round size=sm'
                        ).tooltip('아래로 이동')

                        # 삭제
                        def _make_delete(i=idx):
                            def delete():
                                if 0 <= i < len(template_steps):
                                    removed = template_steps.pop(i)
                                    tpl_status.text = f'🗑️ Step {i+1} ({removed.get("action","")}) 삭제됨 (총 {len(template_steps)}스텝)'
                                    _render_steps()
                            return delete
                        ui.button('✕', on_click=_make_delete(), color='negative').props(
                            'flat dense round size=sm'
                        ).tooltip('삭제')

                    # 두 번째 줄: expect_visible / expect_hidden
                    with ui.row().classes('w-full items-center no-wrap gap-2 pl-10'):
                        expect_vis_inp = ui.input(
                            '기대 화면 (expect_visible)', value=expect_vis,
                        ).classes('flex-grow').props('dense outlined')

                        expect_hid_inp = ui.input(
                            '사라져야 하는 화면 (expect_hidden)', value=expect_hid,
                        ).classes('flex-grow').props('dense outlined')

                    # 액션 변경 시 target readonly 토글
                    def _on_action_change(e, i=idx, t_inp=target_inp):
                        template_steps[i]['action'] = e.value
                        if e.value in TARGET_ACTIONS:
                            t_inp.props(remove='readonly')
                        else:
                            t_inp.props(add='readonly')
                            template_steps[i]['target'] = None
                    action_sel.on_value_change(_on_action_change)

                    # 타겟 변경 반영
                    def _on_target_change(e, i=idx):
                        template_steps[i]['target'] = e.value if e.value else None
                    target_inp.on_value_change(_on_target_change)

                    # 설명 변경 반영
                    def _on_desc_change(e, i=idx):
                        template_steps[i]['description'] = e.value or ''
                    desc_inp.on_value_change(_on_desc_change)

                    # expect_visible 변경 반영
                    def _on_expect_vis_change(e, i=idx):
                        template_steps[i].setdefault('params', {})['expect_visible'] = e.value if e.value else None
                    expect_vis_inp.on_value_change(_on_expect_vis_change)

                    # expect_hidden 변경 반영
                    def _on_expect_hid_change(e, i=idx):
                        template_steps[i].setdefault('params', {})['expect_hidden'] = e.value if e.value else None
                    expect_hid_inp.on_value_change(_on_expect_hid_change)

            def do_load_template():
                name = tpl_select.value
                if not name:
                    tpl_status.text = '⚠️ 템플릿을 선택해주세요.'
                    return
                data = _load_template(name)
                if not data:
                    tpl_status.text = f'❌ {name} 로드 실패'
                    return
                template_steps.clear()
                template_steps.extend(data.get('steps', []))
                tpl_title.value = data.get('title', '')
                tpl_package.value = data.get('package', '')
                tpl_status.text = f'✅ 로드 완료 — {data.get("title", "")} ({len(template_steps)}스텝)'
                _render_steps()

            tpl_load_btn.on_click(do_load_template)

            # ── 스텝 추가 ──
            ui.markdown('#### 스텝 추가')
            with ui.row().classes('items-end'):
                add_action_sel = ui.select(
                    ACTION_CHOICES, value='find_and_tap', label='추가할 액션',
                ).classes('w-48')

                def do_append_step():
                    new_step = {
                        'action': add_action_sel.value or 'find_and_tap',
                        'target': None,
                        'params': {},
                        'description': '',
                        'timeout': 10,
                        'retry': 2,
                    }
                    template_steps.append(new_step)
                    tpl_status.text = f'✅ Step {len(template_steps)} 추가됨 (총 {len(template_steps)}스텝)'
                    _render_steps()

                ui.button('➕ 맨 뒤에 추가', on_click=do_append_step, color='primary')

            ui.separator()

            # ── 실행 영역 ──
            tpl_log = ui.textarea('실시간 실행 로그').props(
                'readonly outlined autogrow'
            ).classes('w-full log-area')
            tpl_summary = ui.markdown('')

            async def do_run_template():
                global _rec_adb, _test_stop_event
                from test_manager import TestCase

                if not template_steps:
                    tpl_status.text = '⚠️ 먼저 템플릿을 로드해주세요.'
                    return

                steps = [dict(s) for s in template_steps]
                pkg = (tpl_package.value or '').strip()
                if pkg:
                    for step in steps:
                        if step.get('action') == 'launch_app':
                            step.setdefault('params', {})['package'] = pkg

                tc_data = {
                    'id': 'TPL_RUN',
                    'title': (tpl_title.value or '').strip() or '템플릿 실행',
                    'description': '',
                    'package': pkg,
                    'steps': steps,
                    'expected_results': [],
                    'preconditions': [],
                }
                testcase = TestCase(**tc_data)

                _test_stop_event = threading.Event()
                log_q: queue.Queue = queue.Queue()
                result_holder: dict = {}

                class _StreamHandler(logging.Handler):
                    def emit(self, record):
                        msg = record.getMessage()
                        if any(m in msg for m in _STEP_MARKERS):
                            log_q.put(msg)

                handler = _StreamHandler()
                root_logger = logging.getLogger()
                root_logger.addHandler(handler)

                tpl_log.value = ''
                tpl_summary.content = ''
                tpl_status.text = '🔄 실행 중...'

                def _run():
                    global _rec_adb
                    try:
                        cfg = _config()
                        _rec_adb = ADBController()
                        session = datetime.now().strftime("%Y%m%d_%H%M%S")
                        _rec_adb.start_recording(cfg.paths.recordings_dir, session)
                        logger.info(f"🔴 화면 녹화 시작 — 세션: {session}")
                    except Exception as e:
                        logger.warning(f"⚠️ 녹화 시작 실패: {e}")
                        _rec_adb = None
                    try:
                        orchestrator = QAOrchestrator(_config())
                        result_holder["result"] = orchestrator.run_test(
                            testcase.id, _test_stop_event, testcase_override=testcase
                        )
                    except Exception as e:
                        logger.exception("run_template thread failed")
                        result_holder["error"] = str(e)
                    finally:
                        if _rec_adb and _rec_adb.is_recording:
                            try:
                                files = _rec_adb.stop_recording()
                                if files:
                                    logger.info(f"⏹️ 녹화 완료 — {len(files)}개 청크")
                            except Exception:
                                pass
                        log_q.put(None)

                t = threading.Thread(target=_run, daemon=True)
                t.start()

                log_lines: list[str] = []
                while True:
                    await asyncio.sleep(0.15)
                    drained = False
                    while True:
                        try:
                            msg = log_q.get_nowait()
                        except queue.Empty:
                            break
                        if msg is None:
                            drained = True
                            break
                        log_lines.append(msg)
                    tpl_log.value = '\n'.join(log_lines)
                    if drained or not t.is_alive():
                        break

                root_logger.removeHandler(handler)
                t.join(timeout=5)
                _test_stop_event = None

                if "result" in result_holder:
                    tpl_summary.content = _format_summary(result_holder["result"])
                    tpl_status.text = '✅ 실행 완료'
                else:
                    err = result_holder.get("error", "알 수 없는 오류")
                    tpl_summary.content = f"## ❌ 실행 실패\n\n> {err}"
                    tpl_status.text = '❌ 실행 실패'

                # 녹화 목록 갱신
                rec_select.options = _get_recording_choices()
                rec_select.update()

            def do_stop_template():
                global _test_stop_event
                if _test_stop_event and not _test_stop_event.is_set():
                    _test_stop_event.set()
                    tpl_status.text = '⏹️ 중단 요청됨 — 현재 스텝 완료 후 중지됩니다.'
                else:
                    tpl_status.text = '⚠️ 실행 중인 테스트가 없습니다.'

            async def do_save_edited_template():
                if not template_steps:
                    tpl_status.text = '⚠️ 저장할 스텝이 없습니다.'
                    return
                title = (tpl_title.value or '').strip()
                if not title:
                    tpl_status.text = '⚠️ 테스트 제목을 입력해주세요.'
                    return
                pkg = (tpl_package.value or '').strip()
                safe_name = title.replace(' ', '_')
                data = {
                    'title': title,
                    'description': '',
                    'package': pkg,
                    'steps': [dict(s) for s in template_steps],
                    'expected_results': [],
                    'preconditions': [],
                }
                out_path = _config().paths.templates_dir / f'{safe_name}.yaml'
                try:
                    with open(out_path, 'w', encoding='utf-8') as f:
                        yaml.dump(data, f, allow_unicode=True, sort_keys=False)
                    tpl_status.text = f'💾 템플릿 저장 완료 → {out_path.name}'
                    tpl_select.options = _get_template_choices()
                    tpl_select.update()
                except Exception as e:
                    logger.exception('save edited template failed')
                    tpl_status.text = f'❌ 저장 오류: {e}'

            with ui.row():
                ui.button('▶️ 바로 실행', on_click=do_run_template, color='primary')
                ui.button('⏹️ 중단', on_click=do_stop_template, color='negative')
                ui.button('💾 템플릿 저장', on_click=do_save_edited_template)

        # ═══════════════════════════════════════
        # Tab 4 — 녹화 영상
        # ═══════════════════════════════════════
        with ui.tab_panel(tab_video):
            ui.markdown('### 녹화된 테스트 영상 다운로드')
            ui.label(
                '테스트 실행 시 자동 저장된 화면 녹화 파일을 선택하여 미리보기 및 다운로드할 수 있습니다.'
            ).classes('text-grey-7')

            with ui.row().classes('w-full items-end'):
                rec_select = ui.select(
                    _get_recording_choices(), label='녹화 파일 선택',
                ).classes('flex-grow')

                def refresh_recs():
                    rec_select.options = _get_recording_choices()
                    rec_select.update()

                ui.button('🔄 새로고침', on_click=refresh_recs).props('flat')

            # 비디오 + 다운로드를 담을 컨테이너 (선택 시 동적으로 재생성)
            video_container = ui.column().classes('w-full')

            def on_rec_change(e):
                filename = e.value
                video_container.clear()
                if not filename:
                    return
                path = _config().paths.recordings_dir / filename
                if not path.exists():
                    with video_container:
                        ui.label(f'⚠️ 파일을 찾을 수 없습니다: {filename}').classes('text-red')
                    return
                with video_container:
                    ui.video(f'/recordings/{filename}').classes('w-full').style(
                        'max-height: 400px'
                    )
                    with ui.row().classes('items-center gap-4'):
                        ui.link(
                            f'⬇️ {filename} 다운로드',
                            f'/recordings/{filename}',
                            new_tab=True,
                        )

                        def do_download(fn=filename):
                            fpath = _config().paths.recordings_dir / fn
                            ui.download(fpath)

                        ui.button('💾 다운로드', on_click=do_download).props('flat')

            rec_select.on_value_change(on_rec_change)


# ─────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title='QA 자동화 테스트',
        host='0.0.0.0',
        port=7860,
        reload=False,
        show=False,
    )
