import ApkTab from './ApkTab'
import ModuleEditorTab from './ModuleEditorTab'
import type { AgentInfo } from '../../types'

interface Props {
  agent: AgentInfo | null
}

export default function ManagementTab({ agent }: Props) {
  return (
    <div className="space-y-6">
      <section className="rounded-2xl border border-gray-800 bg-gray-900/50 p-5">
        <h1 className="text-lg font-semibold text-gray-100">게임패키지 및 모듈 추가</h1>
        <p className="mt-1 text-sm text-gray-400">
          게임 패키지/APK 매핑과 공통 모듈만 여기서 관리합니다. 앱 설치와 삭제는 파이프라인 step에서만 처리합니다.
        </p>
      </section>

      <ApkTab agent={agent} />
      <ModuleEditorTab agent={agent} />
    </div>
  )
}
