import { useEffect, useMemo, useRef, useState } from 'react'
import { Play, Square, RefreshCw, Loader2, ArrowLeft, FileText, Save, Pencil, Braces, Sparkles, Plus, Layers, X } from 'lucide-react'
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
}

type Mode = 'select' | 'create'

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

export default function RunStep({ device, selectedPackage, onBack, onComplete }: Props) {
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
  const [apks, setApks] = useState<string[]>([])
  const [scenario, setScenario] = useState('')
  const [generating, setGenerating] = useState(false)
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

  useEffect(() => {
    loadTemplates()
    apkApi.list().then((r) => setApks(r.apks)).catch(() => {})
  }, [])
  useEffect(() => { logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [logs])
  useEffect(() => () => wsRef.current?.close(), [])

  const paramNames = useMemo(() => extractParams(steps), [steps])
  const pipelinePreview = useMemo(() => makePipelinePreview(stepIds, pipelineSegments), [pipelineSegments, stepIds])

  const resetEditing = () => {
    setSteps([])
    setStepIds([])
    setPipelineSegments([])
    setTitle('')
    setSelected('')
    setStatus('')
    setParamValues({})
  }

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

  const handleTemplateLoad = async (append: boolean) => {
    const name = selected
    if (!name) { setStatus('⚠️ 테스트케이스를 선택하세요.'); return }
    const res = await templateApi.get(name)
    const t = res.template
    const loaded = cloneSteps(t.steps || [])
    const ids = loaded.map(() => newId())
    const label = t.title || name
    const defaults = templateDefaults(t.parameters)
    const segment: PipelineSegment = { id: segmentId(), name: label, stepIds: ids }

    if (append && steps.length) {
      setTitle((prev) => prev.trim() ? `${prev.trim()} + ${label}` : label)
      setSteps((prev) => [...prev, ...loaded])
      setStepIds((prev) => [...prev, ...ids])
      setPipelineSegments((prev) => [...prev, segment])
      setParamValues((prev) => ({ ...defaults, ...prev }))
      setStatus(`➕ '${label}' 스텝 ${loaded.length}개를 뒤에 붙였습니다.`)
    } else {
      setTitle(label)
      setSteps(loaded)
      setStepIds(ids)
      setPipelineSegments([segment])
      setParamValues(defaults)
      setStatus('✏️ 값을 수정한 뒤 실행하거나 저장할 수 있습니다.')
    }
    setLogs([])
  }

  const handleClearLoaded = () => {
    setSteps([])
    setStepIds([])
    setPipelineSegments([])
    setTitle('')
    setParamValues({})
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
      const params: TemplateParam[] = paramNames.map((name) => ({ name, default: paramValues[name] ?? '' }))
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

    // 파라미터 치환 후, launch_app 스텝에 target이 없으면 선택한 게임 패키지로 보정
    const resolved = substituteSteps(steps, paramValues)
    const runSteps = resolved.map((s) => (s.action === 'launch_app' && !s.target ? { ...s, target: selectedPackage } : s))
    const pkg = runSteps.find((s) => s.action === 'launch_app' && s.target)?.target ?? selectedPackage
    await testApi.run({ title: title || '테스트 실행', package: pkg, steps: runSteps, session_id: sid, device })
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
            <div className="flex items-end gap-2">
              <div className="flex-1 min-w-0">
                <label className="block text-xs text-gray-400 mb-1">테스트케이스</label>
                <select value={selected} onChange={(e) => setSelected(e.target.value)} disabled={running}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500 disabled:opacity-60">
                  <option value="">테스트케이스 선택</option>
                  {templates.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <button onClick={() => handleTemplateLoad(false)} disabled={running || !selected}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-200 disabled:opacity-50 transition-colors">
                <Layers size={14} /> 교체
              </button>
              <button onClick={() => handleTemplateLoad(true)} disabled={running || !selected}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-blue-700 hover:bg-blue-600 text-xs font-medium disabled:opacity-50 transition-colors">
                <Plus size={14} /> 뒤에 붙이기
              </button>
              <button onClick={loadTemplates} disabled={running}
                className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-white disabled:opacity-50 transition-colors"
                title="목록 새로고침">
                <RefreshCw size={16} />
              </button>
            </div>
            {pipelinePreview.length > 0 && (
              <div className="flex items-start gap-2 min-h-7">
                <div className="flex-1 min-w-0 flex flex-wrap items-center gap-1">
                  {pipelinePreview.map((segment) => (
                    <div key={segment.id} className="flex flex-wrap items-center gap-1">
                      <span className="px-2 py-1 rounded bg-blue-950/50 border border-blue-800/60 text-[11px] font-medium text-blue-200">
                        [{segment.name}]
                      </span>
                      {segment.stepNumbers.map((n) => (
                        <span key={`${segment.id}_${n}`} className="px-1.5 py-1 rounded bg-gray-800 border border-gray-700 text-[11px] text-gray-300">
                          step {n}
                        </span>
                      ))}
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
            {paramNames.map((name) => (
              <div key={name} className="flex items-center gap-2">
                <span className="flex-none w-28 truncate text-xs font-mono text-indigo-300">{`{{${name}}}`}</span>
                <input
                  value={paramValues[name] ?? ''}
                  disabled={running}
                  onChange={(e) => setParamValues((v) => ({ ...v, [name]: e.target.value }))}
                  placeholder={`${name} 값`}
                  className="flex-1 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-indigo-500 disabled:opacity-50"
                />
              </div>
            ))}
          </div>
        )}

        {/* 스텝 편집기 (기존 선택도 값 수정 가능) */}
        <div className="flex-1 min-h-0 overflow-y-auto scrollbar-thin pr-1">
          <StepEditor steps={steps} ids={stepIds} selectedPackage={selectedPackage} apks={apks} disabled={running} onChange={handleEditorChange} />
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
