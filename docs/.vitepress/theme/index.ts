import type { Theme } from 'vitepress'
import DefaultTheme from 'vitepress/theme'
import './silkcircuit.css'

declare global {
    interface Window {
        renderMermaidNow?: () => Promise<void>
    }
}

export default {
    extends: DefaultTheme,
    setup() {
        // Client-side Mermaid rendering with SilkCircuit theming.
        // VitePress emits ```mermaid fences as static code blocks; we convert
        // them to live SVG diagrams and re-theme them when the color mode flips.
        if (typeof window === 'undefined') return

        const isDarkMode = () =>
            document.documentElement.classList.contains('dark') ||
            document.documentElement.getAttribute('data-theme') === 'dark'

        // SilkCircuit Dawn palette (light mode): white nodes raised on a
        // lavender card, electric-purple borders, teal flow lines.
        const lightThemeVariables = {
            darkMode: false,
            background: '#f1ecff',
            primaryColor: '#ffffff',
            mainBkg: '#ffffff',
            primaryBorderColor: '#7e2bd5',
            nodeBorder: '#7e2bd5',
            primaryTextColor: '#2b2540',
            nodeTextColor: '#2b2540',
            lineColor: '#007f8e',
            secondaryColor: '#efeaff',
            tertiaryColor: '#faf8ff',
            clusterBkg: 'rgba(126, 43, 213, 0.06)',
            clusterBorder: '#9e4df3',
            edgeLabelBackground: '#f1ecff',
            titleColor: '#7e2bd5',
            fontSize: '14px',
            fontFamily: 'JetBrains Mono, Fira Code, SF Mono, monospace',
        }

        // SilkCircuit Neon palette (dark mode): deep purple-tinted nodes
        // raised on an elevated card, electric-purple borders, neon-cyan flow.
        const darkThemeVariables = {
            darkMode: true,
            background: '#1e1e28',
            primaryColor: '#2d283c',
            mainBkg: '#2d283c',
            primaryBorderColor: '#e135ff',
            nodeBorder: '#e135ff',
            primaryTextColor: '#f8f8f2',
            nodeTextColor: '#f8f8f2',
            lineColor: '#80ffea',
            secondaryColor: '#252531',
            tertiaryColor: '#1e1e28',
            clusterBkg: 'rgba(225, 53, 255, 0.07)',
            clusterBorder: '#bd93f9',
            edgeLabelBackground: '#1e1e28',
            titleColor: '#80ffea',
            fontSize: '14px',
            fontFamily: 'JetBrains Mono, Fira Code, SF Mono, monospace',
        }

        const getMermaidConfig = () => ({
            startOnLoad: false,
            theme: 'base' as const,
            themeVariables: isDarkMode() ? darkThemeVariables : lightThemeVariables,
            securityLevel: 'loose' as const,
            flowchart: {
                htmlLabels: true,
                curve: 'basis',
            },
        })

        let mermaidLoadPromise: Promise<any> | null = null
        const ensureMermaid = async () => {
            if (!mermaidLoadPromise) {
                // A plain dynamic import lets Vite bundle mermaid as a lazy
                // chunk. A bare deep path behind @vite-ignore reached the
                // browser unresolved, so no diagram ever rendered.
                mermaidLoadPromise = import('mermaid').then((mod) => mod.default ?? mod)
            }
            return mermaidLoadPromise
        }

        const convertCodeFences = () => {
            let converted = 0
            const wrappers = Array.from(
                document.querySelectorAll<HTMLDivElement>('div.language-mermaid')
            )
            for (const wrap of wrappers) {
                const code = wrap.querySelector('code')
                const text = (code?.textContent ?? wrap.textContent ?? '').trim()
                if (!text) continue
                const container = document.createElement('div')
                container.className = 'mermaid-diagram'
                container.dataset.mermaidSource = text
                container.textContent = text
                wrap.replaceWith(container)
                converted++
            }

            const pres = Array.from(document.querySelectorAll<HTMLPreElement>('pre'))
            for (const pre of pres) {
                const code = pre.querySelector('code')
                const isMermaid =
                    pre.className.includes('language-mermaid') ||
                    code?.className.includes('language-mermaid')
                if (!isMermaid) continue
                const text = (code?.textContent ?? pre.textContent ?? '').trim()
                if (!text) continue
                const container = document.createElement('div')
                container.className = 'mermaid-diagram'
                container.dataset.mermaidSource = text
                container.textContent = text
                pre.replaceWith(container)
                converted++
            }

            return converted
        }

        const resetExistingDiagrams = () => {
            let reset = 0
            document.querySelectorAll<HTMLElement>('.mermaid-diagram').forEach((diagram) => {
                const source = diagram.dataset.mermaidSource
                if (!source) return
                diagram.textContent = source
                reset++
            })
            return reset
        }

        const addZoomListeners = () => {
            setTimeout(() => {
                document.querySelectorAll('.mermaid-diagram').forEach((diagram) => {
                    const svg = diagram.querySelector('svg')
                    if (!svg || svg.dataset.zoomEnabled) return

                    svg.dataset.zoomEnabled = 'true'
                    svg.style.cursor = 'zoom-in'

                    svg.addEventListener('click', () => {
                        const modal = document.createElement('div')
                        modal.className = 'mermaid-zoom-modal active'
                        modal.appendChild(svg.cloneNode(true))
                        modal.addEventListener('click', () => modal.remove())

                        const handleEscape = (e: KeyboardEvent) => {
                            if (e.key === 'Escape') {
                                modal.remove()
                                document.removeEventListener('keydown', handleEscape)
                            }
                        }
                        document.addEventListener('keydown', handleEscape)
                        document.body.appendChild(modal)
                    })
                })
            }, 100)
        }

        const renderMermaid = async (force = false) => {
            const newlyConverted = convertCodeFences()
            const resetCount = force ? resetExistingDiagrams() : 0
            if (newlyConverted === 0 && resetCount === 0) return
            try {
                const mermaid = await ensureMermaid()
                mermaid.initialize(getMermaidConfig())
                await mermaid.run({ querySelector: '.mermaid-diagram' })
                addZoomListeners()
            } catch (err) {
                console.warn('Mermaid failed to render:', err)
            }
        }

        // Debug helper: call window.renderMermaidNow() from devtools.
        window.renderMermaidNow = () => renderMermaid(true)

        // Re-theme diagrams when the color mode changes, debounced so a rapid
        // toggle does not trigger overlapping mermaid.run() passes.
        const scheduleThemeRender = (() => {
            let timer: number | null = null
            return () => {
                if (timer) window.clearTimeout(timer)
                timer = window.setTimeout(() => {
                    timer = null
                    renderMermaid(true)
                }, 120)
            }
        })()

        const themeObserver = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                if (mutation.type === 'attributes') {
                    scheduleThemeRender()
                    break
                }
            }
        })
        themeObserver.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['class', 'data-theme'],
        })

        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
        if (typeof mediaQuery.addEventListener === 'function') {
            mediaQuery.addEventListener('change', scheduleThemeRender)
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => {
                setTimeout(renderMermaid, 100)
            })
        } else {
            setTimeout(renderMermaid, 100)
        }

        // VitePress SPA navigation swaps page content without a reload.
        window.addEventListener('vitepress:after-route-changed', () => {
            setTimeout(renderMermaid, 100)
        })
    },
} satisfies Theme
