import axios from 'axios'
import type { DeviceInfo, Template } from '../types'

const api = axios.create({ baseURL: '/api' })

export const deviceApi = {
  getStatus: () => api.get<DeviceInfo>('/device').then((r) => r.data),
}

export const apkApi = {
  list: () => api.get<{ apks: string[] }>('/apks').then((r) => r.data),
  install: (filename: string) =>
    api.post<{ success: boolean; message: string }>('/apk/install', { filename }).then((r) => r.data),
}

export const packageApi = {
  list: () => api.get<{ packages: string[] }>('/packages').then((r) => r.data),
  getApkMap: () => api.get<{ map: Record<string, string> }>('/package-apk-map').then((r) => r.data),
  uninstall: (pkg: string) =>
    api.post<{ success: boolean; message: string }>('/app/uninstall', { package: pkg }).then((r) => r.data),
}

export const templateApi = {
  list: () => api.get<{ templates: string[] }>('/templates').then((r) => r.data),
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
      .post<{ title: string; steps_count: number; yaml: string }>('/plan/generate', {
        scenario,
        package: pkg,
      })
      .then((r) => r.data),
}

export const testApi = {
  run: (payload: { title: string; package: string; steps: object[]; session_id: string; record: boolean }) =>
    api.post<{ session_id: string; status: string }>('/test/run', payload).then((r) => r.data),
  stop: (session_id: string) =>
    api.post<{ success: boolean; message: string }>('/test/stop', { session_id }).then((r) => r.data),
}

export const recordingApi = {
  list: () => api.get<{ recordings: string[] }>('/recordings').then((r) => r.data),
}
