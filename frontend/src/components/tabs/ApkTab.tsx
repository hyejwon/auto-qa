import { useEffect, useState } from 'react'
import { Package, Trash2, RefreshCw, Download } from 'lucide-react'
import { apkApi, packageApi } from '../../api/client'

export default function ApkTab() {
  const [apks, setApks] = useState<string[]>([])
  const [packages, setPackages] = useState<string[]>([])
  const [selectedApk, setSelectedApk] = useState('')
  const [selectedPkg, setSelectedPkg] = useState('')
  const [installMsg, setInstallMsg] = useState('')
  const [uninstallMsg, setUninstallMsg] = useState('')
  const [installing, setInstalling] = useState(false)
  const [uninstalling, setUninstalling] = useState(false)

  const loadData = async () => {
    const [apkRes, pkgRes] = await Promise.all([apkApi.list(), packageApi.list()])
    setApks(apkRes.apks)
    setPackages(pkgRes.packages)
  }

  useEffect(() => { loadData() }, [])

  const handleInstall = async () => {
    if (!selectedApk) return setInstallMsg('⚠️ APK 파일을 선택해주세요.')
    setInstalling(true)
    setInstallMsg(`📦 설치 중: ${selectedApk} ...`)
    const res = await apkApi.install(selectedApk)
    setInstallMsg(res.success ? `✅ ${res.message}` : `❌ ${res.message}`)
    setInstalling(false)
  }

  const handleUninstall = async () => {
    if (!selectedPkg) return setUninstallMsg('⚠️ 패키지를 선택해주세요.')
    setUninstalling(true)
    setUninstallMsg(`🗑️ 삭제 중: ${selectedPkg} ...`)
    const res = await packageApi.uninstall(selectedPkg)
    setUninstallMsg(res.success ? `✅ ${res.message}` : `❌ ${res.message}`)
    setUninstalling(false)
  }

  return (
    <div className="space-y-8 max-w-2xl">
      {/* APK 설치 */}
      <section>
        <div className="flex items-center gap-2 mb-1">
          <Download size={18} className="text-blue-400" />
          <h2 className="text-base font-semibold">APK 설치</h2>
        </div>
        <p className="text-xs text-gray-500 mb-4">apks/ 폴더에 있는 APK 파일을 선택하여 디바이스에 설치합니다.</p>
        <div className="flex gap-2 mb-3">
          <select
            value={selectedApk}
            onChange={(e) => setSelectedApk(e.target.value)}
            className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
          >
            <option value="">APK 파일 선택</option>
            {apks.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <button
            onClick={loadData}
            className="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-white transition-colors"
            title="새로고침"
          >
            <RefreshCw size={16} />
          </button>
        </div>
        {installMsg && (
          <div className="mb-3 p-3 rounded-lg bg-gray-800 text-sm font-mono">{installMsg}</div>
        )}
        <button
          onClick={handleInstall}
          disabled={installing}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
        >
          <Package size={16} />
          {installing ? '설치 중...' : '📲 설치'}
        </button>
      </section>

      <hr className="border-gray-800" />

      {/* 앱 삭제 */}
      <section>
        <div className="flex items-center gap-2 mb-1">
          <Trash2 size={18} className="text-red-400" />
          <h2 className="text-base font-semibold">앱 삭제</h2>
        </div>
        <p className="text-xs text-gray-500 mb-4">패키지를 선택하여 디바이스에서 앱을 삭제합니다.</p>
        <select
          value={selectedPkg}
          onChange={(e) => setSelectedPkg(e.target.value)}
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm mb-3 focus:outline-none focus:border-red-500"
        >
          <option value="">패키지 선택</option>
          {packages.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        {uninstallMsg && (
          <div className="mb-3 p-3 rounded-lg bg-gray-800 text-sm font-mono">{uninstallMsg}</div>
        )}
        <button
          onClick={handleUninstall}
          disabled={uninstalling}
          className="flex items-center gap-2 px-4 py-2 bg-red-700 hover:bg-red-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
        >
          <Trash2 size={16} />
          {uninstalling ? '삭제 중...' : '🗑️ 삭제'}
        </button>
      </section>
    </div>
  )
}
