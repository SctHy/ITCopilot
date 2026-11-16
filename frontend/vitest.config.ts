import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config.ts'

export default mergeConfig(viteConfig, defineConfig({
  test: {
    restoreMocks: true,
    unstubGlobals: true,
    projects: [
      { extends: true, test: { name: 'unit', environment: 'node', include: ['tests/*.test.{js,ts}'] } },
      { extends: true, test: { name: 'ui', environment: 'jsdom', include: ['tests/ui/**/*.test.tsx'], setupFiles: ['tests/ui/setup.ts'] } },
    ],
  },
}))
