import { createRootRoute, createRoute, createRouter, redirect, Link, type RouterHistory } from '@tanstack/react-router'
import { App } from './App'
import { validateAppSearch, viewPaths } from './lib/navigation'

const root = createRootRoute({
  component: App,
  validateSearch: validateAppSearch,
  notFoundComponent: () => <div className="p-8"><h1>页面不存在</h1><Link to="/chat" className="underline">返回对话</Link></div>,
})
const index = createRoute({
  getParentRoute: () => root,
  path: '/',
  beforeLoad: ({ search }) => {
    // Leave an OIDC callback on its exact redirect URI until its code has been exchanged.
    if (!search.code && !search.error) throw redirect({ to: '/chat', search: {}, replace: true })
  },
})
const pages = Object.values(viewPaths).map(path => createRoute({ getParentRoute: () => root, path }))
const routeTree = root.addChildren([index, ...pages])
export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history, defaultPreload: 'intent' })
}
export const router = createAppRouter()
declare module '@tanstack/react-router' { interface Register { router: typeof router } }
