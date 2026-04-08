import { createContext, memo, useCallback, useContext, useEffect, useRef, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  addEdge,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  Play, Square, Save, RefreshCw, Loader2, X, Plus, Monitor, MonitorOff,
} from 'lucide-react'
import { templateApi, pipelineApi, testApi, packageApi, apkApi, agentWsUrl } from '../../api/client'
import { StableInput } from '../StableInput'
import type { AgentInfo, Step, TestResult } from '../../types'
import { ACTION_CHOICES, TARGET_ACTIONS } from '../../types'

// ─── Context: packages / apks를 노드에 전달 ───────────────────
const NodeCtx = createContext<{ packages: string[]; apks: string[] }>({ packages: [], apks: [] })

// ─── 노드 데이터 타입 ─────────────────────────────────────────
type TNodeData = {
  label: string
  node_type: 'template' | 'step'
  // step 전용
  action?: string
  target?: string
  description?: string
  seconds?: number
  // template 확장
  expanded?: boolean
  steps?: Step[]
  saving?: boolean
  // 콜백 (직렬화 X)
  onDelete: (id: string) => void
  onUpdate?: (id: string, patch: Record<string, unknown>) => void
  onStepChange?: (nodeId: string, stepIdx: number, patch: Partial<Step>) => void
  onSave?: (nodeId: string) => void
  // 실행 상태
  status?: 'running' | 'pass' | 'fail'
}
type TNode = Node<TNodeData, 'template' | 'step'>
type TEdge = Edge

// ─── 공통: 상태 색 ────────────────────────────────────────────
function statusBorder(status?: string, selected?: boolean) {
  if (selected) return 'border-blue-500 shadow-lg shadow-blue-900/40'
  if (status === 'running') return 'border-blue-600'
  if (status === 'pass')    return 'border-emerald-700'
  if (status === 'fail')    return 'border-red-700'
  return 'border-gray-600'
}
function statusText(status?: string) {
  if (status === 'running') return <span className="text-[10px] text-blue-400 animate-pulse">실행 중...</span>
  if (status === 'pass')    return <span className="text-[10px] text-emerald-400">✅ 통과</span>
  if (status === 'fail')    return <span className="text-[10px] text-red-400">❌ 실패</span>
  return null
}

function isRecordingLog(message: string) {
  return message.includes('녹화 시작') || message.includes('녹화 완료')
}

