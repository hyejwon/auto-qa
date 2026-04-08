import ApkTab from './ApkTab'
import ModuleEditorTab from './ModuleEditorTab'
import type { AgentInfo } from '../../types'

interface Props {
  agent: AgentInfo | null
}

export default function ManagementTab({ agent }: Props) {
  return (
    <div className="flex-1 min-h-0 flex gap-4">
      <div className="w-[420px] flex-none flex flex-col">
        <ApkTab agent={agent} />
      </div>
      <div className="flex-1 min-w-0 flex flex-col">
        <ModuleEditorTab agent={agent} />
      </div>
    </div>
  )
}
