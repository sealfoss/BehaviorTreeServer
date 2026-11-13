<script lang="ts">
  // The page's own context menu (replaces the browser's on the canvas, design 11.4).
  import { session } from '../lib/stores/session.svelte';
  import type { MenuItem } from '../lib/stores/ui.svelte';

  const ui = session.ui;
  let openSub = $state<number | null>(null);
  let menuEl = $state<HTMLDivElement | null>(null);

  function slug(label: string): string {
    return label.replace(/\.\.\.$/, '').replace(/\s*\(.*\)$/, '').trim().replace(/[^A-Za-z0-9]+/g, '-');
  }

  function choose(item: MenuItem): void {
    if (item.disabled || item.heading || item.separator || item.submenu) return;
    ui.closeMenu();
    item.action?.();
  }

  const position = $derived.by(() => {
    const menu = ui.menu;
    if (!menu) return '';
    const x = Math.min(menu.x, window.innerWidth - 220);
    const y = Math.min(menu.y, Math.max(0, window.innerHeight - 24 * menu.items.length - 20));
    return `left: ${x}px; top: ${y}px`;
  });

  $effect(() => {
    if (!ui.menu) return;
    openSub = null;
    const close = (event: PointerEvent) => {
      if (menuEl && !menuEl.contains(event.target as Node)) ui.closeMenu();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') ui.closeMenu();
    };
    window.addEventListener('pointerdown', close, true);
    window.addEventListener('keydown', key, true);
    return () => {
      window.removeEventListener('pointerdown', close, true);
      window.removeEventListener('keydown', key, true);
    };
  });
</script>

{#snippet list(items: MenuItem[], sub: boolean)}
  {#each items as item, index (index)}
    {#if item.separator}
      <div class="menu-separator" role="separator"></div>
    {:else if item.heading}
      <div class="menu-heading">{item.label}</div>
    {:else}
      <div
        class="menu-item"
        class:disabled={item.disabled}
        class:has-sub={!!item.submenu}
        role="menuitem"
        tabindex="-1"
        aria-disabled={item.disabled}
        data-testid="menu-{slug(item.label)}"
        onpointerenter={() => {
          if (!sub) openSub = item.submenu ? index : null;
        }}
        onclick={() => choose(item)}
        onkeydown={(e) => e.key === 'Enter' && choose(item)}
      >
        <span class="menu-check">{item.checked ? '✓' : ''}</span>
        {#if item.color}<span class="menu-swatch" style="background: {item.color}"></span>{/if}
        <span class="menu-label">{item.label}</span>
        {#if item.submenu}<span class="menu-arrow">▸</span>{/if}
        {#if item.submenu && openSub === index && !item.disabled}
          <div class="menu submenu" role="menu">{@render list(item.submenu, true)}</div>
        {/if}
      </div>
    {/if}
  {/each}
{/snippet}

{#if ui.menu}
  <div class="menu" role="menu" style={position} bind:this={menuEl} data-testid="context-menu">
    {@render list(ui.menu.items, false)}
  </div>
{/if}
