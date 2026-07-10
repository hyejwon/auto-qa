import { useCallback, useEffect, useState } from 'react'
import { Smartphone, Plus, Loader2, Unplug } from 'lucide-react'
import { deviceApi } from '../api/client'
import type { DeviceEntry } from '../types'

interface Props {
  value: string
  onChange: (deviceId: string) => void
}

const POLL_MS = 10_000

/** 헤더용 디바이스 선택기 — 서버에 물린 폰 목록 + IP로 무선 연결 */
export default function DevicePicker({ value, onChange }: Props) {
  const [devices, setDevices] = useState<DeviceEntry[]>([])
  const [adding, setAdding] = useState(false)
  const [address, setAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await deviceApi.list()
      setDevices(res.devices)
      // 선택된 디바이스가 목록에서 사라졌으면 선택 해제
      if (value && !res.devices.some((d) => d.device_id === value)) onChange('')
      // 미선택 상태에서 온라인 디바이스가 딱 하나면 자동 선택
      const online = res.devices.filter((d) => d.status === 'device')
      if (!value && online.length === 1) onChange(online[0].device_id)
    } catch {
      /* 서버 미응답 시 기존 목록 유지 */
    }
  }, [value, onChange])

  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [load])

  const handleConnect = async () => {
    const addr = address.trim()
    if (!addr) return
    setBusy(true)
    setMsg('')
    try {
      const res = await deviceApi.connect(addr)
      setMsg(res.success ? `✅ ${res.message}` : `❌ ${res.message}`)
      if (res.success) {
        setAddress('')
        setAdding(false)
        await load()
        if (res.address) onChange(res.address)
      }
    } catch (e) {
      setMsg(`❌ ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const handleDisconnect = async () => {
    if (!value || !value.includes(':')) return
    if (!window.confirm(`${value} 연결을 해제할까요? (자동 재연결 목록에서도 제거됩니다)`)) return
    setBusy(true)
    try {
      await deviceApi.disconnect(value)
      onChange('')
      await load()
    } finally {
      setBusy(false)
    }
  }

  const label = (d: DeviceEntry) => {
    const state = d.status === 'device' ? (d.busy ? ' · 실행 중' : '') : ' · 오프라인'
    return `${d.model}${d.model !== d.device_id ? ` (${d.device_id})` : ''}${state}`
  }

  return (
    <div className="ml-auto flex items-center gap-2">
      <Smartphone size={14} className="text-gray-400 flex-none" />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-gray-200 max-w-56 focus:outline-none focus:border-blue-500"
      >
        <option value="">디바이스 선택</option>
        {devices.map((d) => (
          <option key={d.device_id} value={d.device_id} disabled={d.status !== 'device'}>
            {label(d)}
          </option>
        ))}
      </select>

      {value.includes(':') && (
        <button
          onClick={handleDisconnect}
          disabled={busy}
          title="무선 연결 해제"
          className="p-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-red-300 disabled:opacity-50 transition-colors"
        >
          <Unplug size={13} />
        </button>
      )}

      {adding ? (
        <div className="flex items-center gap-1.5">
          <input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleConnect()}
            placeholder="폰 IP (예: 192.168.0.23)"
            autoFocus
            className="w-44 bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500"
          />
          <button
            onClick={handleConnect}
            disabled={busy || !address.trim()}
            className="px-2.5 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-xs font-medium transition-colors"
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : '연결'}
          </button>
          <button
            onClick={() => { setAdding(false); setMsg('') }}
            className="px-2 py-1.5 rounded-lg text-xs text-gray-500 hover:text-gray-300"
          >
            취소
          </button>
        </div>
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-300 transition-colors"
        >
          <Plus size={13} /> IP로 연결
        </button>
      )}

      {msg && <span className="text-xs text-gray-400 max-w-48 truncate">{msg}</span>}
    </div>
  )
}
