import { useEffect, useState } from 'react'
import { Package, Trash2, RefreshCw, Plus } from 'lucide-react'
import { apkApi, packageApi } from '../../api/client'
import type { AgentInfo } from '../../types'

interface Props { agent: AgentInfo | null }

export default function ApkTab({ agent }: Props) {
  const [apks, setApks] = useState<string[]>([])
  const [apkMap, setApkMap] = useState<Record<string, string>>({})
  const [newPackage, setNewPackage] = useState('')
  const [newApk, setNewApk] = useState('')
  const [mapMsg, setMapMsg] = useState('')

  const loadData = async () => {
    if (!agent) return
    const [apkRes, mapRes] = await Promise.all([
      apkApi.list(agent.name),
      packageApi.getApkMap(agent.name),
    ])
    setApks(apkRes.apks)
    setApkMap(mapRes.map ?? {})
  }

  useEffect(() => { loadData() }, [agent?.name])

  const handleAddMap = async () => {
    if (!agent) return setMapMsg('⚠️ 에이전트를 먼저 선택해주세요.')
    if (!newPackage.trim()) return setMapMsg('⚠️ 패키지명을 입력해주세요.')
    if (!newApk) return setMapMsg('⚠️ APK를 선택해주세요.')
    const res = await packageApi.addApkMap(agent.name, newPackage.trim(), newApk)
    if (res.success) {
      setApkMap(res.map)
      setNewPackage('')
      setNewApk('')
      setMapMsg('✅ 등록 완료')
    } else {
      setMapMsg(`❌ 실패`)
    }
  }

  const handleDeleteMap = async (pkg: string) => {
    if (!agent) return
    const res = await packageApi.deleteApkMap(agent.name, pkg)
    if (res.success) setApkMap(res.map)
  }

  return (
    <section className="rounded-2xl border border-gray-800 bg-gray-900/70 p-5">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Package size={18} className="text-green-400" />
            <h2 className="text-base font-semibold">게임 패키지 및 APK 매핑</h2>
          </div>
          <p className="text-xs text-gray-500">
            파이프라인에서 사용할 게임 패키지와 APK 연결 정보만 관리합니다.
          </p>
        </div>
        <button
          onClick={loadData}
          className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-white transition-colors"
          title="새로고침"
        >
          <RefreshCw size={16} />
        </button>
      </div>

      <div className="mb-4 rounded-lg border border-gray-700 overflow-hidden">
        {Object.keys(apkMap).length === 0 ? (
          <p className="text-xs text-gray-600 text-center py-4">등록된 매핑이 없습니다.</p>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-gray-800 text-gray-400">
                <th className="text-left px-3 py-2">패키지명</th>
                <th className="text-left px-3 py-2">APK</th>
                <th className="w-10" />
              </tr>
            </thead>
            <tbody>
              {Object.entries(apkMap).map(([pkg, apk]) => (
                <tr key={pkg} className="border-t border-gray-800 hover:bg-gray-800/50">
                  <td className="px-3 py-2 font-mono text-gray-300">{pkg}</td>
                  <td className="px-3 py-2 text-gray-400">{apk}</td>
                  <td className="px-2 py-2 text-center">
                    <button
                      onClick={() => handleDeleteMap(pkg)}
                      className="p-1 rounded hover:bg-red-900 text-red-500 transition-colors"
                    >
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <label className="block text-xs text-gray-400 mb-1">패키지명</label>
          <input
            value={newPackage}
            onChange={(e) => setNewPackage(e.target.value)}
            placeholder="com.example.app"
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-green-500"
          />
        </div>
        <div className="flex-1">
          <label className="block text-xs text-gray-400 mb-1">APK 파일</label>
          <select
            value={newApk}
            onChange={(e) => setNewApk(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-green-500"
          >
            <option value="">APK 선택</option>
            {apks.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>
        <button
          onClick={handleAddMap}
          className="flex items-center gap-1 px-3 py-2 bg-green-700 hover:bg-green-600 rounded-lg text-sm font-medium transition-colors"
        >
          <Plus size={15} />
          추가
        </button>
      </div>
      {mapMsg && (
        <div className="mt-2 p-2 rounded-lg bg-gray-800 text-xs font-mono">{mapMsg}</div>
      )}
    </section>
  )
}
