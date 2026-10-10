// Node test for the desktop half. Run: node --test tests/js/
// A resolve hook maps '@hermes/plugin-sdk' to a stub so plugin.js loads unchanged.
import { registerHooks } from 'node:module'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const STUB = new URL('./sdk-stub.mjs', import.meta.url).href
registerHooks({
  resolve(specifier, context, next) {
    return specifier === '@hermes/plugin-sdk' ? { url: STUB, shortCircuit: true } : next(specifier, context)
  }
})

const sdk = await import('@hermes/plugin-sdk')
const { default: plugin, makeReporter } = await import('../../desktop/plugin.js')

function fakeCtx() {
  const calls = []
  const ctx = {
    calls,
    contributions: [],
    disposers: [],
    intervals: [],
    listeners: [],
    restResult: { ok: true },
    rest: async (path, opts) => { calls.push({ path, opts }); return ctx.restResult },
    register: c => { ctx.contributions.push(c); return () => {} },
    onDispose: fn => ctx.disposers.push(fn),
    setInterval: (fn, ms) => { ctx.intervals.push({ fn, ms }); return () => {} },
    addEventListener: (target, type, fn) => { ctx.listeners.push({ type, fn }); return () => {} }
  }
  return ctx
}

test('manifest: opt-in, id matches folder', () => {
  assert.equal(plugin.id, 'tick')
  assert.equal(plugin.defaultEnabled, false)
})

test('reports the device zone once, then only on change', async () => {
  const ctx = fakeCtx()
  let zone = 'America/New_York'
  let t = 0
  const report = makeReporter(ctx, () => zone, () => t)
  await report()
  await report()
  assert.equal(ctx.calls.length, 1)
  assert.deepEqual(ctx.calls[0], {
    path: '/client',
    opts: { method: 'POST', body: { timezone: 'America/New_York', source: 'desktop' } }
  })
  zone = 'America/Los_Angeles'
  await report()
  assert.equal(ctx.calls.length, 2)
})

test('re-reports after the refresh window so the record stays fresh', async () => {
  const ctx = fakeCtx()
  let t = 0
  const report = makeReporter(ctx, () => 'UTC', () => t)
  await report()
  t += 6 * 3600 * 1000 + 1
  await report()
  assert.equal(ctx.calls.length, 2)
})

test('force reports even when nothing changed (profile switch)', async () => {
  const ctx = fakeCtx()
  const report = makeReporter(ctx, () => 'UTC', () => 0)
  await report()
  await report(true)
  assert.equal(ctx.calls.length, 2)
})

test('backend errors are swallowed and retried next time', async () => {
  const ctx = fakeCtx()
  ctx.rest = async () => { ctx.calls.push(1); throw new Error('backend off') }
  const report = makeReporter(ctx, () => 'UTC', () => 0)
  await report()
  await report()
  assert.equal(ctx.calls.length, 2)
})

test('empty zone sends nothing', async () => {
  const ctx = fakeCtx()
  const report = makeReporter(ctx, () => '', () => 0)
  await report()
  assert.equal(ctx.calls.length, 0)
})

test('register wires interval, visibility, profile change and a palette command', async () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  await new Promise(r => setTimeout(r, 0))
  assert.equal(ctx.calls.length, 1, 'reports on load')
  assert.ok(ctx.intervals.some(i => i.ms === 15 * 60 * 1000))
  assert.ok(ctx.listeners.some(l => l.type === 'visibilitychange'))
  const palette = ctx.contributions.find(c => c.area === sdk.PALETTE_AREA)
  assert.ok(palette && typeof palette.data.run === 'function')
  sdk.host.state.profile.set('work')
  await new Promise(r => setTimeout(r, 0))
  assert.equal(ctx.calls.length, 2, 'reports again on profile switch')
  assert.equal(ctx.disposers.length >= 1, true, 'profile subscription is disposed with the plugin')
})

test('a failed forced report on profile switch is retried on the next check', async () => {
  const ctx = fakeCtx()
  let profile = 'a'
  let fail = false
  ctx.rest = async (path, opts) => {
    ctx.calls.push({ profile, path })
    if (fail) throw new Error('reconnecting')
    return { ok: true }
  }
  const report = makeReporter(ctx, () => 'America/New_York', () => 0, () => profile)
  assert.equal(await report(true), true) // profile a delivered
  profile = 'b'
  fail = true
  assert.equal(await report(true), false) // switch while gateway reconnects
  fail = false
  assert.equal(await report(), true, 'next interval check retries b')
  assert.deepEqual(ctx.calls.map(c => c.profile), ['a', 'b', 'b'])
  assert.equal(await report(), false, 'then dedupes again')
})

test('a non-forced check after switching profile reports to the new profile', async () => {
  const ctx = fakeCtx()
  let profile = 'a'
  const report = makeReporter(ctx, () => 'UTC', () => 0, () => profile)
  await report()
  profile = 'b'
  await report()
  assert.equal(ctx.calls.length, 2)
})
