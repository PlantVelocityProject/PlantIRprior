import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [tailwindcss()],
  define: {'process.env.NODE_ENV': JSON.stringify('production')},
  resolve: {alias: {'@': fileURLToPath(new URL('./src', import.meta.url))}},
  build: {
    // This bundle runs entirely in the browser; server/client boundaries do not apply.
    rolldownOptions: {checks: {moduleLevelDirective: false}},
    outDir: 'assets',
    emptyOutDir: false,
    lib: {
      entry: 'src/main.tsx',
      name: 'PlantIRComponents',
      formats: ['iife'],
      fileName: () => 'ui.js',
      cssFileName: 'ui',
    },
  },
})
