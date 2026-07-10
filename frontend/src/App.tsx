import { useState } from 'react'
import Stepper, { type WizardStep } from './components/wizard/Stepper'
import GameSelectStep from './components/wizard/GameSelectStep'
import RunStep from './components/wizard/RunStep'
import ReportStep from './components/wizard/ReportStep'
import DevicePicker from './components/DevicePicker'
import type { AdaptiveRun, TestResult } from './types'

export default function App() {
  const [step, setStep] = useState<WizardStep>(1)
  const [device, setDevice] = useState('')
  const [pkg, setPkg] = useState('')
  const [result, setResult] = useState<TestResult | null>(null)
  const [since, setSince] = useState('')
  const [adaptive, setAdaptive] = useState<AdaptiveRun | null>(null)

  const handleComplete = (r: TestResult, s: string, adaptiveRun?: AdaptiveRun) => {
    setResult(r)
    setSince(s)
    setAdaptive(adaptiveRun ?? null)
    setStep(3)
  }

  return (
    <div className="h-screen overflow-hidden flex flex-col bg-gray-950">
      <header className="px-6 py-3 border-b border-gray-800 bg-gray-900 flex items-center gap-2">
        <span className="text-lg">🎮</span>
        <h1 className="text-sm font-semibold text-gray-100">Auto QA</h1>
        <span className="text-xs text-gray-500">게임 자동 테스트</span>
        <DevicePicker value={device} onChange={setDevice} />
      </header>

      <Stepper current={step} onJump={(s) => setStep(s)} />

      <main className="flex-1 min-h-0 p-6 overflow-hidden flex flex-col">
        {step === 1 && (
          <GameSelectStep
            device={device}
            selectedPackage={pkg}
            onSelect={setPkg}
            onNext={() => setStep(2)}
          />
        )}
        {step === 2 && (
          <RunStep
            device={device}
            selectedPackage={pkg}
            onBack={() => setStep(1)}
            onComplete={handleComplete}
          />
        )}
        {step === 3 && result && (
          <ReportStep
            result={result}
            since={since}
            adaptive={adaptive}
            onRerun={() => setStep(2)}
            onRestart={() => { setResult(null); setAdaptive(null); setStep(1) }}
          />
        )}
      </main>
    </div>
  )
}
