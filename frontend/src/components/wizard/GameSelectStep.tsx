import { useEffect, useMemo, useState } from 'react'
import { CheckCircle, XCircle, RefreshCw, Loader2, Smartphone, Gamepad2, ArrowRight, Download, Trash2 } from 'lucide-react'
import { deviceApi, preflightApi, packageApi, apkApi } from '../../api/client'
import type { DeviceInfo } from '../../types'

interface Props {
  selectedPackage: string
  onSelect: (pkg: string) => void
  onNext: () => void
}

// 패키지명을 사람이 읽기 쉬운 라벨로
function gameLabel(pkg: string): string {
  const last = pkg.split('.').pop() || pkg
  return last.charAt(0).toUpperCase() + last.slice(1)
}

export default function GameSelectStep({ selectedPackage, onSelect, onNext }: Props) {
  const [device, setDevice] = useState<DeviceInfo | null>(null)
  const [installed, setInstalled] = useState<string[]>([])
  const [apkMap, setApkMap] = useState<Record<string, string>>({})
  const [apks, setApks] = useState<string[]>([])
  const [installApk, setInstallApk] = useState('')
  const [loading, setLoading] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const [dev, pkgs, mapRes, apkRes] = await Promise.all([
        deviceApi.getStatus(),
        packageApi.list(),
        packageApi.getApkMap(),
        apkApi.list(),
      ])
      setDevice(dev)
      setInstalled(pkgs.packages)
      setApkMap(mapRes.map)
      setApks(apkRes.apks)
    } catch {
      setDevice({ status: 'error', device_id: null, model: null })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  // 설치된 패키지 + APK 매핑된 패키지 합집합 (미설치 게임도 선택/설치 가능)
  const games = useMemo(
    () => [...new Set([...installed, ...Object.keys(apkMap)])].sort(),
    [installed, apkMap],
  )
  const installedSet = useMemo(() => new Set(installed), [installed])

  // 선택한 게임이 바뀌면 매핑된 APK를 기본 선택
  useEffect(() => {
    const mapped = apkMap[selectedPackage]
    setInstallApk(mapped && apks.includes(mapped) ? mapped : '')
    setMsg('')
  }, [selectedPackage, apkMap, apks])

  const handleReconnect = async () => {
    setReconnecting(true)
    try {
      const res = await preflightApi.reconnect()
      setDevice(res.device)
      if (res.success) await load()
    } finally {
      setReconnecting(false)
    }
  }

  const handleInstall = async () => {
    if (!installApk) { setMsg('⚠️ 설치할 APK를 선택하세요.'); return }
    setBusy(true)
    setMsg(`⏬ ${installApk} 설치 중...`)
    try {
      const res = await apkApi.install(installApk)
      setMsg(res.success ? `✅ 설치 완료 — ${installApk}` : `❌ 설치 실패: ${res.message}`)
      if (res.success) await load()
    } catch (e) {
      setMsg(`❌ 설치 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const handleUninstall = async () => {
    if (!selectedPackage) return
    if (!window.confirm(`${selectedPackage} 을(를) 기기에서 삭제할까요?`)) return
    setBusy(true)
    setMsg(`🗑️ ${selectedPackage} 삭제 중...`)
    try {
      const res = await packageApi.uninstall(selectedPackage)
      setMsg(res.success ? `✅ 삭제 완료 — ${selectedPackage}` : `❌ 삭제 실패: ${res.message}`)
      if (res.success) await load()
    } catch (e) {
      setMsg(`❌ 삭제 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const connected = device?.status === 'connected'
  const canProceed = connected && !!selectedPackage
  const selectedInstalled = installedSet.has(selectedPackage)

  return (
    <div className="max-w-3xl mx-auto w-full flex flex-col gap-6 overflow-y-auto scrollbar-thin">
      {/* ADB 연결 상태 */}
      <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border ${
        connected ? 'bg-emerald-950/40 border-emerald-800' : 'bg-red-950/40 border-red-800'
      }`}>
        {connected ? <CheckCircle size={20} className="text-emerald-400 flex-none" />
                   : <XCircle size={20} className="text-red-400 flex-none" />}
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gray-100 flex items-center gap-1.5">
            <Smartphone size={14} className="text-gray-400" />
            {connected ? 'ADB 연결됨' : 'ADB 미연결'}
          </p>
          <p className="text-xs text-gray-400 truncate">
            {connected
              ? `${device?.model ?? '알 수 없는 기기'} (${device?.device_id})`
              : '기기를 USB로 연결하고 USB 디버깅을 허용하세요.'}
          </p>
        </div>
        <button onClick={handleReconnect} disabled={reconnecting}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-200 disabled:opacity-50 transition-colors">
          {reconnecting ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          재연결
        </button>
      </div>

      {/* 게임 선택 */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium text-gray-300 flex items-center gap-1.5">
            <Gamepad2 size={16} className="text-gray-400" /> 게임 선택
          </h2>
          <button onClick={load} disabled={loading}
            className="flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50">
            {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
            새로고침
          </button>
        </div>

        {games.length === 0 ? (
          <p className="text-center text-gray-600 py-12 text-sm border border-dashed border-gray-800 rounded-xl">
            {loading ? '불러오는 중...' : '게임이 없습니다. 기기 연결 또는 apks 폴더를 확인하세요.'}
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            {games.map((pkg) => {
              const active = pkg === selectedPackage
              const isInstalled = installedSet.has(pkg)
              return (
                <button key={pkg} onClick={() => onSelect(pkg)}
                  className={`text-left px-4 py-3 rounded-xl border transition-colors ${
                    active ? 'bg-blue-950/50 border-blue-600' : 'bg-gray-900 border-gray-800 hover:border-gray-600'
                  }`}>
                  <div className="flex items-center gap-1.5">
                    <p className="text-sm font-medium text-gray-100">{gameLabel(pkg)}</p>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                      isInstalled ? 'bg-emerald-900/50 text-emerald-300' : 'bg-gray-800 text-gray-500'
                    }`}>
                      {isInstalled ? '설치됨' : '미설치'}
                    </span>
                  </div>
                  <p className="text-xs text-gray-500 truncate">{pkg}</p>
                </button>
              )
            })}
          </div>
        )}
      </div>

      {/* 선택한 게임 설치/삭제 */}
      {selectedPackage && (
        <div className="rounded-xl border border-gray-800 bg-gray-900/60 p-3 space-y-2">
          <p className="text-xs text-gray-400">
            <span className="text-gray-200 font-medium">{gameLabel(selectedPackage)}</span> 빌드 관리
          </p>
          <div className="flex items-center gap-2">
            <select value={installApk} disabled={busy || !connected}
              onChange={(e) => setInstallApk(e.target.value)}
              className="flex-1 min-w-0 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-xs focus:outline-none focus:border-blue-500 disabled:opacity-50">
              <option value="">설치할 APK 선택</option>
              {apks.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
            <button onClick={handleInstall} disabled={busy || !connected || !installApk}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-xs font-medium transition-colors">
              {busy ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
              설치
            </button>
            <button onClick={handleUninstall} disabled={busy || !connected || !selectedInstalled}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-red-800 hover:bg-red-700 disabled:opacity-40 text-xs font-medium transition-colors">
              <Trash2 size={13} /> 삭제
            </button>
          </div>
          {msg && <p className="text-xs text-gray-400 truncate">{msg}</p>}
        </div>
      )}

      <div className="flex justify-end">
        <button onClick={onNext} disabled={!canProceed}
          className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed text-sm font-medium transition-colors">
          테스트케이스 선택 <ArrowRight size={16} />
        </button>
      </div>
      {!connected && (
        <p className="text-center text-xs text-gray-500 -mt-2">ADB 연결 후 진행할 수 있습니다.</p>
      )}
    </div>
  )
}
