import { useEffect, useMemo, useRef, useState } from 'react'
import { isAxiosError } from 'axios'
import { Play, Square, RefreshCw, Loader2, ArrowLeft, FileText, Save, Pencil, Braces, Sparkles, Plus, GripVertical, X } from 'lucide-react'
import { templateApi, testApi, apkApi, planApi, wsUrl, debugSince } from '../../api/client'
import { stepsToYaml } from '../../lib/template'
import { extractParams, substituteSteps } from '../../lib/params'
import StepEditor, { newId } from './StepEditor'
import ScreenPreview from '../ScreenPreview'
import type { AdaptiveRun, Step, TemplateParam, TestResult } from '../../types'

interface Props {
  device: string
  selectedPackage: string
  onBack: () => void
  onComplete: (result: TestResult, since: string, adaptive?: AdaptiveRun) => void
  // 0보다 큰 값으로 바뀔 때마다 편집 중이던 스텝을 초기화 (처음으로 버튼 등 완전 재시작용)
  resetSignal?: number
}

type Mode = 'select' | 'create'

const PACKAGE_ACTIONS = new Set([
  'launch_app',
  'close_app',
  'uninstall_app',
  'skip_tutorial',
  'enter_sr_debugger',
])

const PACKAGE_TOKEN = /^\{\{\s*package\s*\}\}$/

const KNOWN_PARAM_DEFINITIONS: Record<string, Omit<TemplateParam, 'name'>> = {
  package: {
    label: '앱 패키지명',
    description: '실행할 Android 앱의 패키지명',
    example: 'com.percent.aos.cooptd',
  },
}

function fallbackParamDefinition(name: string): TemplateParam {
  return {
    name,
    ...KNOWN_PARAM_DEFINITIONS[name],
    label: KNOWN_PARAM_DEFINITIONS[name]?.label ?? name.replace(/[._-]+/g, ' '),
  }
}

interface PipelineSegment {
  id: string
  name: string
  stepIds: string[]
}

interface PipelinePreviewSegment {
  id: string
  name: string
  stepNumbers: number[]
}

interface TemplateBlock {
  id: string
  name: string
  stepNumbers: number[]
}

