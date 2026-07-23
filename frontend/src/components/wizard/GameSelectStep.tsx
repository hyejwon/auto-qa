import { useEffect, useMemo, useState } from 'react'
import { isAxiosError } from 'axios'
import { CheckCircle, XCircle, RefreshCw, Loader2, Smartphone, Gamepad2, ArrowRight, ArrowLeft, Download, Trash2 } from 'lucide-react'
import { deviceApi, preflightApi, packageApi, apptesterApi } from '../../api/client'
import type { AppTesterApp, AppTesterBuild } from '../../api/client'
import type { DeviceInfo } from '../../types'

interface Props {
  device: string
  selectedPackage: string
  onSelect: (pkg: string) => void
  onNext: () => void
}

// 패키지명을 사람이 읽기 쉬운 라벨로
function gameLabel(pkg: string): string {
  const last = pkg.split('.').pop() || pkg
  return last.charAt(0).toUpperCase() + last.slice(1)
}

// API 에러를 사람이 읽을 메시지로 (서버 detail 우선)
function apiErr(e: unknown): string {
  if (isAxiosError(e)) {
    const detail = (e.response?.data as { detail?: string } | undefined)?.detail
    if (detail) return detail
    if (e.response?.status === 409) return '디바이스가 다른 작업에 사용 중입니다 — 잠시 후 다시 시도하세요.'
    return e.message
  }
  return e instanceof Error ? e.message : String(e)
}

