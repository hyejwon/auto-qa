import { useEffect, useState } from 'react'
import { RefreshCw, Download, Video } from 'lucide-react'
import { recordingApi } from '../../api/client'

export default function RecordingsTab() {
  const [recordings, setRecordings] = useState<string[]>([])
  const [selected, setSelected] = useState('')

  const load = async () => {
    const res = await recordingApi.list()
    setRecordings(res.recordings)
  }

  useEffect(() => { load() }, [])

  return (
    <div className="space-y-4 max-w-3xl">
      <div>
        <p className="text-xs text-gray-500 mb-3">테스트 실행 시 자동 저장된 화면 녹화 파일을 확인합니다.</p>
        <div className="flex gap-2">
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
          >
            <option value="">녹화 파일 선택</option>
            {recordings.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <button
            onClick={load}
            className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-white transition-colors"
            title="새로고침"
          >
            <RefreshCw size={16} />
          </button>
        </div>
      </div>

      {selected ? (
        <div className="space-y-3">
          <video
            key={selected}
            src={`/recordings/${selected}`}
            controls
            className="w-full rounded-lg border border-gray-700 bg-black"
            style={{ maxHeight: 420 }}
          />
          <div className="flex items-center gap-3">
            <a
              href={`/recordings/${selected}`}
              download={selected}
              className="flex items-center gap-2 px-4 py-2 bg-blue-700 hover:bg-blue-600 rounded-lg text-sm font-medium transition-colors"
            >
              <Download size={16} />
              다운로드
            </a>
            <span className="text-xs text-gray-500">{selected}</span>
          </div>
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center py-16 text-gray-600">
          <Video size={40} className="mb-3" />
          <p className="text-sm">녹화 파일을 선택하세요</p>
        </div>
      )}
    </div>
  )
}