// ─── TemplateNode ─────────────────────────────────────────────
const TemplateNode = memo(({ id, data, selected }: NodeProps<TNode>) => {
  const { packages, apks } = useContext(NodeCtx)
  const stop = (e: React.SyntheticEvent) => e.stopPropagation()
  const steps = data.steps ?? []

  return (
    <div className={`relative rounded-xl bg-gray-800 border-2 min-w-[280px] transition-colors ${statusBorder(data.status, selected)}`}>
      <Handle type="target" position={Position.Left} className="!w-3 !h-3 !bg-gray-400 !border-2 !border-gray-600" />

      {/* 헤더 */}
      <div className="flex items-center gap-2 px-3 py-2 select-none border-b border-gray-700">
        <div className="flex-1 min-w-0">
          <p className="text-[10px] text-gray-500">모듈</p>
          <p className="text-sm font-semibold text-gray-100 leading-tight truncate">{data.label}</p>
        </div>
        {statusText(data.status)}
        <span className="text-[10px] text-gray-600 flex-none">{steps.length}개</span>
        <button
          onClick={() => data.onDelete(id)}
          className="flex-none w-4 h-4 rounded-full bg-gray-700 border border-gray-600 flex items-center justify-center text-gray-400 hover:bg-red-800 hover:text-red-300 transition-colors"
        >
          <X size={8} />
        </button>
      </div>

      {/* 스텝 목록 (항상 펼쳐짐) */}
      <div className="px-2 py-2 space-y-1.5">
        {steps.length === 0 && (
          <p className="text-[10px] text-gray-600 text-center py-1">스텝 없음</p>
        )}
        {steps.map((s, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <span className="flex-none text-[9px] text-gray-600 w-4 text-right">{i + 1}</span>
            <span className={`flex-none text-[10px] font-mono w-[88px] truncate ${ACTION_COLOR[s.action] ?? 'text-gray-400'}`}>
              {s.action}
            </span>
            {PKG_ACTIONS.has(s.action) ? (
              <select
                value={s.target ?? ''}
                onChange={(e) => { stop(e); data.onStepChange?.(id, i, { target: e.target.value }) }}
                onClick={stop} onMouseDown={stop}
                className="nodrag flex-1 min-w-0 bg-gray-900 border border-gray-600 rounded px-1 py-0.5 text-[10px] focus:outline-none focus:border-emerald-500"
              >
                <option value="">패키지 선택</option>
                {packages.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            ) : APK_ACTIONS.has(s.action) ? (
              <select
                value={s.target ?? ''}
                onChange={(e) => { stop(e); data.onStepChange?.(id, i, { target: e.target.value }) }}
                onClick={stop} onMouseDown={stop}
                className="nodrag flex-1 min-w-0 bg-gray-900 border border-gray-600 rounded px-1 py-0.5 text-[10px] focus:outline-none focus:border-green-500"
              >
                <option value="">APK 선택</option>
                {apks.map((apk) => <option key={apk} value={apk}>{apk}</option>)}
              </select>
            ) : s.action === 'wait' ? (
              <div className="flex flex-1 items-center gap-1">
                <input
                  type="number"
                  min={1}
                  value={(s.params?.seconds as number | undefined) ?? ''}
                  onChange={(e) => {
                    stop(e)
                    const value = e.target.value
                    data.onStepChange?.(id, i, {
                      params: {
                        ...s.params,
                        seconds: value === '' ? undefined : Number(value),
                      },
                    })
                  }}
                  onClick={stop} onMouseDown={stop}
                  placeholder="seconds"
                  className="nodrag flex-1 min-w-0 bg-gray-900 border border-gray-600 rounded px-1 py-0.5 text-[10px] focus:outline-none focus:border-purple-500"
                />
                <span className="text-[10px] text-gray-500 flex-none">초</span>
              </div>
            ) : TARGET_ACTIONS.has(s.action) ? (
              <StableInput
                value={s.target ?? ''}
                onValueChange={(value) => { data.onStepChange?.(id, i, { target: value }) }}
                onClick={stop} onMouseDown={stop}
                placeholder="target"
                className="nodrag flex-1 min-w-0 bg-gray-900 border border-gray-600 rounded px-1 py-0.5 text-[10px] focus:outline-none focus:border-blue-500"
              />
            ) : (
              <span className="flex-1 text-[10px] text-gray-600">—</span>
            )}
          </div>
        ))}
      </div>

      <Handle type="source" position={Position.Right} className="!w-3 !h-3 !bg-blue-400 !border-2 !border-blue-600" />
    </div>
  )
})

// ─── StepNode ─────────────────────────────────────────────────
const PKG_ACTIONS = new Set(['launch_app', 'uninstall_app', 'skip_tutorial'])
const APK_ACTIONS = new Set(['install_app'])
const ACTION_COLOR: Record<string, string> = {
  find_and_tap: 'text-blue-400',
  verify:       'text-yellow-400',
  launch_app:   'text-emerald-400',
  close_app:    'text-red-400',
  wait:         'text-purple-400',
  back:         'text-gray-400',
  home:         'text-gray-400',
  input_text:   'text-cyan-400',
  swipe:        'text-orange-400',
  install_app:  'text-green-400',
  uninstall_app:'text-red-300',
}

const StepNode = memo(({ id, data, selected }: NodeProps<TNode>) => {
  const { packages, apks } = useContext(NodeCtx)
  const action = data.action || 'find_and_tap'
  const target = data.target || ''
  const stop = (e: React.SyntheticEvent) => e.stopPropagation()

  const handleAction = (val: string) => {
    let newTarget = target
    if (PKG_ACTIONS.has(val)) newTarget = packages[0] ?? ''
    else if (APK_ACTIONS.has(val)) newTarget = apks[0] ?? ''
    else if (!TARGET_ACTIONS.has(val)) newTarget = ''
    data.onUpdate?.(id, {
      action: val,
      label: val,
      target: newTarget,
      seconds: val === 'wait' ? data.seconds : undefined,
    })
  }

  return (
    <div className={`relative px-3 py-2.5 rounded-xl bg-gray-800 border-2 min-w-[220px] transition-colors ${statusBorder(data.status, selected)}`}>
      <Handle type="target" position={Position.Left} className="!w-3 !h-3 !bg-gray-400 !border-2 !border-gray-600" />
      <button
        onClick={() => data.onDelete(id)}
        className="absolute -top-2 -right-2 w-5 h-5 rounded-full bg-gray-700 border border-gray-600 flex items-center justify-center text-gray-400 hover:bg-red-800 hover:text-red-300 hover:border-red-600 transition-colors z-10"
      >
        <X size={10} />
      </button>

      <p className="text-[10px] text-gray-500 mb-1">단일 스텝</p>

      {/* action */}
      <select
        value={action}
        onChange={(e) => { stop(e); handleAction(e.target.value) }}
        onClick={stop}
        className="nodrag w-full bg-gray-900 border border-gray-600 rounded px-1.5 py-1 text-xs mb-1.5 focus:outline-none focus:border-blue-500"
      >
        {ACTION_CHOICES.map((a) => <option key={a} value={a}>{a}</option>)}
      </select>

      {/* wait: seconds 입력 */}
      {action === 'wait' && (
        <div className="flex items-center gap-1.5 mb-1.5">
          <span className="text-[10px] text-gray-500 flex-none">대기</span>
          <input
            type="number" min={1}
            value={data.seconds ?? ''}
            onChange={(e) => {
              stop(e)
              const value = e.target.value
              data.onUpdate?.(id, { seconds: value === '' ? undefined : Number(value) })
            }}
            onClick={stop} onMouseDown={stop}
            className="nodrag flex-1 bg-gray-900 border border-gray-600 rounded px-1.5 py-1 text-xs focus:outline-none focus:border-purple-500"
          />
          <span className="text-[10px] text-gray-500 flex-none">초</span>
        </div>
      )}

      {/* target — action 타입에 따라 드롭다운 또는 input */}
      {action === 'wait' ? null : PKG_ACTIONS.has(action) ? (
        <select
          value={target}
          onChange={(e) => { stop(e); data.onUpdate?.(id, { target: e.target.value }) }}
          onClick={stop}
          onMouseDown={stop}
          className="nodrag w-full bg-gray-900 border border-gray-600 rounded px-1.5 py-1 text-xs focus:outline-none focus:border-emerald-500"
        >
          <option value="">패키지 선택</option>
          {packages.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      ) : APK_ACTIONS.has(action) ? (
        <select
          value={target}
          onChange={(e) => { stop(e); data.onUpdate?.(id, { target: e.target.value }) }}
          onClick={stop}
          onMouseDown={stop}
          className="nodrag w-full bg-gray-900 border border-gray-600 rounded px-1.5 py-1 text-xs focus:outline-none focus:border-green-500"
        >
          <option value="">APK 선택</option>
          {apks.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
      ) : (
        <StableInput
          value={target}
          onValueChange={(value) => { data.onUpdate?.(id, { target: value }) }}
          onClick={stop}
          onMouseDown={stop}
          disabled={!TARGET_ACTIONS.has(action)}
          placeholder={TARGET_ACTIONS.has(action) ? 'target (UI 요소명)' : '—'}
          className="nodrag w-full bg-gray-900 border border-gray-600 rounded px-1.5 py-1 text-xs focus:outline-none focus:border-blue-500 disabled:opacity-40"
        />
      )}

      <div className="flex items-center justify-between mt-1.5">
        <span className={`text-[10px] font-mono font-medium ${ACTION_COLOR[action] || 'text-gray-400'}`}>{action}</span>
        {statusText(data.status)}
      </div>

      <Handle type="source" position={Position.Right} className="!w-3 !h-3 !bg-blue-400 !border-2 !border-blue-600" />
    </div>
  )
})


const nodeTypes = { template: TemplateNode, step: StepNode }

// ─── 메인 탭 ──────────────────────────────────────────────────
interface Props { agent: AgentInfo | null }

export default function PipelineTab({ agent }: Props) {
  const [templates, setTemplates] = useState<string[]>([])
  const [packages, setPackages] = useState<string[]>([])
  const [apks, setApks] = useState<string[]>([])
  const [savedPipelines, setSavedPipelines] = useState<string[]>([])
  const [pipelineName, setPipelineName] = useState('')
  const [nodes, setNodes, onNodesChange] = useNodesState<TNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<TEdge>([])
  const [newStepAction, setNewStepAction] = useState('find_and_tap')
  const [running, setRunning] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [record, setRecord] = useState(false)
  const [mirrorOn, setMirrorOn] = useState(false)
  const [logs, setLogs] = useState<string[]>([])
  const [result, setResult] = useState<TestResult | null>(null)
  const [status, setStatus] = useState('')
  const [screenSrc, setScreenSrc] = useState('')
  const logsEndRef = useRef<HTMLDivElement>(null)
  const nodesRef = useRef(nodes)
  const wsRef = useRef<WebSocket | null>(null)
  const sessionId = useRef(`pipe_${Date.now()}`)
  const nodeCounter = useRef(0)
  const mirrorIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    templateApi.list().then((r) => setTemplates(r.templates))
    pipelineApi.list().then((r) => setSavedPipelines(r.pipelines))
    if (agent) {
      packageApi.list(agent.name).then((r) => setPackages(r.packages))
      apkApi.list(agent.name).then((r) => setApks(r.apks))
    }
  }, [agent?.name])

  useEffect(() => { nodesRef.current = nodes }, [nodes])
  useEffect(() => { logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [logs])

  const serializeNodeData = useCallback((data: TNodeData) => ({
    label: data.label,
    node_type: data.node_type,
    action: data.action,
    target: data.target,
    description: data.description,
    seconds: data.seconds,
    steps: data.node_type === 'template' ? (data.steps ?? []).map((step) => ({ ...step })) : undefined,
  }), [])

  // ── 모듈 스텝 target 변경
  const handleStepChange = useCallback((nodeId: string, stepIdx: number, patch: Partial<Step>) => {
    setNodes((nds) => nds.map((n) =>
      n.id !== nodeId ? n : {
        ...n,
        data: {
          ...n.data,
          steps: (n.data.steps ?? []).map((s, i) => i === stepIdx ? { ...s, ...patch } : s),
        },
      }
    ))
  }, [setNodes])

  // ── 모듈 스텝 저장 (YAML 갱신)

  // 화면 미러링
  useEffect(() => {
    if (mirrorIntervalRef.current) clearInterval(mirrorIntervalRef.current)
    if (!mirrorOn) { setScreenSrc(''); return }
    const endpoint = running ? '/api/screen/latest' : '/api/screen/snapshot'
    const interval = running ? 1000 : 1500
    const refresh = () => setScreenSrc(`${endpoint}?t=${Date.now()}`)
    refresh()
    mirrorIntervalRef.current = setInterval(refresh, interval)
    return () => { if (mirrorIntervalRef.current) clearInterval(mirrorIntervalRef.current) }
  }, [mirrorOn, running])

  // ── 노드 삭제
  const handleDeleteNode = useCallback((id: string) => {
    setNodes((nds) => nds.filter((n) => n.id !== id))
    setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id))
  }, [setNodes, setEdges])

  // ── 스텝 노드 데이터 업데이트
  const handleUpdateNode = useCallback((id: string, patch: Record<string, unknown>) => {
    setNodes((nds) => nds.map((n) =>
      n.id !== id ? n : { ...n, data: { ...n.data, ...patch } }
    ))
  }, [setNodes])

  // ── 템플릿 노드 추가 (스텝 즉시 로드)
  const addTemplateNode = useCallback(async (templateName: string) => {
    const id = `tmpl_${Date.now()}_${nodeCounter.current++}`
    const x = 60 + (nodes.length % 4) * 240
    const y = 80 + Math.floor(nodes.length / 4) * 160
    let steps: Step[] = []
    try {
      const res = await templateApi.get(templateName)
      steps = res.template.steps ?? []
    } catch {}
    setNodes((nds) => [
      ...nds,
      {
        id,
        type: 'template',
        position: { x, y },
        data: {
          label: templateName,
          node_type: 'template',
          steps,
          onDelete: handleDeleteNode,
          onUpdate: handleUpdateNode,
          onStepChange: handleStepChange,
        },
      },
    ])
  }, [nodes.length, setNodes, handleDeleteNode, handleUpdateNode, handleStepChange])

  // ── 스텝 노드 추가
  const addStepNode = useCallback((action: string) => {
    const id = `step_${Date.now()}_${nodeCounter.current++}`
    const x = 60 + (nodes.length % 4) * 240
    const y = 80 + Math.floor(nodes.length / 4) * 160
    setNodes((nds) => [
      ...nds,
      {
        id,
        type: 'step',
        position: { x, y },
        data: { label: action, node_type: 'step', action, target: '', onDelete: handleDeleteNode, onUpdate: handleUpdateNode },
      },
    ])
  }, [nodes.length, setNodes, handleDeleteNode, handleUpdateNode])

  const onConnect = useCallback(
    (params: Connection) => setEdges((eds) => addEdge({ ...params, animated: true }, eds)),
    [setEdges],
  )

  const onEdgeClick = useCallback((_: React.MouseEvent, edge: TEdge) => {
    setEdges((eds) => eds.filter((e) => e.id !== edge.id))
  }, [setEdges])

  // ── 저장
  const handleSave = async () => {
    if (!pipelineName.trim()) return setStatus('⚠️ 파이프라인 이름을 입력해주세요.')
    const serialNodes = nodes.map(({ id, type, position, data }) => ({
      id, type, position,
      data: serializeNodeData(data),
    }))
    const serialEdges = edges.map(({ id, source, target }) => ({ id, source, target }))
    await pipelineApi.save(pipelineName.trim(), serialNodes, serialEdges)
    setStatus('💾 저장 완료')
    pipelineApi.list().then((r) => setSavedPipelines(r.pipelines))
  }

  // ── 불러오기 (템플릿 노드 스텝 즉시 로드)
  const handleLoad = async (name: string) => {
    if (!name) return
    const data = await pipelineApi.get(name)
    setPipelineName(data.name)
    const loaded = await Promise.all(
      data.nodes.map(async (n) => {
        const nodeType = (n.data?.node_type === 'step' ? 'step' : 'template') as 'template' | 'step'
        let steps: Step[] = []
        if (nodeType === 'template') {
          if (Array.isArray(n.data?.steps)) {
            steps = n.data.steps.map((step) => ({ ...step }))
          } else {
            try { steps = (await templateApi.get(n.data.label)).template.steps ?? [] } catch {}
          }
        }
        return {
          ...n,
          type: nodeType,
          data: {
            ...n.data,
            node_type: nodeType,
            steps: nodeType === 'template' ? steps : undefined,
            onDelete: handleDeleteNode,
            onUpdate: handleUpdateNode,
            onStepChange: nodeType === 'template' ? handleStepChange : undefined,
          },
        }
      })
    )
    setNodes(loaded)
    setEdges(data.edges.map((e) => ({ ...e, animated: true })))
    setStatus(`📂 "${data.name}" 불러옴`)
    setLogs([])
    setResult(null)
  }

  // ── 실행
  const handleRun = async () => {
    if (!agent) return setStatus('⚠️ 에이전트를 먼저 선택해주세요.')
    if (nodes.length === 0) return setStatus('⚠️ 노드를 추가해주세요.')
    setRunning(true)
    setLogs([])
    setResult(null)
    setStatus('🔄 실행 중...')
    setNodes((nds) => nds.map((n) => ({ ...n, data: { ...n.data, status: undefined } })))

    const sid = `pipe_${Date.now()}`
    sessionId.current = sid

    const ws = new WebSocket(agentWsUrl(agent, sid))
    wsRef.current = ws

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'log') {
        if (!isRecordingLog(msg.message)) setLogs((p) => [...p, msg.message])
      }
      else if (msg.type === 'result') {
        setResult(msg.data)
        setStatus(msg.data.status === 'PASS' ? '✅ 파이프라인 통과' : '❌ 파이프라인 실패')
      } else if (msg.type === 'error') {
        setStatus(`❌ ${msg.message}`)
        setRunning(false)
      } else if (msg.type === 'done') {
        setRunning(false)
        setStopping(false)
        ws.close()
      }
    }
    ws.onerror = () => { setStatus('❌ WebSocket 연결 오류'); setRunning(false) }

    const serialNodes = nodes.map(({ id, type, position, data }) => ({
      id, type, position,
      data: serializeNodeData(data),
    }))
    const serialEdges = edges.map(({ id, source, target }) => ({ id, source, target }))
    await pipelineApi.run(agent.name, { nodes: serialNodes, edges: serialEdges, session_id: sid, record })
  }

  // ── 중단
  const handleStop = async () => {
    if (!agent) return
    setStopping(true)
    setStatus('⏹️ 중단 중...')
    await testApi.stop(agent.name, sessionId.current)
  }

  return (
    <div className="flex gap-4 h-[calc(100vh-145px)]">

      {/* ── 왼쪽 패널 ── */}
      <div className="flex flex-col gap-3 w-52 flex-none overflow-y-auto scrollbar-thin">

        {/* 파이프라인 이름 + 저장/불러오기 */}
        <div className="flex flex-col gap-2">
          <label className="text-xs text-gray-400">파이프라인 이름</label>
          <StableInput
            value={pipelineName}
            onValueChange={setPipelineName}
            placeholder="이름 입력"
            className="bg-gray-800 border border-gray-700 rounded-lg px-2.5 py-1.5 text-xs focus:outline-none focus:border-blue-500"
          />
          <button
            onClick={handleSave}
            className="flex items-center justify-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded-lg text-xs transition-colors"
          >
            <Save size={13} /> 저장
          </button>
          <select
            defaultValue=""
            onChange={(e) => { handleLoad(e.target.value); e.target.value = '' }}
            className="bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:border-blue-500"
          >
            <option value="">불러오기...</option>
            {savedPipelines.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>

        <hr className="border-gray-700" />

        {/* 실행 컨트롤 */}
        <div className="flex flex-col gap-2">
          <button
            onClick={handleRun}
            disabled={running || nodes.length === 0}
            className="flex items-center justify-center gap-1.5 px-3 py-2 bg-green-700 hover:bg-green-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
          >
            {running ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            {running ? '실행 중...' : '▶ 실행'}
          </button>
          <button
            onClick={handleStop}
            disabled={!running || stopping}
            className="flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-700 hover:bg-red-600 disabled:opacity-50 rounded-lg text-sm transition-colors"
          >
            {stopping ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} />}
            {stopping ? '중단 중...' : '■ 중단'}
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
          <button
            onClick={() => setMirrorOn((v) => !v)}
            className={`flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs border transition-colors ${
              mirrorOn ? 'bg-cyan-900/50 border-cyan-700 text-cyan-400' : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            {mirrorOn ? <Monitor size={13} /> : <MonitorOff size={13} />}
            {mirrorOn ? '미러링 ON' : '미러링'}
          </button>
          {status && <p className="text-xs text-gray-400 leading-tight">{status}</p>}
        </div>

        <hr className="border-gray-700" />

        {/* 공통 모듈 팔레트 */}
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <label className="text-xs text-gray-400 font-medium">공통 모듈</label>
            <button onClick={() => templateApi.list().then((r) => setTemplates(r.templates))} className="p-0.5 text-gray-600 hover:text-gray-400">
              <RefreshCw size={11} />
            </button>
          </div>
          <p className="text-[10px] text-gray-600">클릭 → 모듈 노드 추가</p>
          <div className="space-y-1">
            {templates.map((t) => (
              <button
                key={t}
                onClick={() => addTemplateNode(t)}
                className="w-full flex items-center gap-1.5 px-2.5 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 hover:border-blue-600 rounded-lg text-xs text-gray-300 hover:text-white text-left transition-colors"
              >
                <span className="w-2 h-2 rounded-full bg-blue-500 flex-none" />
                <span className="truncate">{t}</span>
              </button>
            ))}
            {templates.length === 0 && <p className="text-xs text-gray-600 py-2 text-center">모듈 없음</p>}
          </div>
        </div>

        <hr className="border-gray-700" />

        {/* 단일 스텝 추가 */}
        <div className="flex flex-col gap-1.5">
          <label className="text-xs text-gray-400 font-medium">단일 스텝 추가</label>
          <p className="text-[10px] text-gray-600">선택 후 + 버튼</p>
          <select
            value={newStepAction}
            onChange={(e) => setNewStepAction(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:border-purple-500"
          >
            {ACTION_CHOICES.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <button
            onClick={() => addStepNode(newStepAction)}
            className="flex items-center justify-center gap-1.5 px-3 py-1.5 bg-purple-800 hover:bg-purple-700 rounded-lg text-xs transition-colors"
          >
            <Plus size={13} /> 스텝 노드 추가
          </button>
        </div>
      </div>

      {/* ── 가운데: React Flow 캔버스 ── */}
      <div className="flex-1 min-w-0 rounded-xl overflow-hidden border border-gray-700">
        <NodeCtx.Provider value={{ packages, apks }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onEdgeClick={onEdgeClick}
          nodeTypes={nodeTypes}
          fitView
          deleteKeyCode="Delete"
          className="bg-gray-950"
          defaultEdgeOptions={{ animated: true, style: { stroke: '#4B5563' } }}
        >
          <Background color="#374151" gap={20} />
          <Controls className="[&>button]:bg-gray-800 [&>button]:border-gray-700 [&>button]:text-gray-300" />
        </ReactFlow>
        </NodeCtx.Provider>
      </div>

      {/* ── 오른쪽: 미러 + 로그 + 결과 ── */}
      <div className="flex flex-col gap-3 w-80 flex-none">
        {mirrorOn && (
          <div className="flex-none">
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs text-gray-400">{running ? '📸 테스트 화면 (1s)' : '📱 미러링 (1.5s)'}</label>
              {running && <span className="text-xs text-cyan-500 animate-pulse">● live</span>}
            </div>
            <div className="relative bg-gray-900 rounded-lg overflow-hidden border border-gray-700" style={{ aspectRatio: '9/16', maxHeight: '360px' }}>
              {screenSrc ? (
                <img src={screenSrc} alt="device screen" className="w-full h-full object-contain" onError={() => setScreenSrc('')} />
              ) : (
                <div className="absolute inset-0 flex items-center justify-center text-gray-600 text-xs">
                  <Loader2 size={16} className="animate-spin mr-1" /> 로딩 중...
                </div>
              )}
            </div>
          </div>
        )}


        <div className="flex flex-col flex-1 min-h-0">
          <label className="block text-xs text-gray-400 mb-1 flex-none">
            실행 로그 {running && <span className="ml-2 text-green-400 animate-pulse">● 실행 중</span>}
          </label>
          <div className="flex-1 overflow-y-auto scrollbar-thin bg-gray-900 border border-gray-700 rounded-lg p-3 font-mono text-xs space-y-0.5">
            {logs.length === 0 && !running && <p className="text-gray-600 py-4 text-center">실행 후 로그가 표시됩니다.</p>}
            {logs.map((l, i) => <div key={i} className="text-gray-300 leading-5">{l}</div>)}
            <div ref={logsEndRef} />
          </div>
        </div>

        {result && (
          <div className={`flex-none rounded-lg border p-3 ${result.status === 'PASS' ? 'bg-emerald-950 border-emerald-800' : 'bg-red-950 border-red-800'}`}>
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-base">{result.status === 'PASS' ? '✅' : '❌'}</span>
              <span className="font-semibold text-xs">{result.status}</span>
              <span className="ml-auto text-xs text-gray-400">{result.steps_passed}/{result.steps_executed} 통과</span>
            </div>
            {result.error_message && <p className="text-xs text-red-400 mb-1.5">⚠️ {result.error_message}</p>}
          </div>
        )}
      </div>
    </div>
  )
}
