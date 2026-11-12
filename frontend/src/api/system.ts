// Service-level endpoints: health, identity, monitoring and loaded skills.
import { jsonBody, requestFor } from './http.ts'
import type { Settings } from '../lib/types.ts'

export const requestHealth = (type: string, settings: Settings) => requestFor(type, settings, '/health')
export const requestAuthConfig = (type: string, settings: Settings) => requestFor(type, settings, '/auth/config')
export const requestMe = (type: string, settings: Settings) => requestFor(type, settings, '/auth/me')
export const requestDevToken = (type: string, settings: Settings, username: string) =>
  requestFor<{ access_token: string }>(type, settings, '/auth/dev-token', jsonBody({ username }))
export const requestMonitor = (type: string, settings: Settings) => requestFor(type, settings, '/monitor')
export const requestSkills = (type: string, settings: Settings) => requestFor(type, settings, '/skills')
export const reloadSkills = (type: string, settings: Settings) => requestFor(type, settings, '/skills/reload', { method: 'POST' })
