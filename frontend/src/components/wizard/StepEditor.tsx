import { useEffect, useRef, useState } from 'react'
import { Plus, Trash2, GripVertical } from 'lucide-react'
import type { Step } from '../../types'
import { ACTION_CHOICES, TARGET_ACTIONS } from '../../types'

interface Props {
  steps: Step[]
  ids: string[]
  stepGroupNames?: Record<string, string>
  selectedPackage: string
  apks?: string[]
  disabled?: boolean
  onChange: (steps: Step[], ids: string[]) => void
}

const PKG_ACTIONS = new Set(['launch_app', 'uninstall_app', 'skip_tutorial', 'enter_sr_debugger', 'close_app'])
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

export default function StepEditor({ steps, ids, stepGroupNames = {}, selectedPackage, apks = [], disabled, onChange }: Props) {
  const [draggedId, setDraggedId] = useState('')
  const [dropHint, setDropHint] = useState<{ id: string; edge: 'before' | 'after' } | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const dragPointerY = useRef<number | null>(null)
  const update = (nextSteps: Step[], nextIds: string[] = ids) => onChange(nextSteps, nextIds)

  // 드래그 중 스크롤 영역 가장자리에 머무르면 자동으로 스크롤 — 그래야 화면 밖 스텝으로도 옮길 수 있음
  useEffect(() => {
    if (!draggedId) return
    let node: HTMLElement | null = containerRef.current
    let scrollEl: HTMLElement | null = null
    while (node) {
      const style = getComputedStyle(node)
      if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight) {
        scrollEl = node
        break
      }
      node = node.parentElement
    }
    if (!scrollEl) return
    const EDGE = 56
    const MAX_SPEED = 16
    let raf = 0
    const tick = () => {
      const y = dragPointerY.current
      if (y != null && scrollEl) {
        const rect = scrollEl.getBoundingClientRect()
        const topGap = y - rect.top
        const bottomGap = rect.bottom - y
        if (topGap < EDGE) {
          scrollEl.scrollTop -= MAX_SPEED * (1 - Math.max(topGap, 0) / EDGE)
        } else if (bottomGap < EDGE) {
          scrollEl.scrollTop += MAX_SPEED * (1 - Math.max(bottomGap, 0) / EDGE)
        }
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [draggedId])

  const setStep = (i: number, s: Step) =>
    update(steps.map((p, idx) => (idx === i ? s : p)))

  const moveStep = (sourceId: string, targetId: string, edge: 'before' | 'after') => {
    const sourceIndex = ids.indexOf(sourceId)
    const targetIndex = ids.indexOf(targetId)
    if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) return
    const insertionBoundary = targetIndex + (edge === 'after' ? 1 : 0)
    const nextIds = [...ids]
    nextIds.splice(sourceIndex, 1)
    const insertionIndex = sourceIndex < insertionBoundary ? insertionBoundary - 1 : insertionBoundary
    nextIds.splice(insertionIndex, 0, sourceId)
    const stepById = new Map(ids.map((id, index) => [id, steps[index]]))
    const nextSteps = nextIds.map((id) => stepById.get(id)).filter((step): step is Step => !!step)
    update(nextSteps, nextIds)
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
    let params = s.params || {}
    if (APK_ACTIONS.has(action)) target = apks[0] ?? ''
    else if (PKG_ACTIONS.has(action)) target = selectedPackage || target || ''
    else if (!TARGET_ACTIONS.has(action)) target = ''
    if (action === 'dismiss_popups' && s.action !== action) {
      params = {
        stop_when_visible: '',
        max_count: 4,
        timeout_seconds: 30,
        quiet_seconds: 1.5,
        rules: [{ target: '', action: 'tap_center' }],
      }
    }
    setStep(i, { ...s, action, target, params })
  }

  // 목록 맨 위/맨 아래는 인접한 반대쪽 존이 없어(예: 맨 위 앞엔 넣을 자리가 없음)
  // 커서가 살짝만 밀려도 같은 자리에 재삽입되는 것처럼 보임 — 전용 드롭 존으로 넉넉하게 받아준다.
  const renderEdgeZone = (targetId: string, edge: 'before' | 'after') => {
    const active = dropHint?.id === targetId && dropHint.edge === edge
    return (
      <div
        onDragOver={(e) => {
          if (!e.dataTransfer.types.includes('application/x-autoqa-step')) return
          e.preventDefault()
          dragPointerY.current = e.clientY
          setDropHint({ id: targetId, edge })
        }}
        onDrop={(e) => {
          e.preventDefault()
          const sourceId = draggedId || e.dataTransfer.getData('application/x-autoqa-step')
          moveStep(sourceId, targetId, edge)
          setDraggedId('')
          setDropHint(null)
        }}
        className={`h-3 -my-1 rounded transition-colors ${active ? 'bg-cyan-900/50 ring-1 ring-cyan-500' : ''}`}
      />
    )
  }

  return (
    <div ref={containerRef} className="flex flex-col gap-2">
      {draggedId && ids.length > 0 && ids[0] !== draggedId && renderEdgeZone(ids[0], 'before')}
      {steps.map((s, i) => {
        const isApk = APK_ACTIONS.has(s.action)
        const hasTarget = TARGET_ACTIONS.has(s.action) || PKG_ACTIONS.has(s.action)
        const p = s.params || {}
        const hasWaitCondition = [
          'until_scene',
          'until_property',
          'until_unity_button',
          'until_visible',
          'until_hidden',
        ].some((key) => Boolean(p[key]))
        return (
          <div key={ids[i]}
            onDragOver={(e) => {
              if (!e.dataTransfer.types.includes('application/x-autoqa-step')) return
              e.preventDefault()
              dragPointerY.current = e.clientY
              if (draggedId === ids[i]) return
              const rect = e.currentTarget.getBoundingClientRect()
              setDropHint({ id: ids[i], edge: e.clientY < rect.top + rect.height / 2 ? 'before' : 'after' })
            }}
            onDrop={(e) => {
              e.preventDefault()
              const sourceId = draggedId || e.dataTransfer.getData('application/x-autoqa-step')
              const edge = dropHint?.id === ids[i] ? dropHint.edge : 'before'
              moveStep(sourceId, ids[i], edge)
              setDraggedId('')
              setDropHint(null)
            }}
            className={`relative flex gap-2 p-2.5 bg-gray-800 border rounded-lg items-start transition-colors ${
              draggedId === ids[i] ? 'opacity-40 border-cyan-600' : 'border-gray-700'
            } ${dropHint?.id === ids[i] && dropHint.edge === 'before' ? 'before:absolute before:left-0 before:right-0 before:-top-1 before:h-0.5 before:bg-cyan-400' : ''}
            ${dropHint?.id === ids[i] && dropHint.edge === 'after' ? 'after:absolute after:left-0 after:right-0 after:-bottom-1 after:h-0.5 after:bg-cyan-400' : ''}`}>
            <div className="flex flex-col items-center gap-1 mt-0.5">
              <button type="button" draggable={!disabled}
                disabled={disabled}
                onDragStart={(e) => {
                  e.dataTransfer.effectAllowed = 'move'
                  e.dataTransfer.setData('application/x-autoqa-step', ids[i])
                  setDraggedId(ids[i])
                }}
                onDragEnd={() => { setDraggedId(''); setDropHint(null); dragPointerY.current = null }}
                className="p-0.5 text-gray-500 hover:text-cyan-300 cursor-grab active:cursor-grabbing disabled:cursor-default disabled:opacity-30"
                title="스텝 이동">
                <GripVertical size={15} />
              </button>
              <span className="text-[10px] text-center text-gray-500">{i + 1}</span>
            </div>

            <div className="flex-1 min-w-0 space-y-1.5">
              <div className="flex flex-wrap gap-1.5">
                {stepGroupNames[ids[i]] && (
                  <span title={stepGroupNames[ids[i]]}
                    className="max-w-[8rem] truncate rounded border border-blue-900 bg-blue-950/50 px-1.5 py-1 text-[10px] text-blue-300">
                    [{stepGroupNames[ids[i]]}]
                  </span>
                )}
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
                  hasWaitCondition ? (
                    <span className="rounded border border-emerald-800 bg-emerald-950/40 px-2 py-1 text-[10px] text-emerald-300">
                      상태 조건 대기 · params JSON에서 편집
                    </span>
                  ) : (
                    <label className="flex w-28 flex-col gap-0.5 text-[10px] text-gray-400">
                      <span>대기 시간 (초)</span>
                      <input type="number" min={0} step={0.5} disabled={disabled}
                        value={(p.seconds as number) ?? 2}
                        aria-label="대기 시간(초)"
                        onChange={(e) => setStep(i, { ...s, params: { ...p, seconds: Number(e.target.value) } })}
                        className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-purple-500" />
                    </label>
                  )
                ) : s.action === 'scroll' ? (
                  <>
                    <label className="flex w-32 flex-col gap-0.5 text-[10px] text-gray-400">
                      <span>스크롤 방향</span>
                      <select disabled={disabled}
                        value={(p.direction as string) ?? 'down'}
                        onChange={(e) => setStep(i, { ...s, params: { ...p, direction: e.target.value } })}
                        className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-purple-500">
                        <option value="down">아래로</option>
                        <option value="up">위로</option>
                        <option value="top">맨 위까지</option>
                        <option value="bottom">맨 아래까지</option>
                      </select>
                    </label>
                    {((p.direction as string) ?? 'down') === 'down' || (p.direction as string) === 'up' ? (
                      <label className="flex w-20 flex-col gap-0.5 text-[10px] text-gray-400">
                        <span>반복 (회)</span>
                        <input type="number" min={1} max={20} disabled={disabled}
                          value={(p.times as number) ?? 1}
                          aria-label="스크롤 반복 횟수"
                          onChange={(e) => setStep(i, { ...s, params: { ...p, times: Number(e.target.value) || 1 } })}
                          className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-purple-500" />
                      </label>
                    ) : null}
                  </>
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
                <label className="flex w-28 flex-col gap-0.5 text-[10px] text-gray-400">
                  <span>제한 시간 (초)</span>
                  <input type="number" min={1} disabled={disabled}
                    value={s.timeout ?? 30}
                    aria-label="스텝 제한 시간(초)"
                    onChange={(e) => setStep(i, { ...s, timeout: Number(e.target.value) || 1 })}
                    className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-sky-500" />
                </label>
                <label className="flex w-24 flex-col gap-0.5 text-[10px] text-gray-400">
                  <span>재시도 (회)</span>
                  <input type="number" min={1} disabled={disabled}
                    value={s.retry ?? 1}
                    aria-label="스텝 재시도 횟수"
                    onChange={(e) => setStep(i, { ...s, retry: Number(e.target.value) || 1 })}
                    className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-sky-500" />
                </label>
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
      {draggedId && ids.length > 0 && ids[ids.length - 1] !== draggedId && renderEdgeZone(ids[ids.length - 1], 'after')}

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
