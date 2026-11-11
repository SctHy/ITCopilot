// 对后端、设置和请求代次做快照，过滤切换或重复请求后的旧结果。
import { requestPhase } from './stateMachine.js'

export function createRequestState(settings) {
  let generation = 0
  let sequence = 0
  const latest = new Map()
  return {
    invalidate() { generation += 1 },
    begin(slot) {
      const revision = generation
      const id = ++sequence
      const backend = settings.backend
      const snapshot = { ...settings, endpoints: { ...settings.endpoints } }
      latest.set(slot, id)
      const current = () => revision === generation && latest.get(slot) === id &&
        snapshot.accessToken === settings.accessToken &&
        backend === settings.backend && snapshot.endpoints[backend] === settings.endpoints[backend]
      return {
        backend,
        settings: snapshot,
        get phase() { return requestPhase(current()) },
        isCurrent: () => requestPhase(current()) === 'current'
      }
    }
  }
}
