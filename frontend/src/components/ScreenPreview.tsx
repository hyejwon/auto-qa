import { useCallback, useEffect, useRef, useState } from 'react'
import { Monitor, Pause, Play, RefreshCw } from 'lucide-react'

interface Props {
  device: string
  running: boolean
}

/**
 * 기기 화면 미리보기 패널.
 * - 유휴 상태: /api/screen/snapshot (온디맨드 adb 캡처) 2초 폴링
 * - 실행 중: /api/screen/latest (러너가 찍은 최신 스크린샷, 추가 adb 호출 없음) 1.5초 폴링
 * 새 프레임을 미리 로드한 뒤 교체해 깜빡임을 줄인다.
 */
export default function ScreenPreview({ device, running }: Props) {
  const [url, setUrl] = useState('')
  const [paused, setPaused] = useState(false)
  const [error, setError] = useState('')
  const inFlightRef = useRef(false)

  const endpoint = running ? '/api/screen/latest' : '/api/screen/snapshot'
  const intervalMs = running ? 1500 : 2000

  const refresh = useCallback(() => {
    if (inFlightRef.current || !device) return
    inFlightRef.current = true
    const src = `${endpoint}?device=${encodeURIComponent(device)}&t=${Date.now()}`
    const img = new Image()
    img.onload = () => {
      setUrl(src)
      setError('')
      inFlightRef.current = false
    }
    img.onerror = () => {
      setError('화면을 불러올 수 없습니다')
      inFlightRef.current = false
    }
    img.src = src
  }, [device, endpoint])

  useEffect(() => {
    if (paused || !device) return
    refresh()
    const timer = window.setInterval(refresh, intervalMs)
    return () => window.clearInterval(timer)
  }, [device, paused, intervalMs, refresh])

  return (
    <div className="flex flex-col w-52 flex-none min-h-0">
      <label className="text-xs text-gray-400 mb-1 flex items-center gap-1.5 flex-none">
        <Monitor size={13} /> 기기 화면
        {!paused && !error && <span className="text-green-400 animate-pulse text-[9px]">●</span>}
        <span className="ml-auto flex items-center">
          <button onClick={refresh} title="새로고침"
            className="p-1 rounded hover:bg-gray-800 text-gray-500 hover:text-white transition-colors">
            <RefreshCw size={12} />
          </button>
          <button onClick={() => setPaused((p) => !p)} title={paused ? '자동 갱신 재개' : '자동 갱신 일시정지'}
            className="p-1 rounded hover:bg-gray-800 text-gray-500 hover:text-white transition-colors">
            {paused ? <Play size={12} /> : <Pause size={12} />}
          </button>
        </span>
      </label>
      <div className="flex-1 min-h-0 bg-gray-900 border border-gray-700 rounded-lg overflow-hidden flex items-center justify-center">
        {url ? (
          <img src={url} alt="기기 화면" className="max-w-full max-h-full object-contain" />
        ) : (
          <p className="text-gray-600 text-xs px-3 text-center">
            {error || (device ? '기기 화면 로딩 중...' : '디바이스 미선택')}
          </p>
        )}
      </div>
      {error && url && <p className="text-[10px] text-amber-400 mt-1 flex-none truncate">{error} (마지막 화면)</p>}
    </div>
  )
}
