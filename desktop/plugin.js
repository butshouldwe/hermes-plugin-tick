// tick desktop companion: reports this device's IANA time zone to the tick
// backend so agents show the user's local time, not the server's.
// Opt-in (defaultEnabled: false). Sends only the zone name, for example
// "America/New_York". No location, no clock, no other data.
import { host, PALETTE_AREA } from '@hermes/plugin-sdk'

const REFRESH_MS = 6 * 3600 * 1000 // keep the record fresh (backend treats >72 h as stale)
const CHECK_MS = 15 * 60 * 1000 // notice a zone change (travel, DST zone swap) within 15 min

export function deviceZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
}

export function currentProfile() {
  try {
    return host.state.profile?.get?.() ?? ''
  } catch {
    return ''
  }
}

// Delivery is tracked per destination profile: ctx.rest is profile-aware, so a
// success for profile A says nothing about profile B. A failed send leaves no
// record for that profile, so the next 15-minute or visibility check retries.
export function makeReporter(ctx, getZone = deviceZone, now = () => Date.now(), getProfile = currentProfile) {
  const delivered = new Map() // profile -> { zone, at }
  return async function report(force = false) {
    const zone = getZone()
    if (!zone) return false
    const profile = getProfile()
    const last = delivered.get(profile)
    if (!force && last && last.zone === zone && now() - last.at < REFRESH_MS) return false
    delivered.delete(profile)
    try {
      await ctx.rest('/client', { method: 'POST', body: { timezone: zone, source: 'desktop' } })
      delivered.set(profile, { zone, at: now() })
      return true
    } catch {
      return false // backend off or unreachable: no record, so the next check retries
    }
  }
}

export default {
  id: 'tick',
  name: 'tick: device time zone',
  defaultEnabled: false,
  register(ctx) {
    const report = makeReporter(ctx)
    void report(true)
    ctx.setInterval(() => void report(), CHECK_MS)
    ctx.addEventListener(globalThis.document ?? new EventTarget(), 'visibilitychange', () => {
      if (globalThis.document?.visibilityState !== 'hidden') void report()
    })
    // ctx.rest is profile-aware: report again when the user switches profile.
    const profile = host.state.profile
    if (profile?.listen) ctx.onDispose(profile.listen(() => void report(true)))
    ctx.register({
      id: 'report-now',
      area: PALETTE_AREA,
      data: {
        id: 'tick.report-zone',
        label: 'tick: send my time zone now',
        keywords: ['tick', 'time', 'zone', 'timezone'],
        run: async () => {
          const ok = await report(true)
          host.notify?.({
            kind: ok ? 'success' : 'error',
            message: ok ? `tick: time zone ${deviceZone()} sent` : 'tick: backend not reachable (is tick enabled?)'
          })
        }
      }
    })
  }
}
