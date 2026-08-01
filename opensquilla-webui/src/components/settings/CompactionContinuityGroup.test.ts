// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const rpcCall = vi.fn()
const waitForConnection = vi.fn().mockResolvedValue(undefined)
const mounted: App[] = []

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: rpcCall, waitForConnection }),
}))

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  rpcCall.mockReset()
})

describe('CompactionContinuityGroup', () => {
  it('loads and patches the independent compaction flag', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.get') {
        return { compaction: { anchor_enabled: true } }
      }
      return {}
    })
    const { createApp, nextTick } = await import('vue')
    const i18n = (await import('@/i18n')).default
    i18n.global.locale.value = 'en'
    const Component = (await import('./CompactionContinuityGroup.vue')).default
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(Component)
    app.use(i18n)
    app.mount(el)
    mounted.push(app)

    await Promise.resolve()
    await nextTick()
    await Promise.resolve()
    await nextTick()

    const toggle = el.querySelector<HTMLInputElement>(
      'input[name="compaction_anchors"]',
    )!
    expect(toggle.checked).toBe(true)

    toggle.checked = false
    toggle.dispatchEvent(new Event('change'))
    await Promise.resolve()
    await nextTick()

    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'compaction.anchor_enabled': false },
    })
  })
})
