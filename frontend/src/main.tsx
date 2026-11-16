import { createRoot } from 'react-dom/client'
import { RouterProvider } from '@tanstack/react-router'
import { router } from './router'
import './styles.css'

// No StrictMode: controllers own pollers and late-result fences that are created once per mount.
createRoot(document.getElementById('app')!).render(<RouterProvider router={router} />)
