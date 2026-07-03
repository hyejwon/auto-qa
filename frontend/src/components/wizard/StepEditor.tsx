import { useEffect, useState } from 'react'
import { Plus, Trash2, ChevronUp, ChevronDown } from 'lucide-react'
import type { Step } from '../../types'
import { ACTION_CHOICES, TARGET_ACTIONS } from '../../types'

interface Props {
  steps: Step[]
  ids: string[]
  selectedPackage: string
  apks?: string[]
  disabled?: boolean
  onChange: (steps: Step[], ids: string[]) => void
}

const PKG_ACTIONS = new Set(['launch_app', 'uninstall_app', 'skip_tutorial', 'tutorial_pass', 'enter_sr_debugger', 'close_app'])
const APK_ACTIONS = new Set(['install_app'])

function newId() {
  return `step_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
}

function ParamsJsonEditor({
  value,
  disabled,
  onChange,
}: {
  value: Record<string, unknown>
  disabled?: boolean
  onChange: (params: Record<string, unknown>) => void
}) {
  const [text, setText] = useState(() => JSON.stringify(value || {}, null, 2))
  const [error, setError] = useState('')

  useEffect(() => {
    setText(JSON.stringify(value || {}, null, 2))
    setError('')
  }, [value])

  const apply = () => {
    try {
      const parsed = text.trim() ? JSON.parse(text) : {}
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        setError('params는 JSON object여야 합니다.')
        return
      }
      setError('')
      onChange(parsed)
    } catch {
      setError('JSON 형식이 올바르지 않습니다.')
    }
  }

  return (
    <div className="space-y-1">
      <textarea
        value={text}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onBlur={apply}
        rows={4}
        spellCheck={false}
        className={`w-full resize-y bg-gray-950 border rounded px-2 py-1.5 text-[11px] leading-4 font-mono focus:outline-none disabled:opacity-50 ${
          error ? 'border-red-600 focus:border-red-500' : 'border-gray-700 focus:border-cyan-500'
        }`}
      />
      <div className="flex items-center justify-between gap-2">
        {error ? <p className="text-[11px] text-red-400">{error}</p> : <p className="text-[11px] text-gray-600">blur 또는 적용 시 반영</p>}
        <button
          type="button"
          disabled={disabled}
          onClick={apply}
          className="px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-[11px] text-gray-300"
        >
          적용
        </button>
      </div>
    </div>
  )
}

export default function StepEditor({ steps, ids, selectedPackage, apks = [], disabled, onChange }: Props) {
  const update = (nextSteps: Step[], nextIds: string[] = ids) => onChange(nextSteps, nextIds)

  const setStep = (i: number, s: Step) =>
    update(steps.map((p, idx) => (idx === i ? s : p)))

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (j < 0 || j >= steps.length) return
    const ns = [...steps], ni = [...ids]
    ;[ns[i], ns[j]] = [ns[j], ns[i]]
    ;[ni[i], ni[j]] = [ni[j], ni[i]]
    update(ns, ni)
  }

  const del = (i: number) =>
    update(steps.filter((_, idx) => idx !== i), ids.filter((_, idx) => idx !== i))

  const add = () => {
    const s: Step = { action: 'find_and_tap', target: '', description: '', timeout: 10, retry: 2, params: {} }
    update([...steps, s], [...ids, newId()])
  }

  const changeAction = (i: number, action: string) => {
    const s = steps[i]
    let target = s.target ?? ''
    if (APK_ACTIONS.has(action)) target = apks[0] ?? ''
    else if (PKG_ACTIONS.has(action)) target = selectedPackage || target || ''
    else if (!TARGET_ACTIONS.has(action)) target = ''
    setStep(i, { ...s, action, target })
  }

  return (
    <div className="flex flex-col gap-2">
      {steps.map((s, i) => {
        const isApk = APK_ACTIONS.has(s.action)
        const hasTarget = TARGET_ACTIONS.has(s.action) || PKG_ACTIONS.has(s.action)
        const p = s.params || {}
        return (
          <div key={ids[i]} className="flex gap-2 p-2.5 bg-gray-800 border border-gray-700 rounded-lg items-start">
            <div className="flex flex-col gap-0.5 mt-0.5">
              <button disabled={disabled || i === 0} onClick={() => move(i, -1)}
                className="p-0.5 text-gray-500 hover:text-gray-200 disabled:opacity-30"><ChevronUp size={14} /></button>
              <span className="text-[10px] text-center text-gray-500">{i + 1}</span>
              <button disabled={disabled || i === steps.length - 1} onClick={() => move(i, 1)}
                className="p-0.5 text-gray-500 hover:text-gray-200 disabled:opacity-30"><ChevronDown size={14} /></button>
            </div>

            <div className="flex-1 min-w-0 space-y-1.5">
              <div className="flex flex-wrap gap-1.5">
                <select value={s.action} disabled={disabled}
                  onChange={(e) => changeAction(i, e.target.value)}
                  className="bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500">
                  {ACTION_CHOICES.map((a) => <option key={a} value={a}>{a}</option>)}
                </select>
                {isApk ? (
                  <select value={s.target || ''} disabled={disabled}
                    onChange={(e) => setStep(i, { ...s, target: e.target.value })}
                    className="flex-1 min-w-[130px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-emerald-500">
                    <option value="">설치할 APK 선택</option>
                    {apks.map((a) => <option key={a} value={a}>{a}</option>)}
                  </select>
                ) : (
                  <input value={s.target || ''} disabled={disabled || !hasTarget}
                    onChange={(e) => setStep(i, { ...s, target: e.target.value })}
                    placeholder={hasTarget ? 'target (UI 요소명/패키지)' : '대상 없음'}
                    className="flex-1 min-w-[130px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500 disabled:opacity-40" />
                )}
                <input value={s.description || ''} disabled={disabled}
                  onChange={(e) => setStep(i, { ...s, description: e.target.value })}
                  placeholder="설명"
                  className="flex-1 min-w-[90px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500" />
              </div>

              <div className="flex flex-wrap gap-1.5">
                {s.action === 'wait' ? (
                  <input type="number" min={1} disabled={disabled}
                    value={(p.seconds as number) ?? 2}
                    onChange={(e) => setStep(i, { ...s, params: { ...p, seconds: Number(e.target.value) } })}
                    placeholder="초"
                    className="w-24 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-purple-500" />
                ) : (
                  <>
                    <input value={p.expect_visible || ''} disabled={disabled}
                      onChange={(e) => setStep(i, { ...s, params: { ...p, expect_visible: e.target.value || null } })}
                      placeholder="expect_visible (보여야 할 요소)"
                      className="flex-1 min-w-[130px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-yellow-500" />
                    <input value={p.expect_hidden || ''} disabled={disabled}
                      onChange={(e) => setStep(i, { ...s, params: { ...p, expect_hidden: e.target.value || null } })}
                      placeholder="expect_hidden (사라져야 할 요소)"
                      className="flex-1 min-w-[130px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-orange-500" />
                  </>
                )}
                <input type="number" min={1} disabled={disabled}
                  value={s.timeout ?? 30}
                  onChange={(e) => setStep(i, { ...s, timeout: Number(e.target.value) || 1 })}
                  placeholder="timeout"
                  className="w-24 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-sky-500" />
                <input type="number" min={1} disabled={disabled}
                  value={s.retry ?? 1}
                  onChange={(e) => setStep(i, { ...s, retry: Number(e.target.value) || 1 })}
                  placeholder="retry"
                  className="w-20 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-sky-500" />
              </div>

              <details className="rounded border border-gray-700 bg-gray-900/60">
                <summary className="cursor-pointer select-none px-2 py-1 text-[11px] text-gray-400 hover:text-gray-200">
                  params JSON 편집
                </summary>
                <div className="p-2 pt-1">
                  <ParamsJsonEditor
                    value={(s.params || {}) as Record<string, unknown>}
                    disabled={disabled}
                    onChange={(params) => setStep(i, { ...s, params })}
                  />
                </div>
              </details>
            </div>

            <button disabled={disabled} onClick={() => del(i)}
              className="mt-0.5 p-1 rounded hover:bg-red-900 text-red-400 disabled:opacity-40 transition-colors">
              <Trash2 size={14} />
            </button>
          </div>
        )
      })}

      {steps.length === 0 && (
        <p className="text-center text-gray-600 py-10 text-sm">스텝을 추가해 테스트케이스를 만드세요.</p>
      )}

      <button disabled={disabled} onClick={add}
        className="flex items-center justify-center gap-1 px-2 py-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 rounded text-xs transition-colors">
        <Plus size={13} /> 스텝 추가
      </button>
    </div>
  )
}

export { newId }
