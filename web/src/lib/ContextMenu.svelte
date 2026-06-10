<script lang="ts">
  // Global right-click context menu (spec: docs/ui-specs/lists.md §4, adapted to
  // coloom's named-cursor model). One instance, mounted from Editor.svelte; any
  // view opens it via openContextMenu(nodeId, x, y).
  import { cursorColor } from './colors'
  import {
    closeContextMenu,
    collapsed,
    contextMenu,
    createNode,
    deleteChildren,
    deleteNode,
    generateAt,
    identity,
    moveCursorOf,
    moveMyCursor,
    session,
    threadPath,
    toggleBookmark,
    toggleCollapsed,
    withToast,
  } from './state.svelte'
  import { nodeText } from './types'

  let menuEl: HTMLDivElement | null = $state(null)
  let menuW = $state(0)
  let menuH = $state(0)

  const node = $derived(
    contextMenu.open && contextMenu.nodeId !== null && session.weave
      ? (session.weave.nodes[contextMenu.nodeId] ?? null)
      : null,
  )
  const hasChildren = $derived(node !== null && node.children.length > 0)
  const isRoot = $derived(node !== null && node.parents.length === 0)
  const isCollapsed = $derived(node !== null && collapsed.has(node.id))
  // other participants' cursors — the "summon here" look-here gesture
  const otherCursors = $derived(
    session.weave
      ? Object.values(session.weave.cursors).filter((c) => c.name !== identity.name)
      : [],
  )

  // clamp to the viewport (menu size measured via dimension bindings)
  const left = $derived(
    Math.max(4, Math.min(contextMenu.x, window.innerWidth - menuW - 4)),
  )
  const top = $derived(
    Math.max(4, Math.min(contextMenu.y, window.innerHeight - menuH - 4)),
  )

  /** Fire the action, THEN close. Order matters: in Svelte 5 the
   * {@const nodeId = node.id} the action closures capture is a lazy derived —
   * closing first nulls contextMenu.nodeId and the closure would read null.id
   * (this was a real bug: every menu item was a no-op with a TypeError). */
  function run(action: () => unknown) {
    action()
    closeContextMenu()
  }

  function expandAllBelow(rootId: string) {
    const weave = session.weave
    if (!weave) return
    const stack = [rootId]
    while (stack.length > 0) {
      const id = stack.pop()!
      collapsed.delete(id)
      const n = weave.nodes[id]
      if (n) stack.push(...n.children)
    }
  }

  function copyText(text: string) {
    void withToast(() => navigator.clipboard.writeText(text))
  }

  function threadText(nodeId: string): string {
    const weave = session.weave
    if (!weave) return ''
    return threadPath(weave, nodeId)
      .map((id) => nodeText(weave.nodes[id]))
      .join('')
  }

  function onWindowPointerDown(e: PointerEvent) {
    if (!contextMenu.open) return
    if (menuEl && e.target instanceof Node && menuEl.contains(e.target)) return
    closeContextMenu()
  }

  function onWindowKeydown(e: KeyboardEvent) {
    if (contextMenu.open && e.key === 'Escape') closeContextMenu()
  }
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeydown} />

{#if contextMenu.open && node}
  {@const nodeId = node.id}
  <div
    class="menu"
    role="menu"
    tabindex="-1"
    bind:this={menuEl}
    bind:clientWidth={menuW}
    bind:clientHeight={menuH}
    style:left="{left}px"
    style:top="{top}px"
    oncontextmenu={(e) => e.preventDefault()}
  >
    <button role="menuitem" onclick={() => run(() => generateAt(nodeId))}>
      generate here
    </button>
    <button
      role="menuitem"
      onclick={() => run(() => generateAt(nodeId, { moveCursor: true }))}
    >
      generate and follow
    </button>
    <button
      role="menuitem"
      onclick={() => run(() => createNode('', { parentId: nodeId, moveCursor: true }))}
    >
      add child
    </button>
    <button
      role="menuitem"
      disabled={isRoot}
      title={isRoot ? 'roots have no siblings' : undefined}
      onclick={() =>
        run(() =>
          createNode('', { parentId: node.parents[0] ?? null, moveCursor: true }),
        )}
    >
      add sibling
    </button>

    <div class="sep"></div>

    <button role="menuitem" onclick={() => run(() => toggleBookmark(nodeId))}>
      {node.bookmarked ? 'remove bookmark' : 'bookmark'}
    </button>
    <button role="menuitem" onclick={() => run(() => moveMyCursor(nodeId))}>
      move my cursor here
    </button>
    {#each otherCursors as cursor (cursor.name)}
      <button
        role="menuitem"
        class="summon"
        onclick={() => run(() => moveCursorOf(cursor.name, nodeId))}
      >
        <span class="dot" style:background={cursorColor(cursor.name)}></span>
        summon {cursor.name} here
      </button>
    {/each}

    <div class="sep"></div>

    <button
      role="menuitem"
      disabled={!hasChildren}
      onclick={() => run(() => toggleCollapsed(nodeId))}
    >
      {isCollapsed ? 'expand subtree' : 'collapse subtree'}
    </button>
    <button
      role="menuitem"
      disabled={!hasChildren}
      onclick={() => run(() => expandAllBelow(nodeId))}
    >
      expand all below
    </button>

    <div class="sep"></div>

    <button role="menuitem" onclick={() => run(() => copyText(nodeText(node)))}>
      copy text
    </button>
    <button role="menuitem" onclick={() => run(() => copyText(threadText(nodeId)))}>
      copy thread text
    </button>

    <div class="sep"></div>

    <button
      role="menuitem"
      class="danger"
      title="removes this node and its entire subtree"
      onclick={() => run(() => deleteNode(nodeId))}
    >
      delete node
    </button>
    <button
      role="menuitem"
      class="danger"
      disabled={!hasChildren}
      title="removes every child subtree"
      onclick={() => run(() => deleteChildren(nodeId))}
    >
      delete children
    </button>
  </div>
{/if}

<style>
  .menu {
    position: fixed;
    z-index: 1000;
    min-width: 190px;
    padding: 0.25rem;
    display: flex;
    flex-direction: column;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: 0 6px 20px rgb(0 0 0 / 0.45);
    font-size: var(--fs-ui);
    user-select: none;
  }
  .menu button {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
    text-align: left;
    padding: 0.35rem 0.6rem;
    border: none;
    border-radius: 4px;
    background: none;
    color: var(--text);
    cursor: pointer;
    white-space: nowrap;
  }
  .menu button:hover:not(:disabled) {
    background: var(--bg-raised);
  }
  .menu button:disabled {
    color: var(--text-dim);
    opacity: 0.55;
    cursor: default;
  }
  .menu button.danger:not(:disabled) {
    color: var(--danger);
  }
  .summon .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .sep {
    height: 1px;
    margin: 0.2rem 0.3rem;
    background: var(--border);
  }
</style>