export default function GameSelectStep({ device: selectedDevice, selectedPackage, onSelect, onNext }: Props) {
  const [device, setDevice] = useState<DeviceInfo | null>(null)
  const [installed, setInstalled] = useState<string[]>([])
  const [reconnecting, setReconnecting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  // App Tester 프로젝트 목록 (폰 화면 조회 — 게임 선택의 소스)
  const [projects, setProjects] = useState<AppTesterApp[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [projectsLoaded, setProjectsLoaded] = useState(false)
  const [selectedProject, setSelectedProject] = useState('')
  // 선택한 프로젝트의 빌드 버전 목록
  const [testerBuilds, setTesterBuilds] = useState<AppTesterBuild[]>([])
  const [selectedTesterBuild, setSelectedTesterBuild] = useState('')
  const [testerLoading, setTesterLoading] = useState(false)
  // 기기에 실제 설치된 버전 (versionName) — 상세 헤더의 설치 배지 옆에 표시
  const [installedVersion, setInstalledVersion] = useState('')

  const load = async () => {
    try {
      const [dev, pkgs] = await Promise.all([
        deviceApi.getStatus(selectedDevice),
        packageApi.list(selectedDevice),
      ])
      setDevice(dev)
      setInstalled(pkgs.packages)
    } catch {
      setDevice({ status: 'error', device_id: null, model: null })
    }
  }

  useEffect(() => { load() }, [selectedDevice])

  const connected = device?.status === 'connected'

  // App Tester 프로젝트 목록 로드 — 서버 캐시가 있으면 즉시, 없으면 폰 조회(수십 초)
  const loadProjects = async (refresh = false) => {
    if (projectsLoading) return
    setProjectsLoading(true)
    setMsg(refresh ? '📱 폰의 App Tester에서 프로젝트 목록을 다시 읽는 중... (수십 초)' : '📱 프로젝트 목록 불러오는 중...')
    try {
      const res = await apptesterApi.apps(selectedDevice, refresh)
      setProjects(res.apps)
      setProjectsLoaded(true)
      setMsg(res.apps.length ? `✅ App Tester 프로젝트 ${res.apps.length}개 확인` : '⚠️ 프로젝트가 없음 — App Tester 로그인/테스터 초대 확인')
    } catch (e) {
      setMsg(`❌ 프로젝트 목록 조회 실패: ${apiErr(e)}`)
    } finally {
      setProjectsLoading(false)
    }
  }

  // 연결되면 프로젝트 목록 1회 자동 로드
  useEffect(() => {
    if (connected && !projectsLoaded && !projectsLoading) loadProjects()
  }, [connected]) // eslint-disable-line react-hooks/exhaustive-deps

  // 디바이스가 바뀌면 프로젝트/선택 초기화
  useEffect(() => {
    setProjects([])
    setProjectsLoaded(false)
    setSelectedProject('')
  }, [selectedDevice])

  const currentProject = useMemo(
    () => projects.find((p) => p.name === selectedProject) ?? null,
    [projects, selectedProject],
  )

  // 다른 스텝에 갔다 돌아왔을 때 선택된 패키지의 프로젝트 상세를 복원
  useEffect(() => {
    if (!selectedPackage || selectedProject || projects.length === 0) return
    const match = projects.find((p) => p.package === selectedPackage)
    if (match) setSelectedProject(match.name)
  }, [projects, selectedPackage, selectedProject])

  // 프로젝트가 바뀌면 빌드 버전 목록 자동 조회 (서버 캐시 사용)
  useEffect(() => {
    setTesterBuilds([])
    setSelectedTesterBuild('')
    if (!selectedProject) return
    handleTesterLoadBuilds(selectedProject)
  }, [selectedProject]) // eslint-disable-line react-hooks/exhaustive-deps

  const installedSet = useMemo(() => new Set(installed), [installed])

  const handleSelectProject = (p: AppTesterApp) => {
    setSelectedProject(p.name)
    onSelect(p.package || '')
    setMsg('')
  }

  const handleBack = () => {
    setSelectedProject('')
    onSelect('')
    setMsg('')
  }

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

  const handleTesterLoadBuilds = async (game: string, refresh = false) => {
    if (testerLoading) return
    setTesterLoading(true)
    setMsg(refresh ? `📱 '${game}' 버전 목록을 폰 화면에서 다시 읽는 중... (수십 초)` : `📱 '${game}' 버전 목록 불러오는 중...`)
    try {
      const res = await apptesterApi.builds(game, selectedDevice, refresh)
      setTesterBuilds(res.builds)
      setMsg(res.builds.length
        ? `✅ '${game}' 버전 ${res.builds.length}개 확인${res.cached ? ' (캐시)' : ''}`
        : '⚠️ 버전 목록이 비어 있음 — 테스터 초대/로그인 확인')
    } catch (e) {
      setMsg(`❌ 버전 조회 실패: ${apiErr(e)}`)
    } finally {
      setTesterLoading(false)
    }
  }

  const handleTesterInstall = async () => {
    if (!currentProject) return
    if (!selectedTesterBuild) { setMsg('⚠️ 설치할 버전을 선택하세요.'); return }
    setBusy(true)
    setMsg(`⏬ App Tester로 '${currentProject.name}' ${selectedTesterBuild} 다운로드+설치 중... (수 분, 폰 화면을 조작합니다)`)
    try {
      const res = await apptesterApi.install(currentProject.name, selectedTesterBuild, selectedDevice, currentProject.package)
      setMsg(res.success ? `✅ ${res.message}` : `❌ ${res.message}`)
      if (res.success) {
        const pkg = res.package || currentProject.package
        if (pkg) onSelect(pkg)
        setProjects((ps) => ps.map((p) =>
          p.name === currentProject.name ? { ...p, package: pkg || p.package, installed: true } : p,
        ))
        await load()
      }
    } catch (e) {
      setMsg(`❌ 설치 실패: ${apiErr(e)}`)
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
      const res = await packageApi.uninstall(selectedPackage, selectedDevice)
      setMsg(res.success ? `✅ 삭제 완료 — ${selectedPackage}` : `❌ 삭제 실패: ${res.message}`)
      if (res.success) {
        setProjects((ps) => ps.map((p) =>
          p.package === selectedPackage ? { ...p, installed: false } : p,
        ))
        await load()
      }
    } catch (e) {
      setMsg(`❌ 삭제 실패: ${apiErr(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const canProceed = connected && !!selectedPackage
  const selectedInstalled = installedSet.has(selectedPackage)
  const phoneBusy = busy || projectsLoading || testerLoading
  const isDetail = !!selectedProject || !!selectedPackage
  const projectInstalled = !!currentProject
    && (currentProject.installed || (!!currentProject.package && installedSet.has(currentProject.package)))
  const headerPackage = currentProject ? currentProject.package : selectedPackage
  const headerInstalled = currentProject ? projectInstalled : selectedInstalled

  // 상세 헤더에 표시할 기기 설치 버전 조회 — 설치된 패키지가 바뀔 때만
  useEffect(() => {
    setInstalledVersion('')
    if (!connected || !headerInstalled || !headerPackage) return
    apptesterApi.installedVersion(headerPackage, selectedDevice)
      .then(setInstalledVersion)
      .catch(() => setInstalledVersion(''))
  }, [connected, headerInstalled, headerPackage, selectedDevice])

  return (
    <div className="max-w-3xl mx-auto w-full flex flex-col gap-5 overflow-y-auto scrollbar-thin">
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
              : '상단에서 디바이스를 선택하거나, 폰의 무선 디버깅을 켜고 IP로 연결하세요.'}
          </p>
        </div>
        <button onClick={handleReconnect} disabled={reconnecting}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-200 disabled:opacity-50 transition-colors">
          {reconnecting ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          재연결
        </button>
      </div>

      {!isDetail ? (
        <>
          {/* ── 1단계: 프로젝트 선택 (App Tester 목록만) ─────── */}
          <div>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-medium text-gray-300 flex items-center gap-1.5">
                <Gamepad2 size={16} className="text-gray-400" /> 프로젝트 선택
              </h2>
              <button onClick={() => loadProjects(true)} disabled={phoneBusy || !connected}
                className="flex items-center gap-1 px-2 py-1 rounded text-xs text-violet-400 hover:text-violet-200 disabled:opacity-50">
                {projectsLoading ? <Loader2 size={13} className="animate-spin" /> : <Smartphone size={13} />}
                App Tester 새로고침
              </button>
            </div>

            {projectsLoading && projects.length === 0 ? (
              <p className="text-center text-gray-500 py-6 text-sm border border-dashed border-gray-800 rounded-xl flex items-center justify-center gap-2">
                <Loader2 size={14} className="animate-spin" />
                프로젝트 목록 불러오는 중...
              </p>
            ) : projects.length === 0 ? (
              <p className="text-center text-gray-600 py-12 text-sm border border-dashed border-gray-800 rounded-xl">
                프로젝트가 없습니다. 기기 연결 후 App Tester 새로고침을 누르거나 폰의 App Tester 로그인/테스터 초대를 확인하세요.
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-2">
                {projects.map((p) => {
                  const isInstalled = p.installed || (!!p.package && installedSet.has(p.package))
                  return (
                    <button key={p.name} onClick={() => handleSelectProject(p)} disabled={phoneBusy}
                      className="text-left px-4 py-3 rounded-xl border bg-gray-900 border-gray-800 hover:border-violet-600 disabled:cursor-not-allowed disabled:opacity-50 transition-colors">
                      <div className="flex items-center gap-1.5">
                        <p className="text-sm font-medium text-gray-100 truncate">{p.name}</p>
                        <span className={`flex-none text-[10px] px-1.5 py-0.5 rounded-full ${
                          isInstalled ? 'bg-emerald-900/50 text-emerald-300' : 'bg-gray-800 text-gray-500'
                        }`}>
                          {isInstalled ? '설치됨' : '미설치'}
                        </span>
                      </div>
                      <p className="text-xs text-gray-500 truncate">{p.package || p.info || '패키지 미확인'}</p>
                    </button>
                  )
                })}
              </div>
            )}
          </div>
          {msg && <p className="text-xs text-gray-400 truncate">{msg}</p>}
          <p className="text-center text-xs text-gray-600">프로젝트를 선택하면 버전 목록이 표시됩니다.</p>
        </>
      ) : (
        <>
          {/* ── 2단계: 버전 목록 + 설치/삭제 ─────────────────── */}
          <div className="flex items-center gap-3">
            <button onClick={handleBack} disabled={busy}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-200 disabled:opacity-50 transition-colors">
              <ArrowLeft size={13} /> 프로젝트 목록
            </button>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-gray-100 flex items-center gap-1.5 truncate">
                {currentProject ? currentProject.name : gameLabel(selectedPackage)}
                <span className={`flex-none text-[10px] px-1.5 py-0.5 rounded-full ${
                  headerInstalled ? 'bg-emerald-900/50 text-emerald-300' : 'bg-gray-800 text-gray-500'
                }`}>
                  {headerInstalled ? '설치됨' : '미설치'}
                </span>
                {headerInstalled && installedVersion && (
                  <span className="flex-none text-[10px] text-gray-500">{installedVersion}</span>
                )}
              </p>
              <p className="text-xs text-gray-500 truncate">
                {currentProject ? (currentProject.package || '패키지 미확인 — 설치 시 자동 감지') : selectedPackage}
              </p>
            </div>
            {selectedPackage && (
              <button onClick={handleUninstall} disabled={busy || !connected || !selectedInstalled}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-red-800 hover:bg-red-700 disabled:opacity-40 text-xs font-medium transition-colors">
                <Trash2 size={13} /> 삭제
              </button>
            )}
          </div>

          {currentProject && (
            <div className="rounded-xl border border-violet-900/60 bg-gray-900/60 p-3 space-y-2">
              <div className="flex items-center justify-between">
                <p className="text-xs text-gray-400 flex items-center gap-1.5">
                  <Smartphone size={13} className="text-violet-400" /> 버전 목록
                  {currentProject.info && <span className="text-gray-600">· {currentProject.info}</span>}
                </p>
                <button onClick={() => handleTesterLoadBuilds(currentProject.name, true)} disabled={phoneBusy || !connected}
                  className="flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50">
                  {testerLoading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
                  새로고침
                </button>
              </div>

              {testerLoading && testerBuilds.length === 0 ? (
                <p className="text-center text-gray-500 py-8 text-sm flex items-center justify-center gap-2">
                  <Loader2 size={14} className="animate-spin" />
                  버전 목록 불러오는 중...
                </p>
              ) : testerBuilds.length === 0 ? (
                <p className="text-center text-gray-600 py-8 text-sm">
                  버전이 없습니다 — 테스터 초대/로그인 확인 후 새로고침하세요.
                </p>
              ) : (
                <div className="max-h-72 overflow-y-auto scrollbar-thin flex flex-col gap-1">
                  {testerBuilds.map((b) => {
                    const active = b.version === selectedTesterBuild
                    return (
                      <button key={b.version} onClick={() => setSelectedTesterBuild(b.version)}
                        disabled={busy}
                        className={`text-left px-3 py-2 rounded-lg border text-xs transition-colors ${
                          active ? 'bg-violet-950/50 border-violet-600' : 'bg-gray-900 border-gray-800 hover:border-gray-600'
                        }`}>
                        <span className="text-gray-100 font-medium">{b.version}</span>
                        {b.info && <span className="text-gray-500 ml-2">{b.info}</span>}
                      </button>
                    )
                  })}
                </div>
              )}

              <div className="flex justify-end">
                <button onClick={handleTesterInstall} disabled={phoneBusy || !connected || !selectedTesterBuild}
                  className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-violet-700 hover:bg-violet-600 disabled:opacity-50 text-xs font-medium transition-colors">
                  {busy ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
                  선택한 버전 설치
                </button>
              </div>
            </div>
          )}

          {msg && <p className="text-xs text-gray-400 truncate">{msg}</p>}

          <div className="flex justify-end">
            <button onClick={onNext} disabled={!canProceed}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed text-sm font-medium transition-colors">
              테스트케이스 선택 <ArrowRight size={16} />
            </button>
          </div>
        </>
      )}
      {!connected && (
        <p className="text-center text-xs text-gray-500 -mt-2">ADB 연결 후 진행할 수 있습니다.</p>
      )}
    </div>
  )
}
