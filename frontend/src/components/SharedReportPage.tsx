import { useEffect, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { reportApi } from '../api/client'
import type { SharedReport } from '../types'
import ReportStep from './wizard/ReportStep'

interface Props {
  reportId: string
}

export default function SharedReportPage({ reportId }: Props) {
  const [report, setReport] = useState<SharedReport | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setReport(null)
    setError('')
    reportApi.getShared(reportId)
      .then(setReport)
      .catch((e) => {
        const detail = e?.response?.data?.detail
        setError(typeof detail === 'string' ? detail : '공유 리포트를 불러오지 못했습니다.')
      })
  }, [reportId])

  return (
    <div className="h-screen overflow-hidden flex flex-col bg-gray-950">
      <header className="px-6 py-3 border-b border-gray-800 bg-gray-900 flex items-center gap-2 flex-none">
        <span className="text-lg">🎮</span>
        <h1 className="text-sm font-semibold text-gray-100">Auto QA</h1>
        <span className="text-xs text-gray-500">공유 리포트</span>
        {report?.created_at && (
          <span className="ml-auto text-xs text-gray-500">
            공유 {new Date(report.created_at).toLocaleString('ko-KR')}
          </span>
        )}
      </header>

      <main className="flex-1 min-h-0 p-6 overflow-hidden flex flex-col">
        {!report && !error && (
          <div className="flex-1 flex items-center justify-center text-sm text-gray-400">
            <Loader2 size={18} className="animate-spin mr-2" />
            리포트를 불러오는 중입니다.
          </div>
        )}

        {error && (
          <div className="max-w-xl mx-auto mt-16 w-full px-5 py-4 rounded-xl bg-red-950/30 border border-red-900 text-red-300">
            <div className="flex items-center gap-2 font-medium">
              <AlertTriangle size={18} />
              리포트를 열 수 없습니다.
            </div>
            <p className="mt-2 text-sm text-red-300/80">{error}</p>
          </div>
        )}

        {report && (
          <ReportStep
            result={report.result}
            initialTaps={report.taps}
            adaptive={report.adaptive}
            readOnly
          />
        )}
      </main>

    </div>
  )
}
