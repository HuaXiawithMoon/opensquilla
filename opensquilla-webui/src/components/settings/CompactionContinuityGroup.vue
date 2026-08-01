<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import { useRpcStore } from '@/stores/rpc'

const { t } = useI18n()
const rpc = useRpcStore()
const enabled = ref(false)
const busy = ref(false)

async function load(): Promise<void> {
  try {
    await rpc.waitForConnection()
    const config = await rpc.call<{ compaction?: { anchor_enabled?: boolean } }>(
      'config.get',
    )
    enabled.value = config?.compaction?.anchor_enabled === true
  } catch {
    enabled.value = false
  }
}

async function setEnabled(on: boolean): Promise<void> {
  if (busy.value) return
  const previous = enabled.value
  enabled.value = on
  busy.value = true
  try {
    await rpc.call('config.patch.safe', {
      patches: { 'compaction.anchor_enabled': on },
    })
  } catch {
    enabled.value = previous
  } finally {
    busy.value = false
  }
}

onMounted(() => { void load() })
</script>

<template>
  <label class="control-row">
    <div class="control-row__label-block">
      <span class="control-row__label">
        {{ t('setup.advanced.compactionAnchorsLabel') }}
        <span class="compaction-exp">{{ t('setup.advanced.experimental') }}</span>
      </span>
      <span class="control-row__desc">
        {{ t('setup.advanced.compactionAnchorsDesc') }}
      </span>
    </div>
    <div class="control-row__control">
      <span class="compaction-next">
        {{ t('setup.advanced.nextCompactionBadge') }}
      </span>
      <ControlSwitch
        name="compaction_anchors"
        :checked="enabled"
        :busy="busy"
        :aria-label="t('setup.advanced.compactionAnchorsLabel')"
        @change="setEnabled"
      />
    </div>
  </label>
</template>

<style scoped>
.compaction-exp,
.compaction-next {
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border));
  border-radius: var(--radius-full);
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.03em;
  padding: 1px 6px;
  text-transform: uppercase;
}

.compaction-exp {
  margin-left: var(--sp-1);
}
</style>
