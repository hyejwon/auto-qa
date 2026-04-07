import { useEffect, useRef, useState, type InputHTMLAttributes, type TextareaHTMLAttributes } from 'react'

type StableInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'> & {
  value: string
  onValueChange: (value: string) => void
}

export function StableInput({ value, onValueChange, ...props }: StableInputProps) {
  const [draft, setDraft] = useState(value)
  const composingRef = useRef(false)

  useEffect(() => {
    if (!composingRef.current) setDraft(value)
  }, [value])

  return (
    <input
      {...props}
      value={draft}
      onChange={(e) => {
        const next = e.target.value
        setDraft(next)
        if (!composingRef.current) onValueChange(next)
      }}
      onCompositionStart={(e) => {
        composingRef.current = true
        props.onCompositionStart?.(e)
      }}
      onCompositionEnd={(e) => {
        composingRef.current = false
        const next = e.currentTarget.value
        setDraft(next)
        onValueChange(next)
        props.onCompositionEnd?.(e)
      }}
    />
  )
}

type StableTextareaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'value' | 'onChange'> & {
  value: string
  onValueChange: (value: string) => void
}

export function StableTextarea({ value, onValueChange, ...props }: StableTextareaProps) {
  const [draft, setDraft] = useState(value)
  const composingRef = useRef(false)

  useEffect(() => {
    if (!composingRef.current) setDraft(value)
  }, [value])

  return (
    <textarea
      {...props}
      value={draft}
      onChange={(e) => {
        const next = e.target.value
        setDraft(next)
        if (!composingRef.current) onValueChange(next)
      }}
      onCompositionStart={(e) => {
        composingRef.current = true
        props.onCompositionStart?.(e)
      }}
      onCompositionEnd={(e) => {
        composingRef.current = false
        const next = e.currentTarget.value
        setDraft(next)
        onValueChange(next)
        props.onCompositionEnd?.(e)
      }}
    />
  )
}
