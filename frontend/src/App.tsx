import { useState } from 'react'
import AgentSelector from './components/AgentSelector'
import DeviceStatus from './components/DeviceStatus'
import PreflightPanel from './components/PreflightPanel'
import ManagementTab from './components/tabs/ManagementTab'
import PipelineTab from './components/tabs/PipelineTab'
import RecordingsTab from './components/tabs/RecordingsTab'
import type { AgentInfo } from './types'

type Tab = 'management' | 'pipeline' | 'recordings'

const TABS: { id: Tab; label: string }[] = [
  { id: 'management', label: '🛠️ 관리탭' },
  { id: 'pipeline', label: '🔗 파이프라인' },
  { id: 'recordings', label: '🎬 녹화 영상' },
]

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>('pipeline')
  const [selectedAgent, setSelectedAgent] = useState<AgentInfo | null>(null)

  return (
    <div className="min-h-screen flex flex-col bg-gray-950">
      <DeviceStatus agent={selectedAgent}>
        <AgentSelector selected={selectedAgent} onSelect={setSelectedAgent} />
      </DeviceStatus>

      {selectedAgent ? (
        <PreflightPanel agent={selectedAgent} />
      ) : (
        <div className="bg-gray-900 border-b border-gray-800 px-6 py-2 text-xs text-gray-500">
          에이전트를 선택하면 사전 점검이 시작됩니다.
        </div>
      )}

      {/* 탭 네비게이션 */}
      <nav className="flex border-b border-gray-800 bg-gray-900 px-4">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setActiveTab(t.id)}
            className={`px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
              activeTab === t.id
                ? 'border-blue-500 text-blue-400'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {/* 탭 콘텐츠 */}
      <main className="flex-1 p-6 overflow-auto">
        <div className={activeTab === 'management' ? '' : 'hidden'}>
          <ManagementTab agent={selectedAgent} />
        </div>
        <div className={activeTab === 'pipeline' ? '' : 'hidden'}>
          <PipelineTab agent={selectedAgent} />
        </div>
        <div className={activeTab === 'recordings' ? '' : 'hidden'}>
          <RecordingsTab agent={selectedAgent} />
        </div>
      </main>
    </div>
  )
}
