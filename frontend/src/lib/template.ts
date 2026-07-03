import type { Step, TemplateParam } from '../types'

function q(v: string): string {
  // YAML 안전을 위해 문자열은 큰따옴표로 감싸고 내부 특수문자 이스케이프
  return `"${String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

function scalar(v: unknown): string {
  if (typeof v === 'string') return q(v)
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (v == null) return 'null'
  return JSON.stringify(v)
}

function shouldWriteParam(v: unknown): boolean {
  if (v === undefined || v === null) return false
  if (typeof v === 'string' && v.length === 0) return false
  return true
}

// 수동 편집한 스텝들을 백엔드 템플릿(YAML)로 직렬화
export function stepsToYaml(title: string, pkg: string, steps: Step[], params: TemplateParam[] = []): string {
  const lines: string[] = [
    `title: ${q(title)}`,
    `description: ""`,
    `package: ${q(pkg || '')}`,
  ]
  if (params.length) {
    lines.push(`parameters:`)
    for (const p of params) {
      lines.push(`  - name: ${q(p.name)}`)
      if (p.default != null) lines.push(`    default: ${q(String(p.default))}`)
    }
  }
  lines.push(`steps:`)
  for (const s of steps) {
    lines.push(`  - action: ${s.action}`)
    if (s.target) lines.push(`    target: ${q(s.target)}`)
    if (s.description) lines.push(`    description: ${q(s.description)}`)
    if (s.timeout != null) lines.push(`    timeout: ${s.timeout}`)
    if (s.retry != null) lines.push(`    retry: ${s.retry}`)

    const p = s.params || {}
    const paramLines: string[] = []
    for (const [key, value] of Object.entries(p)) {
      if (!shouldWriteParam(value)) continue
      paramLines.push(`      ${key}: ${scalar(value)}`)
    }
    if (paramLines.length) {
      lines.push(`    params:`)
      lines.push(...paramLines)
    }
  }
  return lines.join('\n') + '\n'
}
