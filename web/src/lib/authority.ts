// Interaction authority — LEG 3 of the optimistic-state contract.
// READ docs/optimistic-state.md BEFORE using or changing this: while the user
// is mid-gesture (input focused, drag in progress, IME composing), the local
// widget owns its value — server echoes, refetches and reactive re-derivations
// must NOT write into it; the freshest server state is still older than the
// user's hands. This helper is the reusable mechanism: every optimistic widget
// guards each user-editable value with one instance and routes ALL
// server-driven writes through `receive()`.
//
// Lifecycle the caller wires up:
//   gestureStart/gestureEnd  focus/blur, dragnum dragStart/dragEnd, IME
//                            composition — depth-counted, so overlapping
//                            gestures (drag blurs the input it started on)
//                            never drop authority on the floor
//   edited(sent)             a local write is committed / scheduled for the
//                            server with value `sent` (echo detection + "our
//                            write is ahead" suppression). Call BEFORE
//                            gestureEnd so the deferred stale value is dropped
//                            rather than applied over the user's commit.
//   failed()                 the server write errored: relinquish authority
//                            (caller should then re-seed from server truth so
//                            the divergence is honest, not hidden)
//   receive(incoming, apply) a server-derived value wants in. Applied only
//                            when no gesture is active AND no local write is
//                            outstanding; deferred to gesture end otherwise.
//                            An incoming value equal to the last sent write is
//                            the echo coming home — it confirms (clears) the
//                            outstanding write instead of re-applying it.
//   reset()                  the widget re-binds to a different entity (e.g.
//                            chip focus moved): forget everything, the caller
//                            seeds the new value unconditionally.

export interface InteractionAuthority<T> {
  gestureStart(): void
  gestureEnd(): void
  edited(sent: T): void
  failed(): void
  receive(incoming: T, apply: (v: T) => void): void
  reset(): void
  readonly inGesture: boolean
}

export function interactionAuthority<T>(
  equals: (a: T, b: T) => boolean = Object.is,
): InteractionAuthority<T> {
  let depth = 0
  let pendingWrite = false // a local edit is debounced/in flight; we're ahead
  let lastSent: T | undefined
  let deferred: { v: T; apply: (v: T) => void } | null = null

  return {
    get inGesture() {
      return depth > 0
    },
    gestureStart() {
      depth++
    },
    gestureEnd() {
      depth = Math.max(0, depth - 1)
      if (depth > 0) return
      const d = deferred
      deferred = null
      // a gesture that committed a local write supersedes whatever the server
      // said mid-gesture (our PATCH overwrites it anyway); only a gesture that
      // ended WITHOUT editing lets the deferred server value through
      if (d && !pendingWrite) d.apply(d.v)
    },
    edited(sent: T) {
      pendingWrite = true
      lastSent = sent
      deferred = null // our write is newer than anything the server deferred
    },
    failed() {
      pendingWrite = false
      lastSent = undefined
      deferred = null
    },
    receive(incoming: T, apply: (v: T) => void) {
      if (depth > 0) {
        deferred = { v: incoming, apply }
        return
      }
      if (pendingWrite) {
        if (lastSent !== undefined && equals(incoming, lastSent)) {
          // our echo coming home: the write is confirmed, nothing to apply
          pendingWrite = false
          lastSent = undefined
        }
        // anything else is older than our outstanding write — drop it; the
        // refresh that follows our write's response will carry the truth
        return
      }
      apply(incoming)
    },
    reset() {
      depth = 0
      pendingWrite = false
      lastSent = undefined
      deferred = null
    },
  }
}
