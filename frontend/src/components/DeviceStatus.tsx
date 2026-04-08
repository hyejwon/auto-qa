import { useEffect, useState } from 'react'
import { Smartphone, SmartphoneNfc, AlertCircle, RefreshCw } from 'lucide-react'
import { deviceApi } from '../api/client'
import type { AgentInfo, DeviceInfo } from '../types'

interface Props {
  agent: AgentInfo | null
  children?: React.ReactNode
}

export default function DeviceStatus({ agent, children }: Props) {
  const [device, setDevice] = useState<DeviceInfo | null>(null)

  const check = async () => {
    if (!agent) { setDevice(null); return }
    try {
      const data = await deviceApi.getStatus(agent.name)
      setDevice(data)
    } catch {
      setDevice({ status: 'error', device_id: null, model: null })
    }
  }

  useEffect(() => {
    check()
    const id = setInterval(check, 10000)
    return () => clearInterval(id)
  }, [agent?.name])

  const badge = () => {
    if (!agent) return <span className="text-gray-500 text-sm">에이전트 미선택</span>
    if (!device) return <span className="text-gray-400 text-sm">확인 중...</span>
    if (device.status === 'connected')
      return (
        <span className="flex items-center gap-1.5 text-emerald-400 text-sm">
          <Smartphone size={16} />
          {device.model} ({device.device_id})
        </span>
      )
    if (device.status === 'disconnected')
      return (
        <span className="flex items-center gap-1.5 text-red-400 text-sm">
          <SmartphoneNfc size={16} />
          디바이스 없음
        </span>
      )
    return (
      <span className="flex items-center gap-1.5 text-yellow-400 text-sm">
        <AlertCircle size={16} />
        ADB 연결 실패
      </span>
    )
  }

  return (
    <header className="flex items-center justify-between px-6 py-3 bg-gray-900 border-b border-gray-800">
      <div>
        <h1 className="text-lg font-bold text-white">📱 QA 자동화 테스트</h1>
        <p className="text-xs text-gray-500">AI 기반 모바일 QA 자동화 도구</p>
      </div>
      <div className="flex items-center gap-3">
        {badge()}
        <button
          onClick={check}
          className="p-1.5 rounded hover:bg-gray-800 text-gray-400 hover:text-white transition-colors"
          title="새로고침"
        >
          <RefreshCw size={14} />
        </button>
        {children}
      </div>
    </header>
  )
}
