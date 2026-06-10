// In-app confirmation (replaces window.confirm's ugly native popup).
// askConfirm() resolves true/false; ConfirmDialog.svelte (mounted once in
// Editor) renders the open state. Callers offering a power-user skip pass
// the gesture's shiftKey themselves (convention: shift+click = no confirm).

export const confirmState = $state<{
  open: boolean
  title: string
  body: string
  confirmLabel: string
  danger: boolean
}>({ open: false, title: '', body: '', confirmLabel: 'confirm', danger: true })

let resolver: ((ok: boolean) => void) | null = null

export function askConfirm(opts: {
  title: string
  body?: string
  confirmLabel?: string
  danger?: boolean
}): Promise<boolean> {
  resolver?.(false) // a newer dialog supersedes any unanswered one
  return new Promise((resolve) => {
    confirmState.open = true
    confirmState.title = opts.title
    confirmState.body = opts.body ?? ''
    confirmState.confirmLabel = opts.confirmLabel ?? 'confirm'
    confirmState.danger = opts.danger ?? true
    resolver = resolve
  })
}

export function settleConfirm(ok: boolean) {
  confirmState.open = false
  resolver?.(ok)
  resolver = null
}
