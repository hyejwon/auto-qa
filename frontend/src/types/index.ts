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

export interface TemplateParam {
  name: string
  label?: string
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
  failure_reason?: string
  vision_confidence?: number
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

export interface TestResult {
  test_id?: string
  status: 'PASS' | 'FAIL'
  title: string
  start_time?: string | null
  end_time?: string | null
  steps_passed: number
  steps_executed: number
  error_message?: string
  screenshots?: string[]
  step_results: StepResult[]
  eval_output?: EvalOutput | null
}

export interface TapDebug {
  timestamp: string
  target: string | null
  confidence: number | null
  verified: boolean
  failure_reason: string
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
  'tutorial_pass',
  'enter_sr_debugger',
  'swipe',
  'input_text',
]

// target(대상 UI 요소/텍스트)을 입력받는 액션
export const TARGET_ACTIONS = new Set(['find_and_tap', 'verify', 'read_text', 'input_text', 'skip_tutorial', 'tutorial_pass'])
