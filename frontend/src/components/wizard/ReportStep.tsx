import { useEffect, useMemo, useState } from 'react'
import { CheckCircle, XCircle, MinusCircle, RotateCcw, Home, ImageIcon, AlertTriangle, Bot, Wrench, Download, Loader2 } from 'lucide-react'
import { debugApi, reportApi } from '../../api/client'
import type { AdaptiveRun, TapDebug, TestResult } from '../../types'

interface Props {
  result: TestResult
  since: string
  adaptive?: AdaptiveRun | null
  onRerun: () => void
  onRestart: () => void
}

function scorePct(v?: number) {
  return v == null ? '—' : `${Math.round(v * 100)}%`
}

function evidenceTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const hms = date.toLocaleTimeString('ko-KR', { hour12: false })
  return `${hms}.${String(date.getMilliseconds()).padStart(3, '0')}`
}

function evidencePhaseLabel(phase?: TapDebug['evidence_phase']) {
  if (phase === 'post_verification') return '후조건 판정'
  if (phase === 'popup_detection') return '팝업 감지'
  if (phase === 'final_verification') return '최종 화면 판정'
  return '클릭 전'
}

export default function ReportStep({ result, since, adaptive, onRerun, onRestart }: Props) {
  const [taps, setTaps] = useState<TapDebug[]>([])
  const [zoom, setZoom] = useState<string | null>(null)
  const [exportingCsv, setExportingCsv] = useState(false)
  const [exportError, setExportError] = useState('')
  const pass = result.status === 'PASS'
  const evalOut = result.eval_output
  const stepGroups = useMemo(() => {
    const steps = result.step_results || []
    const templates = result.pipeline?.templates || []
    if (!templates.length) return [{ name: '테스트 실행', steps, startStep: 0 }]
    return templates
      .map((template) => ({
        name: template.name,
        steps: steps.filter((s) => s.step >= template.start_step && s.step <= template.end_step),
        startStep: template.start_step,
      }))
      .filter((group) => group.steps.length > 0)
  }, [result])

  useEffect(() => {
    debugApi.taps(since).then((r) => setTaps(r.taps)).catch(() => setTaps([]))
  }, [since])

  const handleExportCsv = async () => {
    setExportingCsv(true)
    setExportError('')
    try {
      const { blob, filename } = await reportApi.csv({ result, taps })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e))
    } finally {
      setExportingCsv(false)
    }
  }

  return (
    <div className="max-w-4xl mx-auto w-full flex flex-col gap-4 h-full min-h-0">
      {/* 요약 */}
      <div className={`flex items-center gap-3 px-5 py-4 rounded-xl border flex-none ${
        pass ? 'bg-emerald-950/40 border-emerald-800' : 'bg-red-950/40 border-red-800'
      }`}>
        {pass ? <CheckCircle size={28} className="text-emerald-400 flex-none" />
              : <XCircle size={28} className="text-red-400 flex-none" />}
        <div className="min-w-0 flex-1">
          <p className="text-lg font-semibold text-gray-100">{result.status} — {result.title}</p>
          <p className="text-sm text-gray-400">{result.steps_passed}/{result.steps_executed} 스텝 통과</p>
        </div>
        {evalOut?.final_score != null && (
          <div className="text-right flex-none">
            <p className="text-xs text-gray-400">종합 점수</p>
            <p className="text-xl font-bold text-gray-100">{scorePct(evalOut.final_score)}</p>
          </div>
        )}
      </div>

      {result.error_message && (
        <div className="flex items-start gap-2 px-4 py-2.5 rounded-lg bg-red-950/30 border border-red-900 text-sm text-red-300 flex-none">
          <AlertTriangle size={15} className="mt-0.5 flex-none" /> {result.error_message}
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-thin flex flex-col gap-4 pr-1">
        {/* Gemini 평가 */}
        {evalOut && (
          <div className="grid grid-cols-2 gap-3">
            <div className="px-4 py-3 rounded-xl bg-gray-900 border border-gray-800">
              <p className="text-xs text-gray-400 mb-1">플로우 평가 {evalOut.flow?.severity && (
                <span className="ml-1 text-red-400">({evalOut.flow.severity})</span>
              )}</p>
              <p className="text-lg font-semibold text-gray-100">{scorePct(evalOut.flow?.score)}</p>
              {evalOut.flow?.reason && <p className="text-xs text-gray-500 mt-1">{evalOut.flow.reason}</p>}
            </div>
            <div className="px-4 py-3 rounded-xl bg-gray-900 border border-gray-800">
              <p className="text-xs text-gray-400 mb-1">비전 평가</p>
              <p className="text-lg font-semibold text-gray-100">{scorePct(evalOut.vision?.score)}</p>
              {!!evalOut.vision?.low_confidence_steps?.length && (
                <p className="text-xs text-gray-500 mt-1">낮은 신뢰 스텝: {evalOut.vision.low_confidence_steps.join(', ')}</p>
              )}
            </div>
          </div>
        )}

        {/* 어댑티브 반복 타임라인 */}
        {adaptive && (
          <div>
            <h3 className="text-xs text-gray-400 mb-2 flex items-center gap-1.5">
              <Bot size={13} className="text-indigo-400" /> 어댑티브 반복 ({adaptive.iterations.length}회)
            </h3>
            <div className="space-y-2">
              {adaptive.iterations.map((it) => (
                <div key={it.iteration} className={`px-3 py-2 rounded-lg border text-xs ${
                  it.status === 'PASS' ? 'bg-emerald-950/20 border-emerald-900/50' : 'bg-red-950/20 border-red-900/50'
                }`}>
                  <div className="flex items-center gap-2">
                    {it.status === 'PASS' ? <CheckCircle size={14} className="text-emerald-400 flex-none" />
                                          : <XCircle size={14} className="text-red-400 flex-none" />}
                    <span className="text-gray-200 font-medium">반복 {it.iteration}</span>
                    <span className={it.status === 'PASS' ? 'text-emerald-400' : 'text-red-400'}>{it.status}</span>
                    {!!it.patches?.length && (
                      <span className="ml-auto text-indigo-300 flex items-center gap-1">
                        <Wrench size={11} /> 수정 {it.patches.length}건
                      </span>
                    )}
                  </div>
                  {it.analysis?.failure_reason && (
                    <p className="text-red-300 mt-1">원인: {it.analysis.failure_reason}</p>
                  )}
                  {it.analysis?.patch_rationale && (
                    <p className="text-indigo-300 mt-0.5">보정: {it.analysis.patch_rationale}</p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 스텝 결과 */}
        {!!result.step_results?.length && (
          <div>
            <h3 className="text-xs text-gray-400 mb-2">스텝 결과</h3>
            <div className="space-y-3">
              {stepGroups.map((group) => {
                const passed = group.steps.filter((s) => s.passed || s.skipped).length
                return (
                  <div key={`${group.name}_${group.startStep ?? 0}`} className="rounded-lg border border-gray-800 bg-gray-900/40 overflow-hidden">
                    <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-800 bg-gray-900">
                      <span className="px-2 py-1 rounded bg-blue-950/50 border border-blue-800/60 text-[11px] font-medium text-blue-200">
                        [{group.name}]
                      </span>
                      <span className="text-[11px] text-gray-500">{passed}/{group.steps.length} 스텝 통과</span>
                    </div>
                    <div className="space-y-1.5 p-2">
                      {group.steps.map((s) => (
                        <div key={s.step} className={`flex items-start gap-2 px-3 py-2 rounded-lg border text-xs ${
                          s.skipped ? 'bg-amber-950/20 border-amber-900/50'
                            : s.passed ? 'bg-emerald-950/20 border-emerald-900/50' : 'bg-red-950/20 border-red-900/50'
                        }`}>
                          {s.skipped ? <MinusCircle size={14} className="text-amber-400 mt-0.5 flex-none" />
                            : s.passed ? <CheckCircle size={14} className="text-emerald-400 mt-0.5 flex-none" />
                            : <XCircle size={14} className="text-red-400 mt-0.5 flex-none" />}
                          <div className="min-w-0">
                            <p className="text-gray-200">step {s.step}. {s.label}</p>
                            {s.skipped && <p className="text-amber-400">건너뜀 — 조건부 스텝, 대상 미노출 (정상)</p>}
                            {!s.passed && !s.skipped && s.failure_reason && <p className="text-red-400">{s.failure_reason}</p>}
                            {s.passed && !s.skipped && s.pass_reason && (
                              <p className="text-emerald-400">{s.pass_reason}</p>
                            )}
                            {s.vision_confidence != null && !s.skipped && !(s.passed && s.pass_reason) && (
                              <p className="text-gray-500">신뢰도 {scorePct(s.vision_confidence)}</p>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
            {!!result.pipeline?.templates?.length && (
              <div className="mt-2 flex flex-wrap items-center gap-1">
                {result.pipeline.templates.map((template) => (
                  <div key={`${template.name}_${template.start_step}`} className="flex flex-wrap items-center gap-1">
                    <span className="px-2 py-1 rounded bg-blue-950/50 border border-blue-800/60 text-[11px] font-medium text-blue-200">
                      [{template.name}]
                    </span>
                    {Array.from({ length: template.step_count }, (_, i) => template.start_step + i).map((step) => (
                      <span key={`${template.name}_${step}`} className="px-1.5 py-1 rounded bg-gray-800 border border-gray-700 text-[11px] text-gray-300">
                        step {step}
                      </span>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 탭 및 후조건 판정 스크린샷 */}
        <div>
          <h3 className="text-xs text-gray-400 mb-2 flex items-center gap-1.5">
            <ImageIcon size={13} /> 판정 스크린샷 ({taps.length})
          </h3>
          {taps.length === 0 ? (
            <p className="text-xs text-gray-600 py-6 text-center border border-dashed border-gray-800 rounded-lg">
              이번 실행의 디버그 스크린샷이 없습니다.
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-2">
              {taps.map((t) => {
                const inferredStep = t.step_number ?? (
                  t.action === 'dismiss_popups'
                    ? result.step_results.find((step) => step.action === 'dismiss_popups')?.step
                    : undefined
                )
                return (
                <button key={t.timestamp + t.image} onClick={() => setZoom(t.image)}
                  className={`text-left rounded-lg overflow-hidden border ${
                    t.verified ? 'border-emerald-800' : 'border-red-800'
                  }`}>
                  <img src={t.image} alt={t.target || ''} loading="lazy" className="w-full h-36 object-cover bg-gray-950" />
                  <div className="px-2 py-1 bg-gray-900">
                    <p className="text-[11px] text-gray-300 truncate">{t.target}</p>
                    <p className="text-[10px] text-gray-500 truncate">
                      {inferredStep ? `step ${inferredStep} · ` : ''}{evidencePhaseLabel(t.evidence_phase)}
                      {t.evidence_captured_at ? ` · ${evidenceTime(t.evidence_captured_at)}` : ''}
                    </p>
                    <p className={`text-[10px] truncate ${t.verified ? 'text-emerald-400' : 'text-red-400'}`}
                      title={t.verified ? (t.pass_reason || '') : (t.failure_reason || '')}>
                      {t.verified
                        ? (t.pass_reason ? `PASS · ${t.pass_reason}` : `PASS · conf ${t.confidence != null ? t.confidence.toFixed(2) : '—'}`)
                        : `FAIL${t.failure_reason ? ` · ${t.failure_reason}` : ''}`}
                    </p>
                  </div>
                </button>
                )
              })}
            </div>
          )}
        </div>
      </div>

      {/* 액션 */}
      {exportError && (
        <div className="flex items-start gap-2 px-4 py-2.5 rounded-lg bg-red-950/30 border border-red-900 text-sm text-red-300 flex-none">
          <AlertTriangle size={15} className="mt-0.5 flex-none" /> CSV 저장 실패: {exportError}
        </div>
      )}
      <div className="flex justify-end gap-2 flex-none">
        <button onClick={handleExportCsv} disabled={exportingCsv}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-sm font-medium transition-colors">
          {exportingCsv ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
          CSV 저장
        </button>
        <button onClick={onRerun}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-sm transition-colors">
          <RotateCcw size={15} /> 다시 실행
        </button>
        <button onClick={onRestart}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-sm font-medium transition-colors">
          <Home size={15} /> 처음으로
        </button>
      </div>

      {/* 확대 보기 */}
      {zoom && (
        <div onClick={() => setZoom(null)}
          className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-8 cursor-zoom-out">
          <img src={zoom} alt="" className="max-h-full max-w-full object-contain rounded-lg" />
        </div>
      )}
    </div>
  )
}
