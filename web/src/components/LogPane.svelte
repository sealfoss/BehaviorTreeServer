<script lang="ts">
  // The log pane (design 11.5): the latest lines with a level filter, following the end unless
  // the user scrolled up; older lines are fetched (ask log_before) when scrolled to the top.
  import { session } from '../lib/stores/session.svelte';
  import { LOG_LEVELS } from '../lib/types';

  const log = session.log;
  const NAMES = ['NONE', 'ERROR', 'WARNING', 'DEBUG', 'INFO'];
  let level = $state(4);
  let box = $state<HTMLDivElement | null>(null);
  let follow = $state(true);
  let fetching = false;

  const rows = $derived(log.rows.filter((row) => row.level <= level));

  $effect(() => {
    void rows.length;
    void rows[rows.length - 1];
    if (follow && box) {
      const element = box;
      queueMicrotask(() => (element.scrollTop = element.scrollHeight));
    }
  });

  function scrolled(): void {
    if (!box) return;
    follow = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
    if (box.scrollTop === 0) void older();
  }

  async function older(): Promise<void> {
    if (fetching || log.firstSeq <= 0) return;
    fetching = true;
    const before = log.firstSeq;
    const height = box?.scrollHeight ?? 0;
    const reply = await session.ask('log_before', { before, count: 200 });
    fetching = false;
    if (reply?.t !== 'ok') return;
    log.prepend(Number(reply.first ?? 0), (reply.lines ?? []) as [string, number, string][]);
    queueMicrotask(() => {
      if (box) box.scrollTop = box.scrollHeight - height;
    });
  }
</script>

<section class="log-pane" data-testid="log-pane">
  <div class="log-controls">
    <label>
      Show up to
      <select bind:value={level} data-testid="log-level-filter">
        {#each LOG_LEVELS.slice(1) as text, index (index)}<option value={index + 1}>{text}</option>{/each}
      </select>
    </label>
    {#if log.firstSeq > 0}
      <button type="button" onclick={older} data-testid="log-older">Older lines</button>
    {/if}
    {#if !follow}
      <button type="button" onclick={() => (follow = true)} data-testid="log-follow">Follow</button>
    {/if}
    {#if log.gapBefore}<span class="muted">{log.gapBefore} earlier lines not shown</span>{/if}
  </div>
  <div class="log-lines" bind:this={box} onscroll={scrolled}>
    {#each rows as row (row.seq)}
      <div class="log-line level-{row.level}" data-testid="log-line" data-seq={row.seq}>
        <span class="log-time">{row.time}</span>
        <span class="log-level">{NAMES[row.level] ?? row.level}</span>
        <span class="log-text">{row.text}</span>
      </div>
    {/each}
  </div>
</section>
