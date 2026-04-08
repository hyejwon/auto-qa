import { useEffect, useState } from 'react'
import { Wand2, Save, Loader2 } from 'lucide-react'
import { planApi, templateApi, packageApi } from '../../api/client'
import { StableTextarea } from '../StableInput'
import type { AgentInfo } from '../../types'
import { SAMPLE_SCENARIOS } from '../../types'

interface Props { agent: AgentInfo | null }

export default function TemplateCreateTab({ agent }: Props) {
  const [packages, setPackages] = useState<string[]>([])
  const [selectedPkg, setSelectedPkg] = useState('com.percent.aos.cooptd')
  const [scenario, setScenario] = useState('')
  const [yamlOutput, setYamlOutput] = useState('')
  const [status, setStatus] = useState('')
  const [generating, setGenerating] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (agent) packageApi.list(agent.name).then((r) => setPackages(r.packages))
  }, [agent?.name])

  const handleGenerate = async () => {
    if (!scenario.trim()) return setStatus('⚠️ 시나리오를 입력해주세요.')
    setGenerating(true)
    setStatus('🔄 플랜 생성 중...')
    try {
      const res = await planApi.generate(scenario.trim(), selectedPkg)
      setYamlOutput(res.yaml)
      setStatus(`✅ 플랜 생성 완료 — ${res.title} (${res.steps_count}개 스텝)`)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setStatus(`❌ 오류: ${msg}`)
    } finally {
      setGenerating(false)
    }
  }

  const handleSave = async () => {
    if (!yamlOutput.trim()) return setStatus('⚠️ 먼저 플랜을 생성해주세요.')
    setSaving(true)
    try {
      // YAML에서 title 파싱
      const titleMatch = yamlOutput.match(/^title:\s*(.+)$/m)
      const title = titleMatch ? titleMatch[1].trim() : 'untitled'
      const res = await templateApi.save(title, yamlOutput, scenario)
      setStatus(res.success ? `✅ 템플릿 저장 완료 → ${res.filename}` : `❌ 저장 실패: ${res.error}`)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setStatus(`❌ 오류: ${msg}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-4">
      {/* 샘플 버튼 */}
      <div>
        <p className="text-xs text-gray-500 mb-2">샘플 시나리오</p>
        <div className="flex flex-wrap gap-2">
          {SAMPLE_SCENARIOS.map((s) => (
            <button
              key={s.label}
              onClick={() => { setSelectedPkg(s.package); setScenario(s.scenario) }}
              className="px-3 py-1 text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-full transition-colors"
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {/* 패키지 선택 */}
      <div>
        <label className="block text-xs text-gray-400 mb-1">패키지명</label>
        <select
          value={selectedPkg}
          onChange={(e) => setSelectedPkg(e.target.value)}
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
        >
          {packages.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </div>

      {/* 시나리오 입력 */}
      <div>
        <label className="block text-xs text-gray-400 mb-1">테스트 시나리오 (자연어)</label>
        <StableTextarea
          value={scenario}
          onValueChange={setScenario}
          rows={10}
          placeholder="앱을 실행한다.&#10;→ 구글 로그인 버튼을 클릭한다.&#10;→ ..."
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm font-mono resize-y focus:outline-none focus:border-blue-500 placeholder-gray-600"
        />
      </div>

      {/* 상태 */}
      {status && (
        <div className="p-3 bg-gray-800 rounded-lg text-sm">{status}</div>
      )}

      {/* 버튼 */}
      <div className="flex gap-3">
        <button
          onClick={handleGenerate}
          disabled={generating}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
        >
          {generating ? <Loader2 size={16} className="animate-spin" /> : <Wand2 size={16} />}
          {generating ? '생성 중...' : '🔍 플랜 생성'}
        </button>
        <button
          onClick={handleSave}
          disabled={saving || !yamlOutput}
          className="flex items-center gap-2 px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
        >
          {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
          {saving ? '저장 중...' : '💾 템플릿으로 저장'}
        </button>
      </div>

      {/* YAML 출력 */}
      {yamlOutput && (
        <div>
          <label className="block text-xs text-gray-400 mb-1">생성된 테스트 플랜 (YAML)</label>
          <StableTextarea
            value={yamlOutput}
            onValueChange={setYamlOutput}
            rows={22}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs font-mono resize-y focus:outline-none focus:border-blue-500"
          />
        </div>
      )}
    </div>
  )
}
