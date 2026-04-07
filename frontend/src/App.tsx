import { useState } from 'react'
import DeviceStatus from './components/DeviceStatus'
import PreflightPanel from './components/PreflightPanel'
import ModuleEditorTab from './components/tabs/ModuleEditorTab'
import PipelineTab from './components/tabs/PipelineTab'
import RecordingsTab from './components/tabs/RecordingsTab'

type Tab = 'modules' | 'pipeline' | 'recordings'

const TABS: { id: Tab; label: string }[] = [
  { id: 'modules', label: '📦 공통 모듈' },
  { id: 'pipeline', label: '🔗 파이프라인' },
  { id: 'recordings', label: '🎬 녹화 영상' },
]

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>('pipeline')

  return (
    <div className="min-h-screen flex flex-col bg-gray-950">
      <DeviceStatus />
      <PreflightPanel />

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

      {/* 탭 콘텐츠 — 언마운트 방지: hidden으로 숨김 */}
      <main className="flex-1 p-6 overflow-auto">
        <div className={activeTab === 'modules' ? '' : 'hidden'}><ModuleEditorTab /></div>
        <div className={activeTab === 'pipeline' ? '' : 'hidden'}><PipelineTab /></div>
        <div className={activeTab === 'recordings' ? '' : 'hidden'}><RecordingsTab /></div>
      </main>
    </div>
  )
}
