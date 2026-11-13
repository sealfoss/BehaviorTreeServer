<script lang="ts">
  // Configure (design 11.5, 12): the widget's options and the three new ones.
  import { session } from '../lib/stores/session.svelte';
  import { LOG_LEVELS, type TreeConfig } from '../lib/types';

  let { onclose }: { onclose: () => void } = $props();

  const key = session.docs.root;
  const config: TreeConfig = session.docs.get(key)?.config ?? {};
  let tick = $state(config.tick_interval_ms ?? 100);
  let repeat = $state(config.repeat === true);
  let logLevel = $state(config.log_level ?? 4);
  let restore = $state(config.restore_blackboard === true);
  let memory = $state(config.default_memory !== false);
  let waitForCancels = $state(config.wait_for_cancels !== false);
  let cancelTimeout = $state(config.cancel_timeout_ms ?? 5000);
  let onUnconfirmed = $state<'stop' | 'continue'>(config.on_unconfirmed_cancel ?? 'stop');

  function ok(): void {
    const next: TreeConfig = {
      tick_interval_ms: Math.min(Math.max(Math.round(Number(tick) || 100), 1), 60000),
      repeat,
      log_level: Number(logLevel),
      restore_blackboard: restore,
      default_memory: memory,
      wait_for_cancels: waitForCancels,
      cancel_timeout_ms: Math.max(0, Math.round(Number(cancelTimeout) || 0)),
      on_unconfirmed_cancel: onUnconfirmed,
    };
    const changed: TreeConfig = {};
    const defaults: TreeConfig = { tick_interval_ms: 100, repeat: false, log_level: 4, restore_blackboard: false, default_memory: true, wait_for_cancels: true, cancel_timeout_ms: 5000, on_unconfirmed_cancel: 'stop' };
    for (const [name, value] of Object.entries(next)) {
      if ((config[name] ?? defaults[name]) !== value) changed[name] = value;
    }
    if (key && Object.keys(changed).length) void session.sendOp(key, { op: 'doc.set_config', config: changed });
    onclose();
  }
</script>

<div class="modal-backdrop">
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Configure Behavior Tree" data-testid="dialog-configure">
    <h2>Configure Behavior Tree</h2>
    <div class="form">
      <label for="cfg-tick">Tick interval:</label>
      <span><input id="cfg-tick" type="number" min="1" max="60000" bind:value={tick} data-testid="cfg-tick" title="Time between two ticks of the behavior tree." /> ms</span>
      <label for="cfg-mode">Execution:</label>
      <select id="cfg-mode" value={repeat ? '1' : '0'} onchange={(e) => (repeat = (e.currentTarget as HTMLSelectElement).value === '1')} data-testid="cfg-mode">
        <option value="0">Run once (stop when the root completes)</option>
        <option value="1">Loop (execute again each time the root completes)</option>
      </select>
      <label for="cfg-log">Log level:</label>
      <select id="cfg-log" bind:value={logLevel} data-testid="cfg-log-level">
        {#each LOG_LEVELS as text, level (level)}<option value={level}>{text}</option>{/each}
      </select>
      <label class="check wide"><input type="checkbox" bind:checked={restore} data-testid="cfg-restore" /> Restore blackboard values on Stop / Reset</label>
      <label class="check wide"><input type="checkbox" bind:checked={memory} data-testid="cfg-memory" /> New Sequence / Selector nodes use memory</label>
      <label class="check wide" title="A leaf's start waits until the cancels before it are confirmed (design 5.6).">
        <input type="checkbox" bind:checked={waitForCancels} data-testid="cfg-wait-for-cancels" /> Starts wait for earlier cancels
      </label>
      <label for="cfg-cancel-timeout">Cancel timeout:</label>
      <span><input id="cfg-cancel-timeout" type="number" min="0" bind:value={cancelTimeout} data-testid="cfg-cancel-timeout" /> ms</span>
      <label for="cfg-unconfirmed">Unconfirmed cancel:</label>
      <select id="cfg-unconfirmed" bind:value={onUnconfirmed} data-testid="cfg-on-unconfirmed">
        <option value="stop">Stop the tree</option>
        <option value="continue">Continue</option>
      </select>
    </div>
    <p class="muted">Changes apply immediately, including to a running tree.</p>
    <div class="dialog-buttons">
      <button type="button" class="primary" onclick={ok} data-testid="btn-configure-ok">OK</button>
      <button type="button" onclick={onclose} data-testid="btn-configure-cancel">Cancel</button>
    </div>
  </div>
</div>
