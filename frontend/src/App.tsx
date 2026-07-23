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
  const [runResetSignal, setRunResetSignal] = useState(0)
  // 실행 단계는 한 번 방문하면 계속 마운트해둬서 보고서↔실행 화면을 오가도 방금 돌린 스텝이 남는다
  const [everRun, setEverRun] = useState(false)

  const handleComplete = (r: TestResult, s: string, adaptiveRun?: AdaptiveRun) => {
    setResult(r)
    setSince(s)
    setAdaptive(adaptiveRun ?? null)
    setStep(3)
  }

  const goToStep2 = () => { setEverRun(true); setStep(2) }
  const handleRestart = () => {
    setResult(null)
    setAdaptive(null)
    setRunResetSignal((v) => v + 1)
    setStep(1)
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
        <div className={step === 1 ? 'flex-1 min-h-0 flex flex-col' : 'hidden'}>
          <GameSelectStep
            device={device}
            selectedPackage={pkg}
            onSelect={setPkg}
            onNext={goToStep2}
          />
        </div>
        {(step === 2 || everRun) && (
          <div className={step === 2 ? 'flex-1 min-h-0 flex flex-col' : 'hidden'}>
            <RunStep
              device={device}
              selectedPackage={pkg}
              onBack={() => setStep(1)}
              onComplete={handleComplete}
              resetSignal={runResetSignal}
            />
          </div>
        )}
        {result && (
          <div className={step === 3 ? 'flex-1 min-h-0 flex flex-col' : 'hidden'}>
            <ReportStep
              result={result}
              since={since}
              adaptive={adaptive}
              onRerun={() => setStep(2)}
              onRestart={handleRestart}
            />
          </div>
        )}
      </main>
    </div>
  )
}
