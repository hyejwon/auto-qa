import type { Step } from '../types'

// 플레이스홀더 문법: {{name}} (공백 허용). name = 영숫자/밑줄/점/하이픈
const TOKEN = /\{\{\s*([\w.-]+)\s*\}\}/g

// 스텝의 문자열 필드에서 치환 대상이 되는 값들만 순회
function stringFields(step: Step): string[] {
  const out: string[] = []
  if (typeof step.target === 'string') out.push(step.target)
  if (typeof step.description === 'string') out.push(step.description)
  const p = step.params || {}
  for (const v of Object.values(p)) if (typeof v === 'string') out.push(v)
  return out
}

// 스텝들에 등장하는 파라미터 이름을 등장 순서대로(중복 제거) 추출
export function extractParams(steps: Step[]): string[] {
  const seen: string[] = []
  for (const s of steps) {
    for (const field of stringFields(s)) {
      let m: RegExpExecArray | null
      TOKEN.lastIndex = 0
      while ((m = TOKEN.exec(field))) {
        if (!seen.includes(m[1])) seen.push(m[1])
      }
    }
  }
  return seen
}

function replace(value: string, values: Record<string, string>): string {
  return value.replace(TOKEN, (whole, name) =>
    Object.prototype.hasOwnProperty.call(values, name) ? values[name] : whole,
  )
}

// 플레이스홀더를 실제 값으로 치환한 스텝 배열 반환 (원본 불변)
export function substituteSteps(steps: Step[], values: Record<string, string>): Step[] {
  return steps.map((s) => {
    const next: Step = { ...s }
    if (typeof next.target === 'string') next.target = replace(next.target, values)
    if (typeof next.description === 'string') next.description = replace(next.description, values)
    if (s.params) {
      const np: Record<string, unknown> = { ...s.params }
      for (const [k, v] of Object.entries(np)) {
        if (typeof v === 'string') np[k] = replace(v, values)
      }
      next.params = np
    }
    return next
  })
}
