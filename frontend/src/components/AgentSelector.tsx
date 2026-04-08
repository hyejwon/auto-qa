import { useEffect, useState } from 'react'
import { Monitor, MonitorOff, RefreshCw, ChevronDown } from 'lucide-react'
import { agentsApi } from '../api/client'
import type { AgentInfo } from '../types'

interface Props {
  selected: AgentInfo | null
  onSelect: (agent: AgentInfo | null) => void
}

export default function AgentSelector({ selected, onSelect }: Props) {
  const [agents, setAgents] = useState<AgentInfo[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const fetch = async () => {
    setLoading(true)
    try {
      const res = await agentsApi.list()
      setAgents(res.agents)
      // 선택된 에이전트가 오프라인이 되면 해제
      if (selected) {
        const updated = res.agents.find((a) => a.name === selected.name)
        if (!updated?.online) onSelect(null)
        else onSelect(updated)
      }
      // 에이전트가 1개 뿐이고 온라인이면 자동 선택
      const online = res.agents.filter((a) => a.online)
      if (!selected && online.length === 1) onSelect(online[0])
    } catch {
      // 오케스트레이터 없음 (단독 실행 모드) — 무시
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetch()
    const id = setInterval(fetch, 15000)
    return () => clearInterval(id)
  }, [])

  const onlineAgents = agents.filter((a) => a.online)

  if (agents.length === 0) return null  // 오케스트레이터 없으면 숨김

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm transition-colors ${
          selected
            ? 'bg-blue-900/40 border-blue-700 text-blue-300 hover:bg-blue-900/60'
            : 'bg-gray-800 border-gray-600 text-gray-400 hover:bg-gray-700'
        }`}
      >
        {selected ? (
          <>
            <Monitor size={14} className="text-blue-400" />
            <span className="font-medium">{selected.name}</span>
          </>
        ) : (
          <>
            <MonitorOff size={14} />
            <span>에이전트 선택</span>
          </>
        )}
        <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
        {loading && <RefreshCw size={11} className="animate-spin text-gray-500" />}
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-64 bg-gray-800 border border-gray-600 rounded-xl shadow-xl z-50">
          <div className="px-3 py-2 border-b border-gray-700">
            <p className="text-xs text-gray-400 font-medium">
              온라인 에이전트 ({onlineAgents.length}/{agents.length})
            </p>
          </div>
          <div className="max-h-48 overflow-y-auto py-1">
            {agents.length === 0 ? (
              <p className="px-3 py-2 text-xs text-gray-500">연결된 에이전트 없음</p>
            ) : (
              agents.map((agent) => (
                <button
                  key={agent.name}
                  disabled={!agent.online}
                  onClick={() => { onSelect(agent.online ? agent : null); setOpen(false) }}
                  className={`w-full flex items-center gap-2.5 px-3 py-2 text-left text-sm transition-colors ${
                    agent.online
                      ? selected?.name === agent.name
                        ? 'bg-blue-900/40 text-blue-300'
                        : 'hover:bg-gray-700 text-gray-200'
                      : 'opacity-40 cursor-not-allowed text-gray-500'
                  }`}
                >
                  <span className={`w-1.5 h-1.5 rounded-full flex-none ${agent.online ? 'bg-emerald-400' : 'bg-gray-600'}`} />
                  <div className="min-w-0">
                    <p className="font-medium truncate">{agent.name}</p>
                    <p className="text-xs text-gray-500">{agent.ip}:{agent.port}</p>
                  </div>
                  {!agent.online && <span className="ml-auto text-xs text-gray-600">오프라인</span>}
                </button>
              ))
            )}
          </div>
        </div>
      )}
      {open && <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />}
    </div>
  )
}
