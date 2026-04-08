import { useEffect, useState } from 'react'
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  SortableContext,
  verticalListSortingStrategy,
  useSortable,
  arrayMove,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Save, Plus, Trash2, GripVertical, RefreshCw, Loader2, FilePlus } from 'lucide-react'
import { templateApi, packageApi, apkApi } from '../../api/client'
import type { AgentInfo } from '../../types'
import { StableInput } from '../StableInput'
import type { Step } from '../../types'
import { ACTION_CHOICES, TARGET_ACTIONS } from '../../types'

// ─── StepCard (TemplateRunTab 과 동일 구조) ──────────────────
function StepCard({
  id, step, index, onChange, onDelete, packages, apks,
}: {
  id: string; step: Step; index: number
  onChange: (s: Step) => void; onDelete: () => void
  packages: string[]; apks: string[]
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.4 : 1 }

  const PKG_ACTIONS = new Set(['launch_app', 'uninstall_app', 'skip_tutorial'])
  const APK_ACTIONS = new Set(['install_app'])
  const hasTarget = TARGET_ACTIONS.has(step.action)

  const handleActionChange = (action: string) => {
    let target = step.target ?? null
    if (PKG_ACTIONS.has(action)) target = packages[0] ?? null
    else if (APK_ACTIONS.has(action)) target = apks[0] ?? null
    else if (!TARGET_ACTIONS.has(action)) target = null
    onChange({ ...step, action, target })
  }

  return (
    <div ref={setNodeRef} style={style}
      className="flex gap-2 p-3 bg-gray-800 border border-gray-700 rounded-lg items-start">
      <button {...attributes} {...listeners}
        className="mt-1 flex-none p-0.5 text-gray-500 hover:text-gray-300 cursor-grab active:cursor-grabbing touch-none">
        <GripVertical size={16} />
      </button>
      <span className="mt-1 flex-none w-6 h-6 rounded-full bg-gray-700 text-xs flex items-center justify-center text-gray-300">
        {index + 1}
      </span>
      <div className="flex-1 space-y-2 min-w-0">
        <div className="flex flex-wrap gap-2">
          <select value={step.action} onChange={(e) => handleActionChange(e.target.value)}
            className="bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500">
            {ACTION_CHOICES.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          {PKG_ACTIONS.has(step.action) ? (
            <select value={step.target || ''} onChange={(e) => onChange({ ...step, target: e.target.value })}
              className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500">
              <option value="">패키지 선택</option>
              {packages.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          ) : APK_ACTIONS.has(step.action) ? (
            <select value={step.target || ''} onChange={(e) => onChange({ ...step, target: e.target.value })}
              className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-green-500">
              <option value="">APK 선택</option>
              {apks.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          ) : (
            <StableInput value={step.target || ''} onValueChange={(value) => onChange({ ...step, target: value })}
              disabled={!hasTarget} placeholder="target (UI 요소명)"
              className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500 disabled:opacity-40" />
          )}
          <StableInput value={step.description || ''} onValueChange={(value) => onChange({ ...step, description: value })}
            placeholder="설명"
            className="flex-1 min-w-[100px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500" />
        </div>
        <div className="flex flex-wrap gap-2">
          {step.action === 'wait' ? (
            <input type="number" min={1} value={(step.params?.seconds as number) ?? 5}
              onChange={(e) => onChange({ ...step, params: { ...step.params, seconds: Number(e.target.value) } })}
              placeholder="초(seconds)"
              className="w-28 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-purple-500" />
          ) : (
            <>
              <StableInput value={step.params?.expect_visible || ''}
                onValueChange={(value) => onChange({ ...step, params: { ...step.params, expect_visible: value || null } })}
                placeholder="expect_visible"
                className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-yellow-500" />
              <StableInput value={step.params?.expect_hidden || ''}
                onValueChange={(value) => onChange({ ...step, params: { ...step.params, expect_hidden: value || null } })}
                placeholder="expect_hidden"
                className="flex-1 min-w-[140px] bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs focus:outline-none focus:border-orange-500" />
            </>
          )}
        </div>
      </div>
      <button onClick={onDelete} className="mt-1 flex-none p-1 rounded hover:bg-red-900 text-red-400 transition-colors">
        <Trash2 size={14} />
      </button>
    </div>
  )
}

// ─── 메인 탭 ─────────────────────────────────────────────────
interface Props { agent: AgentInfo | null }

export default function ModuleEditorTab({ agent }: Props) {
  const [modules, setModules] = useState<string[]>([])
  const [packages, setPackages] = useState<string[]>([])
  const [apks, setApks] = useState<string[]>([])
  const [selected, setSelected] = useState('')
  const [title, setTitle] = useState('')
  const [pkg, setPkg] = useState('')
  const [steps, setSteps] = useState<Step[]>([])
  const [stepIds, setStepIds] = useState<string[]>([])
  const [status, setStatus] = useState('')
  const [saving, setSaving] = useState(false)
  const [newName, setNewName] = useState('')

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))

  const loadLists = async () => {
    const [tRes, pRes, aRes] = await Promise.all([
      templateApi.list(),
      agent ? packageApi.list(agent.name) : Promise.resolve({ packages: [] }),
      agent ? apkApi.list(agent.name) : Promise.resolve({ apks: [] }),
    ])
    setModules(tRes.templates)
    setPackages(pRes.packages)
    setApks(aRes.apks)
  }

  useEffect(() => { loadLists() }, [agent?.name])

  const handleSelect = async (name: string) => {
    if (!name) return
    setSelected(name)
    const res = await templateApi.get(name)
    const t = res.template
    const loaded = t.steps || []
    setTitle(t.title || name)
    setPkg(t.package || '')
    setSteps(loaded)
    setStepIds(loaded.map((_, i) => `step-${i}-${Date.now()}`))
    setStatus('')
  }

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oi = stepIds.indexOf(active.id as string)
    const ni = stepIds.indexOf(over.id as string)
    setSteps((s) => arrayMove(s, oi, ni))
    setStepIds((ids) => arrayMove(ids, oi, ni))
  }

  const updateStep = (idx: number, s: Step) =>
    setSteps((prev) => prev.map((p, i) => (i === idx ? s : p)))

  const deleteStep = (idx: number) => {
    setSteps((p) => p.filter((_, i) => i !== idx))
    setStepIds((p) => p.filter((_, i) => i !== idx))
  }

  const addStep = () => {
    const s: Step = { action: 'find_and_tap', target: null, description: '', timeout: 10, retry: 2, params: {} }
    setSteps((p) => [...p, s])
    setStepIds((p) => [...p, `step-${p.length}-${Date.now()}`])
  }

  const buildYaml = (name: string) => {
    const stepsYaml = steps.map((s) => {
      const lines = [`  - action: ${s.action}`]
      if (s.target != null && s.target !== '') lines.push(`    target: "${s.target}"`)
      if (s.description) lines.push(`    description: "${s.description}"`)
      if (s.timeout != null) lines.push(`    timeout: ${s.timeout}`)
      if (s.retry != null) lines.push(`    retry: ${s.retry}`)
      const params = Object.entries(s.params ?? {}).filter(([, v]) => v != null && v !== '')
      if (params.length > 0) {
        lines.push(`    params:`)
        params.forEach(([k, v]) => lines.push(`      ${k}: ${JSON.stringify(v)}`))
      }
      return lines.join('\n')
    }).join('\n')
    return `title: ${name}\ndescription: ''\npackage: '${pkg}'\nsteps:\n${stepsYaml}\nexpected_results: []\n`
  }

  const handleSave = async () => {
    if (!title.trim()) return setStatus('⚠️ 모듈 이름을 입력해주세요.')
    if (!steps.length) return setStatus('⚠️ 스텝이 없습니다.')
    setSaving(true)
    const res = await templateApi.save(title.trim(), buildYaml(title.trim()))
    setStatus(res.success ? `💾 저장 완료 → ${res.filename}` : `❌ 저장 실패: ${res.error}`)
    setSaving(false)
    setSelected(title.trim())
    loadLists()
  }

  const handleNew = async () => {
    const name = newName.trim()
    if (!name) return setStatus('⚠️ 새 모듈 이름을 입력해주세요.')
    setTitle(name)
    setPkg('')
    setSteps([])
    setStepIds([])
    setSelected('')
    setNewName('')
    setStatus(`✨ 새 모듈 "${name}" 생성 — 스텝을 추가하고 저장하세요.`)
  }

  const handleDelete = async () => {
    if (!selected) return
    if (!confirm(`"${selected}" 모듈을 삭제할까요?`)) return
    await templateApi.delete(selected)
    setSelected('')
    setTitle('')
    setSteps([])
    setStepIds([])
    setStatus(`🗑️ "${selected}" 삭제됨`)
    loadLists()
  }

  return (
    <div className="flex gap-4 min-h-[720px]">

      {/* ── 왼쪽: 모듈 목록 ── */}
      <div className="flex flex-col gap-2 w-52 flex-none">
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-400 font-medium">공통 모듈</span>
          <button onClick={loadLists} className="p-0.5 text-gray-600 hover:text-gray-400">
            <RefreshCw size={12} />
          </button>
        </div>

        {/* 새 모듈 생성 */}
        <div className="flex gap-1">
          <StableInput
            value={newName}
            onValueChange={setNewName}
            onKeyDown={(e) => e.key === 'Enter' && handleNew()}
            placeholder="새 모듈 이름"
            className="flex-1 min-w-0 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs focus:outline-none focus:border-blue-500"
          />
          <button onClick={handleNew}
            className="flex-none p-1.5 bg-blue-700 hover:bg-blue-600 rounded text-white transition-colors" title="새 모듈 생성">
            <FilePlus size={13} />
          </button>
        </div>

        {/* 모듈 목록 */}
        <div className="flex-1 min-h-0 overflow-y-auto scrollbar-thin space-y-1">
          {modules.length === 0 && (
            <p className="text-xs text-gray-600 text-center py-6">모듈 없음</p>
          )}
          {modules.map((m) => (
            <button
              key={m}
              onClick={() => handleSelect(m)}
              className={`w-full text-left px-3 py-2 rounded-lg text-xs transition-colors border ${
                selected === m
                  ? 'bg-blue-900/50 border-blue-600 text-blue-300'
                  : 'bg-gray-800 border-gray-700 text-gray-300 hover:bg-gray-700 hover:border-gray-600'
              }`}
            >
              {m}
            </button>
          ))}
        </div>
      </div>

      {/* ── 오른쪽: 에디터 ── */}
      <div className="flex flex-col flex-1 gap-3 min-w-0">

        {/* 헤더 */}
        <div className="flex flex-col gap-2 flex-none">
          <div className="flex items-center gap-2">
            <StableInput
              value={title}
              onValueChange={setTitle}
              placeholder="모듈 이름"
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm font-medium focus:outline-none focus:border-blue-500"
            />
            <button onClick={handleSave} disabled={saving || !steps.length || !title.trim()}
              className="flex items-center gap-1.5 px-4 py-2 bg-blue-700 hover:bg-blue-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors">
              {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
              저장
            </button>
            {selected && (
              <button onClick={handleDelete}
                className="flex items-center gap-1.5 px-3 py-2 bg-gray-700 hover:bg-red-900 border border-gray-600 hover:border-red-700 rounded-lg text-sm text-gray-400 hover:text-red-400 transition-colors">
                <Trash2 size={15} />
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-500 flex-none">패키지</label>
            <select
              value={pkg}
              onChange={(e) => setPkg(e.target.value)}
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-2.5 py-1.5 text-xs focus:outline-none focus:border-blue-500"
            >
              <option value="">미지정 (공통 모듈)</option>
              {packages.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            {status && <span className="text-xs text-gray-400 truncate max-w-xs">{status}</span>}
          </div>
        </div>

        {/* 스텝 목록 */}
        {!title && !selected ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-gray-600 text-sm">왼쪽에서 모듈을 선택하거나 새로 만드세요.</p>
          </div>
        ) : (
          <div className="flex flex-col flex-1 min-h-0">
            <div className="flex items-center justify-between mb-2 flex-none">
              <span className="text-xs text-gray-400">스텝 목록 ({steps.length}개)</span>
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
                        onChange={(ns) => updateStep(i, ns)}
                        onDelete={() => deleteStep(i)}
                        packages={packages}
                        apks={apks}
                      />
                    ))}
                    {steps.length === 0 && (
                      <p className="text-center text-gray-600 py-16 text-sm">스텝을 추가하세요.</p>
                    )}
                  </div>
                </SortableContext>
              </DndContext>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
