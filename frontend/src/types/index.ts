export interface DeviceInfo {
  status: 'connected' | 'disconnected' | 'error'
  device_id: string | null
  model: string | null
  error?: string
}

export interface Step {
  action: string
  target?: string | null
  description?: string
  timeout?: number
  retry?: number
  params?: {
    expect_visible?: string | null
    expect_hidden?: string | null
    package?: string
    text?: string
    [key: string]: unknown
  }
}

export interface Template {
  title: string
  description?: string
  package?: string
  steps: Step[]
  expected_results?: string[]
  preconditions?: string[]
  source_scenario?: string
}

export interface StepResult {
  step: number
  label: string
  passed: boolean
}

export interface TestResult {
  status: 'PASS' | 'FAIL'
  title: string
  steps_passed: number
  steps_executed: number
  error_message?: string
  step_results: StepResult[]
}

export interface WsMessage {
  type: 'log' | 'result' | 'error' | 'done' | 'ping'
  message?: string
  data?: TestResult
}

export const ACTION_CHOICES = [
  'find_and_tap',
  'verify',
  'read_text',
  'wait',
  'back',
  'home',
  'launch_app',
  'close_app',
  'install_app',
  'uninstall_app',
  'skip_tutorial',
  'swipe',
  'input_text',
]

export const TARGET_ACTIONS = new Set(['find_and_tap', 'verify', 'read_text', 'install_app', 'uninstall_app', 'input_text'])

export const SAMPLE_SCENARIOS = [
  {
    label: '로그인 이용약관 확인',
    package: 'com.percent.aos.cooptd',
    scenario:
      '앱을 실행한다.\n→ 구글 로그인 버튼을 클릭한다.\n→ 이용약관 버튼을 클릭한다.\n→ 슈퍼매직 이용약관이 화면에 나오는지 확인.\n→ 뒤로가기를 누른다.\n→ 개인정보 처리방침 버튼을 클릭한다.\n→ 화면에서 개인정보 처리방침 텍스트를 확인한다.\n→ 앱을 종료한다.',
  },
  {
    label: '진동 설정 ON',
    package: 'com.percent.aos.cooptd',
    scenario:
      '앱을 실행한다.\n→ 햄버거 메뉴를 클릭한다.\n→ 설정 메뉴로 진입한다.\n→ 진동 ON 버튼을 클릭한다.\n→ 앱을 재실행한다.\n→ 진동 OFF 표시가 보이는지 확인한다.\n→ 앱을 종료한다.',
  },
  {
    label: '게스트 계정 삭제',
    package: 'com.percent.aos.cooptd',
    scenario:
      '앱을 실행한다.\n→ 햄버거 메뉴를 클릭한다.\n→ 설정 메뉴로 진입한다.\n→ 계정 연동을 클릭한다.\n→ 계정 삭제 버튼을 클릭한다.\n→ 앱을 재실행한다.\n→ 게스트 로그인 버튼이 보이는지 확인한다.\n→ 앱을 종료한다.',
  },
  {
    label: '스태미너 충전 구매',
    package: 'com.percent.aos.cooptd',
    scenario:
      '앱을 실행한다.\n→ 10초 대기한다.\n→ 상단 번개 모양의 스태미너 충전 버튼을 클릭한다.\n→ 구매하기 버튼을 클릭한다.\n→ 구매 완료까지 대기한다.\n→ 스태미너가 정상 지급됐는지 확인한다.\n→ 다이아가 차감됐는지 확인한다.\n→ 앱을 종료한다.',
  },
]
