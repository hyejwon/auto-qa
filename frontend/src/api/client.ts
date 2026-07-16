import axios from 'axios'
import type { DeviceEntry, DeviceInfo, PreflightResult, TapDebug, Template, TestResult } from '../types'

const api = axios.create({ baseURL: '/api' })

// ── 디바이스 / ADB 연결 체크 ────────────────────────────────────
export const deviceApi = {
  getStatus: (device?: string) =>
    api.get<DeviceInfo>('/device', { params: device ? { device } : {} }).then((r) => r.data),
  list: () =>
    api.get<{ devices: DeviceEntry[] }>('/devices').then((r) => ({
      devices: Array.isArray(r.data?.devices) ? r.data.devices : [],
    })),
  connect: (address: string) =>
    api
      .post<{ success: boolean; message: string; address?: string }>('/devices/connect', { address })
      .then((r) => r.data),
  disconnect: (address: string) =>
    api
      .post<{ success: boolean; message: string }>('/devices/disconnect', { address })
      .then((r) => r.data),
}

export const preflightApi = {
  check: (device?: string) =>
    api.get<PreflightResult>('/preflight', { params: device ? { device } : {} }).then((r) => r.data),
  reconnect: () =>
    api
      .post<{ success: boolean; device: DeviceInfo; log: string[] }>('/device/reconnect')
      .then((r) => r.data),
}

// ── 게임(패키지) ────────────────────────────────────────────────
export const packageApi = {
  list: (device?: string) =>
    api.get<{ packages: string[] }>('/packages', { params: device ? { device } : {} }).then((r) => ({
      packages: Array.isArray(r.data?.packages) ? r.data.packages : [],
    })),
  getApkMap: () => api.get<{ map: Record<string, string> }>('/package-apk-map').then((r) => ({
    map: r.data?.map ?? {},
  })),
  uninstall: (pkg: string, device?: string) =>
    api
      .post<{ success: boolean; message: string }>('/app/uninstall', { package: pkg, device: device ?? '' })
      .then((r) => r.data),
}

// ── Firebase App Distribution 빌드 ─────────────────────────────
export interface AppDistRelease {
  name: string
  display_version: string
  build_version: string
  create_time: string
  release_notes: string
  cached: boolean
}

export const appdistApi = {
  apps: () =>
    api.get<{ apps: string[]; configured: boolean }>('/appdist/apps').then((r) => ({
      apps: Array.isArray(r.data?.apps) ? r.data.apps : [],
      configured: !!r.data?.configured,
    })),
  releases: (pkg: string) =>
    api
      .get<{ releases: AppDistRelease[] }>('/appdist/releases', { params: { package: pkg } })
      .then((r) => ({ releases: Array.isArray(r.data?.releases) ? r.data.releases : [] })),
  install: (app: string, releaseName: string, device?: string) =>
    api
      .post<{ success: boolean; message: string; apk?: string }>('/appdist/install', {
        app,
        release_name: releaseName,
        device: device ?? '',
      })
      .then((r) => r.data),
}

// ── APK 설치 ────────────────────────────────────────────────────
export const apkApi = {
  list: () => api.get<{ apks: string[] }>('/apks').then((r) => ({
    apks: Array.isArray(r.data?.apks) ? r.data.apks : [],
  })),
  install: (filename: string, device?: string) =>
    api
      .post<{ success: boolean; message: string }>('/apk/install', { filename, device: device ?? '' })
      .then((r) => r.data),
}

// ── 테스트케이스(템플릿) ─────────────────────────────────────────
export const templateApi = {
  list: () => api.get<{ templates: string[] }>('/templates').then((r) => ({
    templates: Array.isArray(r.data?.templates) ? r.data.templates : [],
  })),
  get: (name: string) => api.get<{ template: Template }>(`/templates/${name}`).then((r) => r.data),
  save: (name: string, content: string) =>
    api
      .post<{ success: boolean; filename?: string; error?: string }>('/templates', { name, content })
      .then((r) => r.data),
}

// ── 테스트 실행 ─────────────────────────────────────────────────
export const testApi = {
  run: (payload: { title: string; package: string; steps: object[]; session_id: string; device?: string }) =>
    api
      .post<{ session_id: string; status: string }>('/test/run', { record: false, device: '', ...payload })
      .then((r) => r.data),
  stop: (session_id: string) =>
    api
      .post<{ success: boolean; message: string }>('/test/stop', { session_id })
      .then((r) => r.data),
}

// ── 어댑티브 실행 (일시 비활성화)
// export const adaptiveApi = {
//   run: (payload: {
//     session_id: string
//     title: string
//     package: string
//     steps: object[]
//     goal: string
//     max_iterations: number
//     reset_app_each_iteration: boolean
//   }) => api.post<{ run: AdaptiveRun }>('/adaptive/test/run', payload).then((r) => r.data.run),
// }

// ── 디버그 탭 스크린샷 ──────────────────────────────────────────
export const debugApi = {
  taps: (since: string) =>
    api.get<{ taps: TapDebug[] }>('/debug/taps', { params: { since } }).then((r) => ({
      taps: Array.isArray(r.data?.taps) ? r.data.taps : [],
    })),
}

// ── 리포트 내보내기 ──────────────────────────────────────────────
export const reportApi = {
  csv: (payload: { result: TestResult; taps: TapDebug[] }) =>
    api
      .post<Blob>('/reports/csv', payload, { responseType: 'blob' })
      .then((r) => ({
        blob: r.data,
        filename: filenameFromDisposition(r.headers['content-disposition']) ?? 'auto-qa-report.csv',
      })),
}

// ── WebSocket URL (로컬 단일 노드) ──────────────────────────────
export function wsUrl(sessionId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws/logs/${sessionId}`
}

function filenameFromDisposition(disposition?: string): string | null {
  if (!disposition) return null
  const utf8 = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8?.[1]) return decodeURIComponent(utf8[1])
  const plain = disposition.match(/filename="?([^";]+)"?/i)
  return plain?.[1] ?? null
}

// find_and_tap 디버그 필터용 타임스탬프 (백엔드 'YYYYMMDD_HHMMSS_mmm' 포맷)
export function debugSince(d: Date = new Date()): string {
  const p = (n: number, w = 2) => String(n).padStart(w, '0')
  return (
    `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_` +
    `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}_000`
  )
}
