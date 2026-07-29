export interface DeviceInfo {
  status: 'connected' | 'disconnected' | 'error'
  device_id: string | null
  model: string | null
  error?: string
}

// 서버에 연결된(또는 등록만 된) 개별 디바이스
export interface DeviceEntry {
  device_id: string
  model: string
  status: 'device' | 'offline' | string
  busy?: boolean
  registered?: boolean
}

export interface PreflightCheck {
  name: string
  status: 'ok' | 'warn' | 'fail' | 'unknown'
  detail: string
}

export interface PreflightResult {
  checks: PreflightCheck[]
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
    seconds?: number
    text?: string
    [key: string]: unknown
  }
}

export type PlannerMode = 'legacy' | 'defense'

export interface SemanticPlan {
  title: string
  description?: string
  package: string
  supported: boolean
  unsupported_reason?: string
  steps: Array<Record<string, unknown>>
  expected_results?: string[]
}

export interface TemplateParam {
  name: string
  label?: string
  description?: string
  placeholder?: string
  example?: string
  default?: string
}

export interface Template {
  title: string
  description?: string
  package?: string
  steps: Step[]
  parameters?: TemplateParam[]
  expected_results?: string[]
  preconditions?: string[]
  source_scenario?: string
}

export interface StepResult {
  step: number
  label: string
  passed: boolean
  skipped?: boolean
  skip_reason?: string
  action?: string
  target?: string
  failure_reason?: string
  pass_reason?: string
  vision_confidence?: number
  evidence_image?: string
  evidence_timestamp?: string
  evidence_captured_at?: string
  evidence_phase?: 'pre_tap' | 'post_verification' | 'final_verification'
}

export interface EvalOutput {
  final_score?: number
  needs_alert?: boolean
  flow?: {
    score?: number
    reason?: string
    failed_steps?: number[]
    severity?: string
  }
  vision?: {
    score?: number
    low_confidence_steps?: number[]
  }
}

// read_text/read_screen/read_items의 compare_with 결과 — 재화/아이템 전후 비교 표용
export interface EconomyRow {
  name: string
  before: string
  after: string
  delta?: string | null
  passed: boolean
}

export interface TestResult {
  test_id?: string
  status: 'PASS' | 'FAIL'
  title: string
  start_time?: string | null
  end_time?: string | null
  steps_passed: number
  steps_skipped?: number
  steps_failed?: number
  steps_completed?: number
  steps_executed: number
  error_message?: string
  screenshots?: string[]
  step_results: StepResult[]
  economy_summary?: EconomyRow[]
  eval_output?: EvalOutput | null
  pipeline?: {
    templates: {
      name: string
      start_step: number
      end_step: number
      step_count: number
    }[]
  }
}

export interface TapDebug {
  timestamp: string
  evidence_captured_at?: string
  evidence_phase?: 'pre_tap' | 'post_verification' | 'popup_detection' | 'final_verification'
  step_number?: number | null
  step_label?: string
  action?: string
  target: string | null
  confidence: number | null
  verified: boolean
  outcome?: 'PASS' | 'FAIL' | 'SKIP'
  skip_reason?: string
  failure_reason: string
  pass_reason?: string
  image: string
}

export interface WsMessage {
  type: 'log' | 'result' | 'error' | 'done' | 'ping'
  message?: string
  data?: TestResult
}

export interface AdaptivePatch {
  op: string
  index: number
  reason?: string
}

export interface AdaptiveIteration {
  iteration: number
  status: 'PASS' | 'FAIL'
  analysis?: { summary?: string; failure_reason?: string; patch_rationale?: string } | null
  patches?: AdaptivePatch[]
}

export interface AdaptiveRun {
  status: 'PASS' | 'FAIL'
  session_id: string
  iterations: AdaptiveIteration[]
  final_steps: Step[]
  final_result: TestResult | null
}

export interface SharedReport {
  report_id: string
  created_at: string
  result: TestResult
  taps: TapDebug[]
  adaptive?: AdaptiveRun | null
}

export const ACTION_CHOICES = [
  'find_and_tap',
  'verify',
  'read_text',
  'read_items',
  'read_screen',
  'scroll',
  'wait',
  'back',
  'dismiss_popups',
  'home',
  'launch_app',
  'close_app',
  'install_app',
  'uninstall_app',
  'skip_tutorial',
  'call_cheat',
  'set_property',
  'check_property',
  'repeat_until',
  'enter_sr_debugger',
  'swipe',
  'input_text',
]

// target(대상 UI 요소/텍스트)을 입력받는 액션
export const TARGET_ACTIONS = new Set(['find_and_tap', 'verify', 'read_text', 'read_items', 'input_text', 'skip_tutorial', 'call_cheat', 'set_property', 'check_property'])
