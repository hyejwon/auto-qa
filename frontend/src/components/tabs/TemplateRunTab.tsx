import { useEffect, useRef, useState } from 'react'
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from '@dnd-kit/core'
import {
  SortableContext,
  verticalListSortingStrategy,
  useSortable,
  arrayMove,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Play, Square, Save, Plus, Trash2, GripVertical, RefreshCw, Loader2 } from 'lucide-react'
import { templateApi, packageApi, testApi, apkApi, agentWsUrl } from '../../api/client'
import { StableInput } from '../StableInput'
import type { AgentInfo, Step, TestResult } from '../../types'
import { ACTION_CHOICES, TARGET_ACTIONS } from '../../types'

function isRecordingLog(message: string) {
  return message.includes('녹화 시작') || message.includes('녹화 완료')
}

function StepCard({
  id,
  step,
  index,
  total,
  onChange,
  onDelete,
  packages,
  apks,
}: {
  id: string
  step: Step
  index: number
  total: number
  onChange: (s: Step) => void
  onDelete: () => void
  packages: string[]
  apks: string[]
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
    zIndex: isDragging ? 50 : undefined,
  }

  const hasTarget = TARGET_ACTIONS.has(step.action)

  const PKG_ACTIONS = new Set(['launch_app', 'uninstall_app', 'skip_tutorial'])
  const APK_ACTIONS = new Set(['install_app'])

  const handleActionChange = (action: string) => {
    let target = step.target ?? null
    if (PKG_ACTIONS.has(action)) {
      target = packages[0] ?? null
    } else if (APK_ACTIONS.has(action)) {
      target = apks[0] ?? null
    } else if (!TARGET_ACTIONS.has(action)) {
      target = null
    }
    onChange({ ...step, action, target })
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="flex gap-2 p-3 bg-gray-800 border border-gray-700 rounded-lg items-start"
    >
      {/* 드래그 핸들 */}
      <button
        {...attributes}
        {...listeners}
        className="mt-1 flex-none p-0.5 text-gray-500 hover:text-gray-300 cursor-grab active:cursor-grabbing touch-none"
        tabIndex={-1}
      >
        <GripVertical size={16} />
      </button>

      {/* 번호 */}
      <span className="mt-1 flex-none w-6 h-6 rounded-full bg-gray-700 text-xs flex items-center justify-center text-gray-300">
        {index + 1}
      </span>

      <div className="flex-1 space-y-2 min-w-0">
        <div className="flex flex-wrap gap-2">
          <select
            value={step.action}
            onChange={(e) => handleActionChange(e.target.value)}
            className="bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500"
          >
            {ACTION_CHOICES.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>

          {PKG_ACTIONS.has(step.action) ? (
            <select
              value={step.target || ''}
              onChange={(e) => onChange({ ...step, target: e.target.value })}
              className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500"
            >
              <option value="">패키지 선택</option>
              {packages.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          ) : APK_ACTIONS.has(step.action) ? (
            <select
              value={step.target || ''}
              onChange={(e) => onChange({ ...step, target: e.target.value })}
              className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-green-500"
            >
              <option value="">APK 선택</option>
              {apks.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          ) : (
            <StableInput
              value={step.target || ''}
              onValueChange={(value) => onChange({ ...step, target: value })}
              disabled={!hasTarget}
              placeholder="target (UI 요소명)"
              className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500 disabled:opacity-40"
            />
          )}

          <StableInput
            value={step.description || ''}
            onValueChange={(value) => onChange({ ...step, description: value })}
            placeholder="설명"
            className="flex-1 min-w-[100px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500"
          />
        </div>

        <div className="flex flex-wrap gap-2">
          {step.action === 'wait' ? (
            <input
              type="number"
              min={1}
              value={(step.params?.seconds as number) ?? 2}
              onChange={(e) =>
                onChange({ ...step, params: { ...step.params, seconds: Number(e.target.value) } })
              }
              placeholder="초(seconds)"
              className="w-28 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-purple-500"
            />
          ) : (
            <>
              <StableInput
                value={step.params?.expect_visible || ''}
                onValueChange={(value) =>
                  onChange({ ...step, params: { ...step.params, expect_visible: value || null } })
                }
                placeholder="expect_visible"
                className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-yellow-500"
              />
              <StableInput
                value={step.params?.expect_hidden || ''}
                onValueChange={(value) =>
                  onChange({ ...step, params: { ...step.params, expect_hidden: value || null } })
                }
                placeholder="expect_hidden"
                className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-orange-500"
              />
            </>
          )}
        </div>
      </div>

      <button
        onClick={onDelete}
        className="mt-1 flex-none p-1 rounded hover:bg-red-900 text-red-400 transition-colors"
      >
        <Trash2 size={14} />
      </button>
    </div>
  )
}

interface Props { agent: AgentInfo | null }

export default function TemplateRunTab({ agent }: Props) {
  const [templates, setTemplates] = useState<string[]>([])
  const [packages, setPackages] = useState<string[]>([])
  const [apks, setApks] = useState<string[]>([])
  const [selectedTemplate, setSelectedTemplate] = useState('')
  const [title, setTitle] = useState('')
  const [steps, setSteps] = useState<Step[]>([])
  const [stepIds, setStepIds] = useState<string[]>([])
  const [status, setStatus] = useState('')
  const [logs, setLogs] = useState<string[]>([])
  const [result, setResult] = useState<TestResult | null>(null)
  const [running, setRunning] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [saving, setSaving] = useState(false)
  const [record, setRecord] = useState(true)
  const logsEndRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const sessionId = useRef(`sess_${Date.now()}`)

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))

  const syncIds = (s: Step[]) => s.map((_, i) => `step-${i}-${Date.now()}`)

  const loadLists = async () => {
    const [tRes, pRes, aRes, mapRes] = await Promise.all([
      templateApi.list(),
      agent ? packageApi.list(agent.name) : Promise.resolve({ packages: [] }),
      agent ? apkApi.list(agent.name) : Promise.resolve({ apks: [] }),
      agent ? packageApi.getApkMap(agent.name) : Promise.resolve({ map: {} }),
    ])
    setTemplates(tRes.templates)
    // 설치된 패키지 + apkMap 등록 패키지 합치기 (중복 제거)
    const mapPkgs = Object.keys(mapRes.map ?? {})
    setPackages([...new Set([...pRes.packages, ...mapPkgs])])
    setApks(aRes.apks)
  }

  useEffect(() => { loadLists() }, [agent?.name])
  useEffect(() => { logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [logs])

  const handleLoad = async () => {
    if (!selectedTemplate) return setStatus('⚠️ 템플릿을 선택해주세요.')
    const res = await templateApi.get(selectedTemplate)
    const t = res.template
    const loaded = t.steps || []
    setTitle(t.title || '')
    setSteps(loaded)
    setStepIds(loaded.map((_, i) => `step-${i}${Date.now()}`))
    setStatus(`✅ 로드 완료 — ${t.title} (${loaded.length}개 스텝)`)
    setLogs([])
    setResult(null)
  }

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oldIdx = stepIds.indexOf(active.id as string)
    const newIdx = stepIds.indexOf(over.id as string)
    setSteps((s) => arrayMove(s, oldIdx, newIdx))
    setStepIds((ids) => arrayMove(ids, oldIdx, newIdx))
  }

  const updateStep = (idx: number, s: Step) =>
    setSteps((prev) => prev.map((p, i) => (i === idx ? s : p)))

  const deleteStep = (idx: number) => {
    setSteps((prev) => prev.filter((_, i) => i !== idx))
    setStepIds((prev) => prev.filter((_, i) => i !== idx))
  }

  const addStep = () => {
    const newStep: Step = { action: 'find_and_tap', target: null, description: '', timeout: 10, retry: 2, params: {} }
    setSteps((prev) => [...prev, newStep])
    setStepIds((prev) => [...prev, `step-${prev.length}-${Date.now()}`])
  }

  const handleRun = async () => {
    if (!agent) return setStatus('⚠️ 에이전트를 먼저 선택해주세요.')
    if (!steps.length) return setStatus('⚠️ 먼저 템플릿을 로드해주세요.')
    setRunning(true)
    setLogs([])
    setResult(null)
    setStatus('🔄 실행 중...')

    const sid = `sess_${Date.now()}`
    sessionId.current = sid

    const ws = new WebSocket(agentWsUrl(agent, sid))
    wsRef.current = ws

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'log') {
        if (!isRecordingLog(msg.message)) setLogs((p) => [...p, msg.message])
      }
      else if (msg.type === 'result') { setResult(msg.data); setStatus('✅ 실행 완료') }
      else if (msg.type === 'error') { setStatus(`❌ ${msg.message}`); setRunning(false) }
      else if (msg.type === 'done') { setRunning(false); setStopping(false); setStatus((s) => s.includes('중단') ? '⏹️ 중단됨' : s); ws.close() }
    }
    ws.onerror = () => { setStatus('❌ WebSocket 연결 오류'); setRunning(false) }

    const pkg = steps.find((s) => s.action === 'launch_app' && s.target)?.target ?? ''
    await testApi.run(agent.name, { title: title || '템플릿 실행', package: pkg, steps, session_id: sid, record })
  }

  const handleStop = async () => {
    if (!agent) return
    setStopping(true)
    setStatus('⏹️ 중단 중...')
    setLogs((p) => [...p, '⏹️ 중단 요청 중...'])
    await testApi.stop(agent.name, sessionId.current)
    setStopping(false)
    // WebSocket은 백엔드가 done 메시지 보낼 때 자동으로 닫힘
  }

  const handleSave = async () => {
    if (!steps.length) return setStatus('⚠️ 저장할 스텝이 없습니다.')
    if (!title.trim()) return setStatus('⚠️ 테스트 제목을 입력해주세요.')
    setSaving(true)
    const yaml = `title: ${title}\ndescription: ''\npackage: ''\nsteps:\n${steps.map(s => {
      let line = `  - action: ${s.action}`
      if (s.target) line += `\n    target: ${s.target}`
      if (s.description) line += `\n    description: ${s.description}`
      if (s.action === 'wait' && s.params?.seconds != null) line += `\n    params:\n      seconds: ${s.params.seconds}`
      return line
    }).join('\n')}\n`
    const res = await templateApi.save(title, yaml)
    setStatus(res.success ? `💾 저장 완료 → ${res.filename}` : `❌ 저장 실패: ${res.error}`)
    setSaving(false)
    loadLists()
  }

  return (
    <div className="flex gap-4 h-[calc(100vh-120px)]">

      {/* ── 왼쪽: 설정 + 스텝 ── */}
      <div className="flex flex-col gap-3 w-[55%] min-w-0">

        {/* 템플릿 선택 */}
        <div className="flex gap-2 items-end flex-none">
          <div className="flex-1">
            <label className="block text-xs text-gray-400 mb-1">템플릿 선택</label>
            <select
              value={selectedTemplate}
              onChange={(e) => setSelectedTemplate(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
            >
              <option value="">템플릿 선택</option>
              {templates.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <button onClick={loadLists} className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-white transition-colors">
            <RefreshCw size={16} />
          </button>
          <button onClick={handleLoad} className="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium transition-colors">
            📂 로드
          </button>
        </div>

        {/* 제목 */}
        <div className="flex gap-2 flex-none">
          <StableInput
            value={title}
            onValueChange={setTitle}
            placeholder="테스트 제목"
            className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
          />
        </div>

        {/* 실행 버튼 */}
        <div className="flex gap-2 flex-none items-center">
          <button onClick={handleRun} disabled={running || !steps.length}
            className="flex items-center gap-2 px-4 py-2 bg-green-700 hover:bg-green-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors">
            {running ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
            {running ? '실행 중...' : '▶️ 실행'}
          </button>
          <button onClick={handleStop} disabled={!running || stopping}
            className="flex items-center gap-2 px-4 py-2 bg-red-700 hover:bg-red-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors">
            {stopping ? <Loader2 size={16} className="animate-spin" /> : <Square size={16} />}
            {stopping ? '중단 중...' : '중단'}
          </button>
          <button onClick={handleSave} disabled={saving}
            className="flex items-center gap-2 px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors">
            {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
            저장
          </button>
          <button
            onClick={() => setRecord((v) => !v)}
            className={`flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs border transition-colors ${
              record ? 'bg-red-950/50 border-red-700 text-red-400' : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            <span>🔴</span>
            {record ? '녹화 ON' : '녹화'}
          </button>
          {status && <span className="flex items-center text-xs text-gray-400 truncate">{status}</span>}
        </div>

        {/* 스텝 목록 */}
        <div className="flex flex-col flex-1 min-h-0">
          <div className="flex items-center justify-between mb-2 flex-none">
            <label className="text-xs text-gray-400">스텝 목록 ({steps.length}개)</label>
            <button onClick={addStep}
              className="flex items-center gap-1 px-2 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs transition-colors">
              <Plus size={13} /> 스텝 추가
            </button>
          </div>

          <div className="flex-1 overflow-y-auto scrollbar-thin pr-1">
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
              <SortableContext items={stepIds} strategy={verticalListSortingStrategy}>
                <div className="space-y-2">
                  {steps.map((s, i) => (
                    <StepCard
                      key={stepIds[i]}
                      id={stepIds[i]}
                      step={s}
                      index={i}
                      total={steps.length}
                      onChange={(ns) => updateStep(i, ns)}
                      onDelete={() => deleteStep(i)}
                      packages={packages}
                      apks={apks}
                    />
                  ))}
                  {steps.length === 0 && (
                    <p className="text-center text-gray-600 py-16 text-sm">템플릿을 로드하거나 스텝을 추가하세요.</p>
                  )}
                </div>
              </SortableContext>
            </DndContext>
          </div>
        </div>
      </div>

      {/* ── 오른쪽: 로그 + 결과 ── */}
      <div className="flex flex-col gap-3 flex-1 min-w-0">

        <div className="flex flex-col flex-1 min-h-0">
          <label className="block text-xs text-gray-400 mb-1 flex-none">
            실시간 실행 로그
            {running && <span className="ml-2 text-green-400 animate-pulse">● 실행 중</span>}
          </label>
          <div className="flex-1 overflow-y-auto scrollbar-thin bg-gray-900 border border-gray-700 rounded-lg p-3 font-mono text-xs space-y-0.5">
            {logs.length === 0 && !running && (
              <p className="text-gray-600 py-4 text-center">실행 후 로그가 여기에 표시됩니다.</p>
            )}
            {logs.map((l, i) => <div key={i} className="text-gray-300 leading-5">{l}</div>)}
            <div ref={logsEndRef} />
          </div>
        </div>

        {result && (
          <div className={`flex-none rounded-lg border p-4 ${result.status === 'PASS' ? 'bg-emerald-950 border-emerald-800' : 'bg-red-950 border-red-800'}`}>
            <div className="flex items-center gap-2 mb-2">
              <span className="text-lg">{result.status === 'PASS' ? '✅' : '❌'}</span>
              <span className="font-semibold text-sm">{result.status} — {result.title}</span>
              <span className="ml-auto text-xs text-gray-400">{result.steps_passed}/{result.steps_executed} 통과</span>
            </div>
            {result.error_message && (
              <div className="text-xs text-red-400 mb-2">⚠️ {result.error_message}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