function segmentId() {
  return `segment_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
}

function makePipelinePreview(ids: string[], segments: PipelineSegment[]): PipelinePreviewSegment[] {
  const owner = new Map<string, { segmentId: string; name: string }>()
  for (const segment of segments) {
    for (const id of segment.stepIds) owner.set(id, { segmentId: segment.id, name: segment.name })
  }

  const out: PipelinePreviewSegment[] = []
  ids.forEach((id, idx) => {
    const meta = owner.get(id) ?? { segmentId: 'manual', name: '직접 추가' }
    const last = out[out.length - 1]
    if (last && last.id.startsWith(`${meta.segmentId}_`) && last.name === meta.name) {
      last.stepNumbers.push(idx + 1)
    } else {
      out.push({ id: `${meta.segmentId}_${idx}`, name: meta.name, stepNumbers: [idx + 1] })
    }
  })
  return out
}

function makeTemplateBlocks(ids: string[], segments: PipelineSegment[]): TemplateBlock[] {
  const positions = new Map(ids.map((id, index) => [id, index + 1]))
  return segments
    .map((segment) => ({
      id: segment.id,
      name: segment.name,
      stepNumbers: segment.stepIds
        .map((id) => positions.get(id))
        .filter((value): value is number => value !== undefined)
        .sort((a, b) => a - b),
    }))
    .filter((segment) => segment.stepNumbers.length > 0)
    .sort((a, b) => a.stepNumbers[0] - b.stepNumbers[0])
}

function formatStepNumbers(numbers: number[]) {
  if (!numbers.length) return ''
  const contiguous = numbers.every((value, index) => index === 0 || value === numbers[index - 1] + 1)
  return contiguous && numbers.length > 1
    ? `step ${numbers[0]}-${numbers[numbers.length - 1]}`
    : `step ${numbers.join(', ')}`
}

export default function RunStep({ device, selectedPackage, onBack, onComplete, resetSignal }: Props) {
  const [mode, setMode] = useState<Mode>('select')
  const [templates, setTemplates] = useState<string[]>([])
  const [selected, setSelected] = useState('')
  const [title, setTitle] = useState('')
  const [steps, setSteps] = useState<Step[]>([])
  const [stepIds, setStepIds] = useState<string[]>([])
  const [pipelineSegments, setPipelineSegments] = useState<PipelineSegment[]>([])
  const [logs, setLogs] = useState<string[]>([])
  const [status, setStatus] = useState('')
  const [running, setRunning] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [saving, setSaving] = useState(false)
  const [paramValues, setParamValues] = useState<Record<string, string>>({})
  const [paramDefinitions, setParamDefinitions] = useState<Record<string, TemplateParam>>({})
  const [apks, setApks] = useState<string[]>([])
  const [scenario, setScenario] = useState('')
  const [generating, setGenerating] = useState(false)
  const [draggedSegmentId, setDraggedSegmentId] = useState('')
  const [segmentDropHint, setSegmentDropHint] = useState<{ id: string; edge: 'before' | 'after' } | null>(null)
  const [refreshingTemplates, setRefreshingTemplates] = useState(false)
  const [templatesRefreshed, setTemplatesRefreshed] = useState(false)
  const logsEndRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const sessionRef = useRef('')
  const resultRef = useRef<TestResult | null>(null)
  const sinceRef = useRef('')
  const pipelineRef = useRef<TestResult['pipeline'] | null>(null)

  const loadTemplates = async () => {
    const res = await templateApi.list()
    setTemplates(res.templates)
  }

  // 새로고침 아이콘 버튼 전용 — 서버에서 목록을 다시 읽어오는 동안/직후 눈에 보이는
  // 반응이 전혀 없어 "안 눌리는 버튼"처럼 보인다는 피드백을 받아 로딩 스피너 +
  // 잠깐 동안의 "갱신됨" 표시를 추가했다.
  const handleRefreshTemplates = async () => {
    setRefreshingTemplates(true)
    setTemplatesRefreshed(false)
    try {
      await loadTemplates()
      setTemplatesRefreshed(true)
      setTimeout(() => setTemplatesRefreshed(false), 1500)
    } finally {
      setRefreshingTemplates(false)
    }
  }

  useEffect(() => {
    loadTemplates()
    apkApi.list().then((r) => setApks(r.apks)).catch(() => {})
  }, [])
  useEffect(() => { logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [logs])
  useEffect(() => () => wsRef.current?.close(), [])

  const paramNames = useMemo(() => extractParams(steps), [steps])
  const templateBlocks = useMemo(() => makeTemplateBlocks(stepIds, pipelineSegments), [pipelineSegments, stepIds])
  const stepGroupNames = useMemo(() => {
    const groups: Record<string, string> = {}
    for (const segment of pipelineSegments) {
      for (const id of segment.stepIds) groups[id] = segment.name
    }
    return groups
  }, [pipelineSegments])

  const resetEditing = () => {
    setSteps([])
    setStepIds([])
    setPipelineSegments([])
    setTitle('')
    setSelected('')
    setStatus('')
    setParamValues({})
    setParamDefinitions({})
  }

  // 이 컴포넌트는 보고서 화면과 오갈 때도 언마운트되지 않고 유지되어 방금 돌린 기록이 남는다 —
  // "처음으로"처럼 완전 재시작이 필요할 때만 부모가 resetSignal을 올려서 편집 내용을 비운다.
  useEffect(() => {
    if (!resetSignal) return
    setMode('select')
    setScenario('')
    resetEditing()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetSignal])

  const switchMode = (m: Mode) => { if (m !== mode) { setMode(m); resetEditing() } }

  const cloneSteps = (loaded: Step[]) =>
    loaded.map((s) => ({ ...s, params: s.params ? { ...s.params } : {} }))

  const templateDefaults = (params: TemplateParam[] = []) => {
    const defaults: Record<string, string> = {}
    for (const p of params) defaults[p.name] = p.default ?? ''
    return defaults
  }

  const buildPipeline = (ids: string[], segments: PipelineSegment[]): TestResult['pipeline'] => {
    const templates = makePipelinePreview(ids, segments)
      .map((segment) => {
        const start = segment.stepNumbers[0]
        const end = segment.stepNumbers[segment.stepNumbers.length - 1]
        return {
          name: segment.name,
          start_step: start,
          end_step: end,
          step_count: segment.stepNumbers.length,
        }
      })
    return { templates }
  }

  const attachPipeline = (result: TestResult): TestResult => ({
    ...result,
    pipeline: pipelineRef.current ?? undefined,
  })

  const handleTemplateLoad = async () => {
    const name = selected
    if (!name) { setStatus('⚠️ 테스트케이스를 선택하세요.'); return }
    const res = await templateApi.get(name)
    const t = res.template
    const loaded = cloneSteps(t.steps || []).map((step) => {
      const isPackageStep = PACKAGE_ACTIONS.has(step.action)
      const needsSelectedPackage = !step.target || PACKAGE_TOKEN.test(step.target)
      return selectedPackage && isPackageStep && needsSelectedPackage
        ? { ...step, target: selectedPackage }
        : step
    })
    const ids = loaded.map(() => newId())
    const label = t.title || name
    const loadedParamNames = extractParams(loaded)
    const defaults = templateDefaults(t.parameters)
    const definitions = Object.fromEntries(loadedParamNames.map((paramName) => {
      const defined = t.parameters?.find((param) => param.name === paramName)
      return [paramName, defined ?? fallbackParamDefinition(paramName)]
    }))
    const usesPackageParam = loadedParamNames.includes('package')
    const initialParams = selectedPackage && usesPackageParam
      ? { ...defaults, package: selectedPackage }
      : defaults
    const segment: PipelineSegment = { id: segmentId(), name: label, stepIds: ids }

    if (steps.length) {
      setTitle((prev) => {
        const current = prev.trim()
        if (!current) return label
        return `${current} + ${label}`
      })
      setSteps((prev) => [...prev, ...loaded])
      setStepIds((prev) => [...prev, ...ids])
      setPipelineSegments((prev) => [...prev, segment])
      setParamValues((prev) => ({ ...initialParams, ...prev, ...(selectedPackage && usesPackageParam ? { package: selectedPackage } : {}) }))
      setParamDefinitions((prev) => ({ ...prev, ...definitions }))
      setStatus(`'${label}' 스텝 ${loaded.length}개를 추가했습니다.`)
    } else {
      setTitle(label)
      setSteps(loaded)
      setStepIds(ids)
      setPipelineSegments([segment])
      setParamValues(initialParams)
      setParamDefinitions(definitions)
      setStatus('✏️ 값을 수정한 뒤 실행하거나 저장할 수 있습니다.')
    }
    setLogs([])
  }

  const moveTemplateSegment = (sourceId: string, targetId: string, edge: 'before' | 'after') => {
    if (!sourceId || sourceId === targetId) return
    const orderedIds = templateBlocks.map((segment) => segment.id)
    const withoutSource = orderedIds.filter((id) => id !== sourceId)
    const targetIndex = withoutSource.indexOf(targetId)
    if (targetIndex < 0) return
    const insertionIndex = targetIndex + (edge === 'after' ? 1 : 0)
    const nextSegmentOrder = [...withoutSource]
    nextSegmentOrder.splice(insertionIndex, 0, sourceId)

    const ownerByStep = new Map<string, string>()
    for (const segment of pipelineSegments) {
      for (const id of segment.stepIds) ownerByStep.set(id, segment.id)
    }
    const idsBySegment = new Map<string, string[]>()
    for (const id of stepIds) {
      const owner = ownerByStep.get(id)
      if (!owner) continue
      idsBySegment.set(owner, [...(idsBySegment.get(owner) ?? []), id])
    }
    const nextIds = nextSegmentOrder.flatMap((id) => idsBySegment.get(id) ?? [])
    const knownIds = new Set(nextIds)
    nextIds.push(...stepIds.filter((id) => !knownIds.has(id)))
    const stepById = new Map(stepIds.map((id, index) => [id, steps[index]]))
    const nextSteps = nextIds.map((id) => stepById.get(id)).filter((step): step is Step => !!step)

    setStepIds(nextIds)
    setSteps(nextSteps)
    const segmentById = new Map(pipelineSegments.map((segment) => [segment.id, segment]))
    const nextSegments = nextSegmentOrder
      .map((id) => segmentById.get(id))
      .filter((segment): segment is PipelineSegment => !!segment)
    setPipelineSegments(nextSegments)
    const orderedNames = nextSegments.filter((segment) => segment.name !== '직접 추가').map((segment) => segment.name)
    if (orderedNames.length) setTitle(orderedNames.join(' + '))
    setStatus('템플릿 순서를 변경했습니다.')
  }

  const removeTemplateSegment = (segmentId: string) => {
    const segment = pipelineSegments.find((item) => item.id === segmentId)
    if (!segment) return
    const removedIds = new Set(segment.stepIds)
    const nextIds = stepIds.filter((id) => !removedIds.has(id))
    const nextSteps = steps.filter((_, index) => !removedIds.has(stepIds[index]))
    const nextSegments = pipelineSegments.filter((item) => item.id !== segmentId)
    const orderedNames = makeTemplateBlocks(nextIds, nextSegments)
      .filter((item) => item.name !== '직접 추가')
      .map((item) => item.name)
    const nextParamNames = new Set(extractParams(nextSteps))

    setStepIds(nextIds)
    setSteps(nextSteps)
    setPipelineSegments(nextSegments)
    setTitle(orderedNames.join(' + '))
    setParamValues((values) => Object.fromEntries(
      Object.entries(values).filter(([name]) => nextParamNames.has(name))
    ))
    setParamDefinitions((definitions) => Object.fromEntries(
      Object.entries(definitions).filter(([name]) => nextParamNames.has(name))
    ))
    setStatus(`'${segment.name}' 템플릿과 소속 스텝을 삭제했습니다.`)
  }

  const handleClearLoaded = () => {
    setSteps([])
    setStepIds([])
    setPipelineSegments([])
    setTitle('')
    setParamValues({})
    setParamDefinitions({})
    setStatus('🧹 선택한 테스트 구성을 비웠습니다.')
    setLogs([])
  }

  const handleEditorChange = (nextSteps: Step[], nextIds: string[]) => {
    setSteps(nextSteps)
    setStepIds(nextIds)
    setPipelineSegments((prev) => {
      const known = new Set(prev.flatMap((segment) => segment.stepIds))
      const nextIdSet = new Set(nextIds)
      const kept = prev
        .map((segment) => ({
          ...segment,
          stepIds: segment.stepIds.filter((id) => nextIdSet.has(id)),
        }))
        .filter((segment) => segment.stepIds.length > 0)
      const added = nextIds.filter((id) => !known.has(id))
      if (!added.length) return kept
      const manual = kept.find((segment) => segment.name === '직접 추가')
      if (manual) {
        return kept.map((segment) => (
          segment.id === manual.id ? { ...segment, stepIds: [...segment.stepIds, ...added] } : segment
        ))
      }
      return [...kept, { id: segmentId(), name: '직접 추가', stepIds: added }]
    })
  }

  // 자연어 시나리오 → 플래너가 검증된 템플릿을 조합해 스텝 생성 → 편집기에 로드
  const handleGenerate = async () => {
    if (!scenario.trim()) { setStatus('⚠️ 시나리오를 자연어로 입력하세요.'); return }
    setGenerating(true)
    setStatus('🤖 검증된 템플릿을 참조해 스텝 생성 중... (수십 초)')
    try {
      const res = await planApi.generate(scenario.trim(), selectedPackage)
      const generated = (res.plan?.steps ?? []) as Step[]
      const ids = generated.map(() => newId())
      const label = res.title || '생성된 테스트'
      setTitle(res.title || '생성된 테스트')
      setSteps(generated)
      setStepIds(ids)
      setPipelineSegments([{ id: segmentId(), name: label, stepIds: ids }])
      const defaults: Record<string, string> = {}
      setParamValues(defaults)
      setParamDefinitions(Object.fromEntries(
        extractParams(generated).map((name) => [name, fallbackParamDefinition(name)])
      ))
      setStatus(`✅ 스텝 ${res.steps_count}개 생성 — 검토·수정 후 실행하거나 저장하세요.`)
    } catch (e) {
      setStatus(`❌ 생성 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setGenerating(false)
    }
  }

  const handleSave = async () => {
    if (!title.trim()) { setStatus('⚠️ 테스트케이스 이름을 입력하세요.'); return }
    if (!steps.length) { setStatus('⚠️ 스텝을 하나 이상 추가하세요.'); return }
    setSaving(true)
    try {
      // 현재 입력값을 파라미터 기본값으로 저장 (플레이스홀더는 스텝에 그대로 보존)
      const params: TemplateParam[] = paramNames.map((name) => ({
        ...(paramDefinitions[name] ?? fallbackParamDefinition(name)),
        name,
        default: paramValues[name] ?? '',
      }))
      const yaml = stepsToYaml(title.trim(), selectedPackage, steps, params)
      const res = await templateApi.save(title.trim(), yaml)
      if (res.success) {
        setStatus(`💾 저장 완료 → ${res.filename ?? title.trim()}`)
        await loadTemplates()
      } else {
        setStatus(`❌ 저장 실패: ${res.error ?? '알 수 없음'}`)
      }
    } catch (e) {
      setStatus(`❌ 저장 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  const handleRun = async () => {
    if (!steps.length) { setStatus('⚠️ 실행할 스텝이 없습니다.'); return }
    const missing = paramNames.filter((n) => !(paramValues[n] ?? '').trim())
    if (missing.length) { setStatus(`⚠️ 파라미터 값을 입력하세요: ${missing.join(', ')}`); return }
    return handleRunNormal()
  }

  const handleRunNormal = async () => {
    setRunning(true)
    setLogs([])
    setStatus('🔄 실행 중...')
    resultRef.current = null
    pipelineRef.current = buildPipeline(stepIds, pipelineSegments)

    const sid = `sess_${Date.now()}`
    sessionRef.current = sid
    sinceRef.current = debugSince()

    const ws = new WebSocket(wsUrl(sid))
    wsRef.current = ws
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'log') setLogs((p) => [...p, msg.message])
      else if (msg.type === 'result') { resultRef.current = attachPipeline(msg.data); setStatus('✅ 실행 완료') }
      else if (msg.type === 'error') { setStatus(`❌ ${msg.message}`); setRunning(false) }
      else if (msg.type === 'done') {
        setRunning(false)
        setStopping(false)
        ws.close()
        if (resultRef.current) onComplete(resultRef.current, sinceRef.current)
      }
    }
    ws.onerror = () => { setStatus('❌ WebSocket 연결 오류'); setRunning(false) }

    // 파라미터 치환 후, 앱 제어 스텝은 앞 단계에서 선택한 게임 패키지로 보정
    const resolved = substituteSteps(steps, paramValues)
    const runSteps = resolved.map((s) => (
      PACKAGE_ACTIONS.has(s.action) && !s.target ? { ...s, target: selectedPackage } : s
    ))
    const pkg = runSteps.find((s) => s.action === 'launch_app' && s.target)?.target ?? selectedPackage
    try {
      await testApi.run({ title: title || '테스트 실행', package: pkg, steps: runSteps, session_id: sid, device })
    } catch (e) {
      const detail = isAxiosError(e)
        ? (e.response?.data as { detail?: string } | undefined)?.detail ?? e.message
        : e instanceof Error ? e.message : String(e)
      setStatus(`❌ 실행 시작 실패: ${detail}`)
      setRunning(false)
      setStopping(false)
      ws.close()
    }
  }

  const handleStop = async () => {
    setStopping(true)
    setStatus('⏹️ 중단 중...')
    await testApi.stop(sessionRef.current)
  }

  return (
    <div className="max-w-6xl mx-auto w-full flex gap-4 h-full min-h-0">
      {/* 왼쪽: 모드 토글 + 편집 */}
      <div className="flex flex-col gap-3 w-1/2 min-w-0">
        {/* 모드 토글 */}
        <div className="flex rounded-lg border border-gray-700 overflow-hidden text-sm flex-none">
          {(['select', 'create'] as Mode[]).map((m) => (
            <button key={m} onClick={() => switchMode(m)} disabled={running}
              className={`flex-1 py-2 font-medium transition-colors disabled:opacity-50 ${
                mode === m ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-gray-200'
              }`}>
              {m === 'select' ? '기존 선택' : '새로 만들기'}
            </button>
          ))}
        </div>

        {mode === 'select' && (
          <div className="flex-none rounded-lg border border-gray-800 bg-gray-900/60 p-2.5 space-y-2">
            <div className="flex flex-wrap items-end gap-2">
              <div className="flex-1 min-w-[180px]">
                <label className="block text-xs text-gray-400 mb-1">테스트케이스</label>
                <select value={selected} onChange={(e) => setSelected(e.target.value)} disabled={running}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500 disabled:opacity-60">
                  <option value="">테스트케이스 선택</option>
                  {templates.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <button onClick={handleTemplateLoad} disabled={running || !selected}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-blue-700 hover:bg-blue-600 text-xs font-medium disabled:opacity-50 transition-colors">
                <Plus size={14} /> 불러오기
              </button>
              <button onClick={handleRefreshTemplates} disabled={running || refreshingTemplates}
                className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-white disabled:opacity-50 transition-colors"
                title="목록 새로고침">
                {refreshingTemplates ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />}
              </button>
              {templatesRefreshed && (
                <span className="text-xs text-emerald-400">✓ 목록 갱신됨</span>
              )}
            </div>
            {templateBlocks.length > 0 && (
              <div className="flex items-start gap-2 min-h-7">
                <div className="flex-1 min-w-0 flex flex-wrap items-center gap-1">
                  {templateBlocks.map((segment) => (
                    <div key={segment.id}
                      draggable={!running}
                      onDragStart={(e) => {
                        e.dataTransfer.effectAllowed = 'move'
                        e.dataTransfer.setData('application/x-autoqa-template', segment.id)
                        setDraggedSegmentId(segment.id)
                      }}
                      onDragOver={(e) => {
                        if (!e.dataTransfer.types.includes('application/x-autoqa-template')) return
                        e.preventDefault()
                        if (draggedSegmentId === segment.id) return
                        const rect = e.currentTarget.getBoundingClientRect()
                        setSegmentDropHint({ id: segment.id, edge: e.clientX < rect.left + rect.width / 2 ? 'before' : 'after' })
                      }}
                      onDrop={(e) => {
                        e.preventDefault()
                        const sourceId = draggedSegmentId || e.dataTransfer.getData('application/x-autoqa-template')
                        const edge = segmentDropHint?.id === segment.id ? segmentDropHint.edge : 'before'
                        moveTemplateSegment(sourceId, segment.id, edge)
                        setDraggedSegmentId('')
                        setSegmentDropHint(null)
                      }}
                      onDragEnd={() => { setDraggedSegmentId(''); setSegmentDropHint(null) }}
                      title="템플릿 이동"
                      className={`relative flex max-w-full items-center gap-1 px-2 py-1 rounded border text-[11px] select-none transition-colors ${
                        draggedSegmentId === segment.id
                          ? 'opacity-40 border-blue-500 bg-blue-950/60'
                          : 'border-blue-800/60 bg-blue-950/50 text-blue-200'
                      } ${running ? 'cursor-default' : 'cursor-grab active:cursor-grabbing'} ${segmentDropHint?.id === segment.id && segmentDropHint.edge === 'before' ? 'before:absolute before:-left-1 before:top-0 before:bottom-0 before:w-0.5 before:bg-cyan-400' : ''}
                      ${segmentDropHint?.id === segment.id && segmentDropHint.edge === 'after' ? 'after:absolute after:-right-1 after:top-0 after:bottom-0 after:w-0.5 after:bg-cyan-400' : ''}`}>
                      <GripVertical size={12} className="text-blue-400 cursor-grab active:cursor-grabbing" />
                      <span className="max-w-[14rem] truncate font-medium">[{segment.name}]</span>
                      <span className="flex-none text-blue-400/70">{formatStepNumbers(segment.stepNumbers)}</span>
                      <button type="button" draggable={false} disabled={running}
                        onClick={(e) => { e.stopPropagation(); removeTemplateSegment(segment.id) }}
                        onMouseDown={(e) => e.stopPropagation()}
                        className="ml-0.5 flex-none rounded p-0.5 text-blue-400/70 hover:bg-red-950 hover:text-red-300 disabled:opacity-40"
                        title="템플릿과 소속 스텝 삭제">
                        <X size={12} />
                      </button>
                    </div>
                  ))}
                  <span className="px-2 py-1 text-[11px] text-gray-500">총 {steps.length} 스텝</span>
                </div>
                <button onClick={handleClearLoaded} disabled={running}
                  className="p-1 rounded text-gray-500 hover:text-gray-200 hover:bg-gray-800 disabled:opacity-40"
                  title="구성 비우기">
                  <X size={14} />
                </button>
              </div>
            )}
          </div>
        )}

        {mode === 'create' && (
          <div className="flex-none rounded-lg border border-fuchsia-800/60 bg-fuchsia-950/20 p-2.5 space-y-2">
            <p className="text-xs text-fuchsia-300 flex items-center gap-1.5">
              <Sparkles size={13} /> 자연어로 시나리오를 쓰면 검증된 템플릿을 조합해 스텝을 생성합니다
            </p>
            <textarea value={scenario} onChange={(e) => setScenario(e.target.value)}
              disabled={running || generating} rows={3}
              placeholder={'예: 앱 실행 후 구글 재로그인하고, 상점에서 마신석 50 상품을 구매한 뒤 다이아가 감소했는지 검증'}
              className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-xs resize-y focus:outline-none focus:border-fuchsia-500 disabled:opacity-50" />
            <div className="flex justify-end">
              <button onClick={handleGenerate} disabled={running || generating || !scenario.trim()}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-fuchsia-700 hover:bg-fuchsia-600 disabled:opacity-50 text-xs font-medium transition-colors">
                {generating ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
                {generating ? '생성 중...' : '스텝 생성'}
              </button>
            </div>
          </div>
        )}

        {/* 이름 (저장용) */}
        <div className="flex items-center gap-2 flex-none">
          <Pencil size={14} className="text-gray-500 flex-none" />
          <input value={title} onChange={(e) => setTitle(e.target.value)} disabled={running}
            placeholder="테스트케이스 이름"
            className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500 disabled:opacity-60" />
        </div>

        {/* 파라미터 패널 — 스텝에 {{name}} 플레이스홀더가 있으면 자동 노출 */}
        {paramNames.length > 0 && (
          <div className="flex-none rounded-lg border border-indigo-800/60 bg-indigo-950/30 p-2.5 space-y-1.5">
            <p className="text-xs text-indigo-300 flex items-center gap-1.5">
              <Braces size={13} /> 파라미터 — 실행 시 값을 채웁니다
            </p>
            {paramNames.map((name) => {
              const definition = paramDefinitions[name] ?? fallbackParamDefinition(name)
              const placeholder = definition.placeholder
                || (definition.example ? `예: ${definition.example}` : `${definition.label} 입력`)

              return (
                <div key={name} className="grid grid-cols-[9rem_minmax(0,1fr)] items-start gap-2 rounded border border-indigo-900/50 bg-gray-950/40 p-2">
                  <div className="min-w-0">
                    <p className="truncate text-xs font-medium text-indigo-200">{definition.label}</p>
                    <p className="truncate text-[10px] font-mono text-indigo-400">{`{{${name}}}`}</p>
                  </div>
                  <div className="min-w-0 space-y-1">
                    <input
                      value={paramValues[name] ?? ''}
                      disabled={running}
                      onChange={(e) => setParamValues((v) => ({ ...v, [name]: e.target.value }))}
                      placeholder={placeholder}
                      className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-indigo-500 disabled:opacity-50"
                    />
                    {(definition.description || definition.example) && (
                      <p className="text-[10px] leading-4 text-gray-500">
                        {definition.description}
                        {definition.description && definition.example ? ' · ' : ''}
                        {definition.example ? `예시: ${definition.example}` : ''}
                      </p>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {/* 스텝 편집기 (기존 선택도 값 수정 가능) */}
        <div className="flex-1 min-h-0 overflow-y-auto scrollbar-thin pr-1">
          <StepEditor steps={steps} ids={stepIds} stepGroupNames={stepGroupNames}
            selectedPackage={selectedPackage} apks={apks} disabled={running} onChange={handleEditorChange} />
        </div>

        <div className="text-xs text-gray-500 flex items-center gap-1.5 flex-none">
          <FileText size={13} /> {selectedPackage}
        </div>

        {/* 컨트롤 */}
        <div className="flex flex-wrap items-center gap-2 flex-none">
          <button onClick={onBack} disabled={running}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-sm disabled:opacity-50 transition-colors">
            <ArrowLeft size={15} /> 게임 변경
          </button>
          <button onClick={handleRun} disabled={running || !steps.length}
            className="flex items-center gap-2 px-4 py-2 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors bg-green-700 hover:bg-green-600">
            {running ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
            {running ? '실행 중...' : '실행'}
          </button>
          <button onClick={handleSave} disabled={saving || running || !steps.length}
            className="flex items-center gap-2 px-3 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors">
            {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
            저장
          </button>
          {running && (
            <button onClick={handleStop} disabled={stopping}
              className="flex items-center gap-2 px-4 py-2 bg-red-700 hover:bg-red-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors">
              {stopping ? <Loader2 size={16} className="animate-spin" /> : <Square size={16} />}
              중단
            </button>
          )}
        </div>
        {status && <span className="text-xs text-gray-400 flex-none">{status}</span>}
      </div>

      {/* 오른쪽: 실시간 로그 */}
      <div className="flex flex-col flex-1 min-w-0 min-h-0">
        <label className="block text-xs text-gray-400 mb-1 flex-none">
          실시간 실행 로그
          {running && <span className="ml-2 text-green-400 animate-pulse">● 실행 중</span>}
        </label>
        <div className="flex-1 min-h-0 overflow-y-auto scrollbar-thin bg-gray-900 border border-gray-700 rounded-lg p-3 font-mono text-xs space-y-0.5">
          {logs.length === 0 && !running && (
            <p className="text-gray-600 py-4 text-center">실행하면 로그가 여기에 표시됩니다.</p>
          )}
          {logs.map((l, i) => <div key={i} className="text-gray-300 leading-5">{l}</div>)}
          <div ref={logsEndRef} />
        </div>
      </div>

      {/* 맨 오른쪽: 기기 화면 미리보기 (실행 전 확인 + 실행 중 모니터링) */}
      <ScreenPreview device={device} running={running} />
    </div>
  )
}
