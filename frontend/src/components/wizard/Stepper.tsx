import { Check } from 'lucide-react'

export type WizardStep = 1 | 2 | 3

const STEPS: { id: WizardStep; label: string }[] = [
  { id: 1, label: '게임 선택' },
  { id: 2, label: '테스트 실행' },
  { id: 3, label: '결과 리포트' },
]

interface Props {
  current: WizardStep
  onJump?: (step: WizardStep) => void
}

export default function Stepper({ current, onJump }: Props) {
  return (
    <div className="flex items-center justify-center gap-2 py-4 border-b border-gray-800 bg-gray-900">
      {STEPS.map((s, i) => {
        const done = s.id < current
        const active = s.id === current
        const clickable = onJump && s.id < current
        return (
          <div key={s.id} className="flex items-center gap-2">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => clickable && onJump?.(s.id)}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
                active
                  ? 'bg-blue-600 text-white'
                  : done
                    ? 'bg-emerald-900/40 text-emerald-300 hover:bg-emerald-900/60'
                    : 'bg-gray-800 text-gray-500'
              } ${clickable ? 'cursor-pointer' : 'cursor-default'}`}
            >
              <span
                className={`flex-none w-5 h-5 rounded-full flex items-center justify-center text-xs ${
                  active ? 'bg-white text-blue-600' : done ? 'bg-emerald-500 text-white' : 'bg-gray-700 text-gray-400'
                }`}
              >
                {done ? <Check size={12} /> : s.id}
              </span>
              {s.label}
            </button>
            {i < STEPS.length - 1 && <div className="w-8 h-px bg-gray-700" />}
          </div>
        )
      })}
    </div>
  )
}
