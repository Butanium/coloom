# The optimistic-state contract

Design doc, written 2026-06-10 by the team lead after the *fourth* bug of the
same family in 24 hours. Read this before building any UI surface that both
(a) shows server state and (b) lets the user change it.

## The family of bugs

coloom's client is a mirror of server state (the weave snapshot, generators,
cursors…) that users mutate *through*. Every surface that naively combines
"render from server state" with "user edits it live" has produced the same
failure, four times, in four guises:

| # | Incident | Guise |
|---|---|---|
| 1 | Cursor flicker on fast navigation (round 4) | your own `cursor_moved` echo, arriving late, yanked the cursor back |
| 2 | Free-form edit double-append (round 4) | edit 2 diffed against a baseline that didn't yet contain edit 1 |
| 3 | Stale-refetch revert (round 5) | a weave snapshot fetched *before* an event landed was assigned *after* it, silently undoing it |
| 4 | Quick-row value jumping under the pointer (round 5.1) | a PATCH response re-seeded an input the user was still dragging |
| 5 | Cursor clawed back to a stale node after an edit (round 5.1) | a snapshot fetched *before* an optimistic cursor move was assigned *after* it — and leg 2's patch replay couldn't help because leg 1 had absorbed the own-origin echo, leaving nothing to replay |

Same disease each time: **the client treats "latest thing I heard from the
server" as authoritative, but the user is locally ahead of the server.**

## The contract (three legs)

Any optimistic surface — anything that updates locally before the server
confirms — must implement all three legs. Each leg answers a different
question; missing any one of them reproduces one of the incidents above.

### Leg 1 — Echo absorption: *"is this event just me?"*

Every mutating request carries a per-tab `X-Coloom-Client` id; the server
stamps it into `event.payload.origin`. An event whose `origin` is your own
CLIENT_ID describes a change you already applied optimistically — **skip it**
(`isOwnEvent()` in `state.svelte.ts`). Applying it anyway is at best a no-op
and at worst (out-of-order arrival) a rollback. Fixed incident 1.

Echo absorption applies ONLY to changes you actually applied optimistically.
A server-side *side effect* of your request that you did NOT apply locally
(e.g. a split moving your cursor) is not an echo — you need it. Skip by
"did I locally apply this change," not by "did I cause this request."

### Legs interact: the 1×2 hole (incident 5)

Leg 1 absorbs your own echoes; leg 2 replays in-flight events over stale
snapshots. Combined, they have a hole: an optimistically-applied change whose
echo was absorbed has **no event left to replay** — so a snapshot fetched
before the change but assigned after it silently reverts it, and nothing ever
corrects it. The fix is a **pending-ops ledger**: optimistic operations are
re-asserted over every incoming snapshot until a snapshot whose fetch
*started after* the operation's POST settled retires them. Subtlety worth its
own sentence: retiring on "my echo arrived" is WRONG — the echo can arrive
before the stale snapshot does. Only fetch-start-after-settle is safe.

### Leg 2 — Causality: *"is this state newer than what I have?"*

Server reads (refetches, snapshots) race with the event stream and with each
other. Rules:

- **Latest-initiated wins**: an older fetch must never assign over a newer
  one's result (track a fetch generation counter).
- **Patch replay**: events applied locally while a fetch was in flight must
  be re-applied on top of the fetched snapshot before it's assigned
  (`refetchWeave` in `state.svelte.ts`). Fixed incident 3.
- **Verified baselines**: an edit computed as a *diff against state X* must
  not be submitted until the live state provably contains X
  (`editbuffer.ts` `awaitingBaseline`). Fixed incident 2.

### Leg 3 — Interaction authority: *"is the user mid-gesture right now?"*

While the user is actively manipulating a widget — input focused, drag in
progress, IME composing — **the local widget owns its value**. Server
echoes, refetch results, and reactive re-derivations must NOT write into it,
even when legs 1–2 say the data is fresh; the freshest server state is still
*older than the user's hands*. Reconcile at gesture end (blur, pointer-up),
and even then skip the write when the incoming value equals your own last
sent write (it's your echo coming home). Fixed incident 4.

Corollary: prefer **commit-on-release** for continuous gestures (drags) —
render locally every frame, send once at the end. It shrinks the window leg 3
has to defend, and saves request churn. It does not *replace* leg 3: a slow
response can still land after the next gesture begins.

## How to use this when building a new surface

1. **Read-only surface?** No contract needed — render server state, done.
2. **Mutating surface, applied optimistically?** All three legs. Steal the
   existing mechanisms: `origin`/`isOwnEvent` (leg 1), fetch-generation +
   patch replay / baseline gating (leg 2), the interaction-authority helper
   (leg 3, `web/src/lib/` — built in round 5.1).
3. **Mutating surface, NOT optimistic** (apply only after server confirms)?
   You escape legs 1–2 but **leg 3 still binds** the widget the user is
   touching: a confirmed-but-stale value must still not be written under an
   active gesture.
4. When in doubt, ask: *whose pen is on this value right now — ours or the
   user's?* If the user's, the network does not get to write.

## Corollary — DOM ownership (a different axis, same lesson)

Incident 6 (round 5.1, the blanking text pane) looked like this family but
wasn't: the conflict was **view vs framework**, not client vs server. A
"stray node sweep" in the contenteditable pane removed an empty `#text` node
that was *Svelte's own block anchor*; Chromium types into/around anchors, so
every subsequent insert landed in detached DOM and silently vanished.

The rule: **never surgically mutate framework-managed DOM.** If a
contenteditable (or any user-mutable region) needs to be reset, replace the
whole element via the framework (`{#key epoch}`) so the framework re-anchors;
don't reach in and "clean up" nodes you didn't create — you can't tell user
debris from framework structure. Same root lesson as the three legs: know
whose pen is on the state — ours, the user's, the server's, *or the
framework's* — before writing.

## Why not "just don't be optimistic"?

Round 4 tried instant-feel navigation and it's the single biggest UX win in
the app (cursor moves went from laggy to instant). Collaborative looming
needs both: my changes feel local-first, everyone else's stream in live. The
contract is the price of having both; this doc exists so it's paid once per
*mechanism*, not once per *bug*.
