// Minimal stand-in for '@hermes/plugin-sdk' (nanostores-style atoms).
function atom(value) {
  const subs = new Set()
  return {
    get: () => value,
    set: v => { value = v; for (const fn of subs) fn(v) },
    listen: fn => { subs.add(fn); return () => subs.delete(fn) },
    subscribe: fn => { subs.add(fn); fn(value); return () => subs.delete(fn) }
  }
}

export const PALETTE_AREA = 'palette'
export const host = {
  state: { profile: atom('default') },
  notify: () => {}
}
