<script lang="ts">
  // Source… of a blackboard entry (design 7.4): a ROS topic whose messages set its value.
  // Test takes one message now and shows the value the entry would get (ask sample).
  import { session } from '../lib/stores/session.svelte';

  let { onclose }: { onclose: () => void } = $props();

  const entry = session.blackboard.byId(session.ui.dialogEntry ?? '');
  let topic = $state(entry?.source?.topic ?? '');
  let type = $state(entry?.source?.type ?? '');
  let field = $state(entry?.source?.field ?? '');
  let rate = $state(entry?.source?.max_rate_hz ?? 2);
  let age = $state(entry?.source?.max_age_s ?? 0);
  let topics = $state<[string, string][]>([]);
  let test = $state('');
  let testing = $state(false);

  $effect(() => {
    void session.ask('graph').then((reply) => {
      if (reply?.t === 'ok') {
        const graph = (reply.graph ?? {}) as { topics?: [string, string | string[]][] };
        topics = (graph.topics ?? []).map(([name, types]) => [name, Array.isArray(types) ? (types[0] ?? '') : types]);
      }
    });
  });

  function topicChosen(): void {
    const known = topics.find(([name]) => name === topic);
    if (known && known[1]) type = known[1];
  }

  async function runTest(): Promise<void> {
    testing = true;
    test = '';
    const reply = await session.ask('sample', { topic, type, field });
    testing = false;
    if (!reply) test = 'offline';
    else if (reply.t === 'no') test = `No value: ${reply.reason ?? ''}`;
    else {
      const value = reply.value as { type?: string; value?: unknown } | null;
      test = `${value?.type ?? ''} ${value?.value === undefined ? '' : String(value.value)}${reply.message ? `\n${String(reply.message)}` : ''}`;
    }
  }

  function ok(): void {
    if (!entry) return onclose();
    const source = topic.trim() ? { topic: topic.trim(), type: type.trim(), field: field.trim(), max_rate_hz: Number(rate) || 2, max_age_s: Number(age) || 0 } : null;
    void session.bbOp({ op: 'bb.set_source', id: entry.id, source });
    onclose();
  }

  function remove(): void {
    if (entry) void session.bbOp({ op: 'bb.set_source', id: entry.id, source: null });
    onclose();
  }
</script>

<div class="modal-backdrop">
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Source" data-testid="dialog-source">
    <h2>Source of {entry?.name ?? ''}</h2>
    <div class="form">
      <label for="src-topic">Topic:</label>
      <input id="src-topic" list="src-topics" bind:value={topic} onchange={topicChosen} placeholder="/robot1/battery_state" data-testid="src-topic" />
      <datalist id="src-topics">
        {#each topics as [name, topicType] (name)}<option value={name}>{topicType}</option>{/each}
      </datalist>
      <label for="src-type">Type:</label>
      <input id="src-type" bind:value={type} placeholder="sensor_msgs/msg/BatteryState" data-testid="src-type" />
      <label for="src-field">Field:</label>
      <input id="src-field" bind:value={field} placeholder="percentage (empty: the whole message)" data-testid="src-field" />
      <label for="src-rate">Max rate:</label>
      <span><input id="src-rate" type="number" min="0" step="0.1" bind:value={rate} data-testid="src-rate" /> per second</span>
      <label for="src-age">Max age:</label>
      <span><input id="src-age" type="number" min="0" step="0.1" bind:value={age} data-testid="src-age" /> s (0: never stale)</span>
    </div>
    <div class="test-row">
      <button type="button" onclick={runTest} disabled={!topic.trim() || testing} data-testid="btn-src-test">Test</button>
      <pre class="test-result" data-testid="src-test-result">{testing ? 'Waiting for a message…' : test}</pre>
    </div>
    <div class="dialog-buttons">
      <button type="button" class="primary" onclick={ok} data-testid="btn-src-ok">OK</button>
      {#if entry?.source}<button type="button" onclick={remove} data-testid="btn-src-remove">Remove Source</button>{/if}
      <button type="button" onclick={onclose} data-testid="btn-src-cancel">Cancel</button>
    </div>
  </div>
</div>
