import { QueryClient } from '@tanstack/react-query'

/** One cache per authenticated connection. Never persist credentials or private server data. */
export function createServerQueryClient() {
  return new QueryClient({ defaultOptions: {
    queries: { staleTime: 15_000, gcTime: 5 * 60_000, retry: false, refetchOnWindowFocus: true },
    // Business writes have their own idempotency and approval semantics.
    mutations: { retry: false },
  } })
}
