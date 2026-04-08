import axios from 'axios'
import type { AgentInfo, DeviceInfo, PipelineData, PreflightResult, Template } from '../types'

const api = axios.create({ baseURL: '/api' })

// ── 에이전트 관리 (오케스트레이터) ──────────────────────────────
export const agentsApi = {
  list: () => api.get<{ agents: AgentInfo[] }>('/agents').then((r) => r.data),
}

// ── 에이전트별 API (agentName 필수) ─────────────────────────────
const agentPath = (name: string, path: string) => `/agents/${encodeURIComponent(name)}${path}`

export const deviceApi = {
  getStatus: (agentName: string) =>
    api.get<DeviceInfo>(agentPath(agentName, '/device')).then((r) => r.data),
}

export const preflightApi = {
  check: (agentName: string) =>
    api.get<PreflightResult>(agentPath(agentName, '/preflight')).then((r) => r.data),
  reconnect: (agentName: string) =>
    api
      .post<{ success: boolean; device: unknown; log: string[] }>(agentPath(agentName, '/device/reconnect'))
      .then((r) => r.data),
}

export const apkApi = {
  list: (agentName: string) =>
    api.get<{ apks: string[] }>(agentPath(agentName, '/apks')).then((r) => r.data),
  install: (agentName: string, filename: string) =>
    api
      .post<{ success: boolean; message: string }>(agentPath(agentName, '/apk/install'), { filename })
      .then((r) => r.data),
}

export const packageApi = {
  list: (agentName: string) =>
    api.get<{ packages: string[] }>(agentPath(agentName, '/packages')).then((r) => r.data),
  getApkMap: () => api.get<{ map: Record<string, string> }>('/package-apk-map').then((r) => r.data),
  uninstall: (agentName: string, pkg: string) =>
    api
      .post<{ success: boolean; message: string }>(agentPath(agentName, '/app/uninstall'), { package: pkg })
      .then((r) => r.data),
}

export const testApi = {
  run: (agentName: string, payload: { title: string; package: string; steps: object[]; session_id: string; record: boolean }) =>
    api
      .post<{ session_id: string; status: string }>(agentPath(agentName, '/test/run'), payload)
      .then((r) => r.data),
  stop: (agentName: string, session_id: string) =>
    api
      .post<{ success: boolean; message: string }>(agentPath(agentName, '/test/stop'), { session_id })
      .then((r) => r.data),
}

export const pipelineApi = {
  list: () => api.get<{ pipelines: string[] }>('/pipelines').then((r) => ({
    pipelines: Array.isArray(r.data?.pipelines) ? r.data.pipelines : [],
  })),
  get: (name: string) => api.get<PipelineData>(`/pipelines/${name}`).then((r) => r.data),
  save: (name: string, nodes: object[], edges: object[]) =>
    api.post<{ success: boolean; filename?: string }>('/pipelines', { name, nodes, edges }).then((r) => r.data),
  delete: (name: string) => api.delete(`/pipelines/${name}`).then((r) => r.data),
  run: (agentName: string, payload: { nodes: object[]; edges: object[]; session_id: string; record: boolean }) =>
    api
      .post<{ session_id: string; status: string }>(agentPath(agentName, '/pipeline/run'), payload)
      .then((r) => r.data),
}

// ── 오케스트레이터 직접 API ──────────────────────────────────────
export const templateApi = {
  list: () => api.get<{ templates: string[] }>('/templates').then((r) => ({
    templates: Array.isArray(r.data?.templates) ? r.data.templates : [],
  })),
  get: (name: string) => api.get<{ template: Template }>(`/templates/${name}`).then((r) => r.data),
  save: (name: string, content: string, scenario = '') =>
    api
      .post<{ success: boolean; filename?: string; error?: string }>('/templates', { name, content, scenario })
      .then((r) => r.data),
  delete: (name: string) => api.delete(`/templates/${name}`).then((r) => r.data),
}

export const planApi = {
  generate: (scenario: string, pkg: string) =>
    api
      .post<{ title: string; steps_count: number; yaml: string }>('/plan/generate', { scenario, package: pkg })
      .then((r) => r.data),
}

export const recordingApi = {
  list: (agentName: string) =>
    api.get<{ recordings: string[] }>(agentPath(agentName, '/recordings')).then((r) => ({
      recordings: Array.isArray(r.data?.recordings) ? r.data.recordings : [],
    })),
}

// ── WebSocket URL 헬퍼 ───────────────────────────────────────────
export function agentWsUrl(agent: AgentInfo, sessionId: string): string {
  // 에이전트에 직접 WebSocket 연결 (사내망 HTTP)
  return `ws://${agent.ip}:${agent.port}/ws/logs/${sessionId}`
}
