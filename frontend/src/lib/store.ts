// Framework-agnostic mutable state. Controllers mutate plain-looking objects in place (as with Vue's
// `reactive`); every write anywhere inside the tree notifies the root's subscribers. React subscribes
// through `useStore` (see hooks/useStore.ts). Only plain objects and arrays are wrapped, so Files,
// Blobs and Dates pass through untouched.

type Listener = () => void

interface Root {
  version: number
  listeners: Set<Listener>
  proxies: WeakMap<object, object>
}

const ROOT = Symbol('store.root')
const RAW = Symbol('store.raw')

function isPlain(value: unknown): value is object {
  if (value === null || typeof value !== 'object') return false
  if (Array.isArray(value)) return true
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

function toRaw<T>(value: T): T {
  return (value && typeof value === 'object' && (value as any)[RAW]) || value
}

function notify(root: Root) {
  root.version++
  for (const listener of [...root.listeners]) listener()
}

function wrap<T extends object>(target: T, root: Root): T {
  const cached = root.proxies.get(target)
  if (cached) return cached as T
  const proxy = new Proxy(target, {
    get(obj, key, receiver) {
      if (key === ROOT) return root
      if (key === RAW) return obj
      const value = Reflect.get(obj, key, receiver)
      return isPlain(value) ? wrap(value, root) : value
    },
    set(obj, key, value, receiver) {
      const raw = toRaw(value)
      const had = Object.prototype.hasOwnProperty.call(obj, key)
      const previous = (obj as any)[key]
      const ok = Reflect.set(obj, key, raw, receiver)
      if (!had || previous !== raw || (Array.isArray(obj) && key === 'length')) notify(root)
      return ok
    },
    deleteProperty(obj, key) {
      const had = Object.prototype.hasOwnProperty.call(obj, key)
      const ok = Reflect.deleteProperty(obj, key)
      if (had) notify(root)
      return ok
    }
  })
  root.proxies.set(target, proxy)
  return proxy
}

/** Make `initial` observable. Nested plain objects/arrays become observable on read. */
export function reactive<T extends object>(initial: T): T {
  if ((initial as any)[ROOT]) return initial
  return wrap(toRaw(initial), { version: 0, listeners: new Set(), proxies: new WeakMap() })
}

function rootOf(state: object): Root {
  const root = (state as any)[ROOT] as Root | undefined
  if (!root) throw new Error('subscribe() expects an object created by reactive()')
  return root
}

export function subscribe(state: object, listener: Listener): () => void {
  const root = rootOf(state)
  root.listeners.add(listener)
  return () => { root.listeners.delete(listener) }
}

/** Changes on every write inside the tree; used as the React snapshot. */
export function version(state: object): number {
  return rootOf(state).version
}
