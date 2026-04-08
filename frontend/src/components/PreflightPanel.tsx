import { useEffect, useState } from 'react'
import { CheckCircle, AlertTriangle, XCircle, HelpCircle, RefreshCw, ChevronDown, ChevronUp, PlugZap, Loader2 } from 'lucide-react'
import { preflightApi } from '../api/client'
import type { AgentInfo, PreflightCheck } from '../types'

const STATUS_CONFIG = {
  ok:      { icon: CheckCircle,   color: 'text-emerald-400', bg: 'bg-emerald-900/30', border: 'border-emerald-700' },
  warn:    { icon: AlertTriangle, color: 'text-yellow-400',  bg: 'bg-yellow-900/30',  border: 'border-yellow-700' },
  fail:    { icon: XCircle,       color: 'text-red-400',     bg: 'bg-red-900/30',     border: 'border-red-700' },
  unknown: { icon: HelpCircle,    color: 'text-gray-400',    bg: 'bg-gray-800',       border: 'border-gray-700' },
}

function CheckBadge({ check }: { check: PreflightCheck }) {
  const cfg = STATUS_CONFIG[check.status]
  const Icon = cfg.icon
  return (
    <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs ${cfg.bg} ${cfg.border}`}>
      <Icon size={13} className={cfg.color} />
      <span className="text-gray-200 font-medium">{check.name}</span>
    </div>
  )
}

function CheckRow({ check }: { check: PreflightCheck }) {
  const cfg = STATUS_CONFIG[check.status]
  const Icon = cfg.icon
  return (
    <div className={`flex items-start gap-3 px-3 py-2 rounded-lg border ${cfg.bg} ${cfg.border}`}>
      <Icon size={15} className={`${cfg.color} mt-0.5 flex-none`} />
      <div className="min-w-0">
        <p className="text-xs font-medium text-gray-200">{check.name}</p>
        <p className="text-xs text-gray-400 mt-0.5">{check.detail}</p>
      </div>
    </div>
  )
}

interface Props {
  agent: AgentInfo
}

export default function PreflightPanel({ agent }: Props) {
  const [checks, setChecks] = useState<PreflightCheck[]>([])
  const [loading, setLoading] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [reconnectLog, setReconnectLog] = useState<string[]>([])
  const [expanded, setExpanded] = useState(false)
  const [lastChecked, setLastChecked] = useState('')

  const run = async () => {
    setLoading(true)
    try {
      const res = await preflightApi.check(agent.name)
      setChecks(res.checks)
      setLastChecked(new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }))
      if (res.checks.some((c) => c.status === 'fail' || c.status === 'warn')) setExpanded(true)
    } catch {
      setChecks([{ name: '점검 실패', status: 'fail', detail: '에이전트 응답 없음' }])
    } finally {
      setLoading(false)
    }
  }

  const autoReconnect = async () => {
    setReconnecting(true)
    setReconnectLog([])
    try {
      const res = await preflightApi.reconnect(agent.name)
      setReconnectLog(res.log)
      await run()
    } catch {
      setReconnectLog(['❌ 에이전트 응답 없음'])
    } finally {
      setReconnecting(false)
    }
  }

  useEffect(() => {
    setChecks([])
    const init = async () => {
      setLoading(true)
      try {
        const res = await preflightApi.check(agent.name)
        setChecks(res.checks)
        setLastChecked(new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }))
        const adbFail = res.checks.find((c) => c.name === 'ADB 연결' && c.status === 'fail')
        if (adbFail) {
          setExpanded(true)
          setReconnecting(true)
          setReconnectLog(['🔄 ADB 미연결 감지 — 자동 복구 시도 중...'])
          const r = await preflightApi.reconnect(agent.name)
          setReconnectLog(r.log)
          const res2 = await preflightApi.check(agent.name)
          setChecks(res2.checks)
          setLastChecked(new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }))
          setReconnecting(false)
        } else if (res.checks.some((c) => c.status === 'fail' || c.status === 'warn')) {
          setExpanded(true)
        }
      } catch {
        setChecks([{ name: '점검 실패', status: 'fail', detail: '에이전트 응답 없음' }])
      } finally {
        setLoading(false)
        setReconnecting(false)
      }
    }
    init()
    const id = setInterval(run, 30000)
    return () => clearInterval(id)
  }, [agent.name])

  const adbFailed = checks.some((c) => c.name === 'ADB 연결' && c.status === 'fail')
  const failCount = checks.filter((c) => c.status === 'fail').length
  const warnCount = checks.filter((c) => c.status === 'warn').length
  const allOk = checks.length > 0 && failCount === 0 && warnCount === 0

  const summaryColor = failCount > 0 ? 'text-red-400' : warnCount > 0 ? 'text-yellow-400' : 'text-emerald-400'
  const summaryText = checks.length === 0
    ? '점검 중...'
    : allOk ? '모든 항목 정상'
    : `${failCount > 0 ? `오류 ${failCount}건` : ''}${failCount > 0 && warnCount > 0 ? ' · ' : ''}${warnCount > 0 ? `경고 ${warnCount}건` : ''}`

  return (
    <div className="bg-gray-900 border-b border-gray-800 px-6 py-2">
      {/* 요약 바 */}
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-200 transition-colors"
        >
          {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          <span className="font-medium text-gray-300">사전 점검</span>
          <span className={`font-medium ${summaryColor}`}>{summaryText}</span>
        </button>

        {!expanded && checks.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap">
            {checks.map((c) => <CheckBadge key={c.name} check={c} />)}
          </div>
        )}

        <div className="ml-auto flex items-center gap-2">
          {/* ADB 실패 시 수동 복구 버튼 */}
          {adbFailed && !reconnecting && (
            <button
              onClick={autoReconnect}
              className="flex items-center gap-1.5 px-2.5 py-1 bg-orange-900/50 hover:bg-orange-800/60 border border-orange-700 rounded-md text-xs text-orange-300 transition-colors"
              title="adb start-server 후 재연결"
            >
              <PlugZap size={12} /> ADB 복구
            </button>
          )}
          {reconnecting && (
            <span className="flex items-center gap-1 text-xs text-yellow-400 animate-pulse">
              <Loader2 size={12} className="animate-spin" /> 복구 중...
            </span>
          )}
          {lastChecked && <span className="text-xs text-gray-600">{lastChecked} 기준</span>}
          <button onClick={run} disabled={loading}
            className="p-1 rounded hover:bg-gray-800 text-gray-500 hover:text-gray-300 transition-colors disabled:opacity-50"
            title="다시 점검">
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* 상세 패널 */}
      {expanded && (
        <div className="mt-2 space-y-2">
          {checks.length > 0 && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {checks.map((c) => <CheckRow key={c.name} check={c} />)}
            </div>
          )}

          {/* ADB 복구 로그 */}
          {reconnectLog.length > 0 && (
            <div className="bg-gray-950 border border-gray-700 rounded-lg px-3 py-2">
              <p className="text-xs text-gray-500 mb-1 font-medium">ADB 복구 로그</p>
              {reconnectLog.map((line, i) => (
                <p key={i} className="text-xs font-mono text-gray-300 leading-5">{line}</p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
