import { defineConfig } from 'vitepress'
import llmstxt from 'vitepress-plugin-llms'

export default defineConfig({
    vite: {
        plugins: [llmstxt()],
        build: {
            chunkSizeWarningLimit: 1000,
        },
    },

    title: 'Sibyl',
    description: 'Collective Intelligence Runtime - Shared memory and task orchestration for AI agents',
    base: '/sibyl/',

    head: [
        ['meta', { name: 'theme-color', content: '#e135ff' }],
        ['meta', { property: 'og:type', content: 'website' }],
        ['meta', { property: 'og:title', content: 'Sibyl - Collective Intelligence Runtime' }],
        [
            'meta',
            {
                property: 'og:description',
                content:
                    'Give your AI agents persistent memory, semantic search, and collaborative knowledge through a graph-powered runtime.',
            },
        ],
        ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
        ['meta', { name: 'twitter:title', content: 'Sibyl - Collective Intelligence Runtime' }],
        [
            'meta',
            {
                name: 'twitter:description',
                content: 'Persistent memory and task orchestration for AI agents.',
            },
        ],
        ['link', { rel: 'icon', type: 'image/svg+xml', href: '/sibyl/favicon.svg' }],
    ],

    themeConfig: {
        logo: '/sibyl-logo.png',
        siteTitle: false,

        nav: [
            { text: 'Guide', link: '/guide/' },
            { text: 'CLI', link: '/cli/' },
            { text: 'API', link: '/api/' },
            { text: 'Deployment', link: '/deployment/' },
        ],

        sidebar: {
            '/guide/': [
                {
                    text: 'Getting Started',
                    items: [
                        { text: 'Introduction', link: '/guide/' },
                        { text: 'Installation', link: '/guide/installation' },
                        { text: 'Quick Start', link: '/guide/quick-start' },
                    ],
                },
                {
                    text: 'Core Concepts',
                    items: [
                        { text: 'Knowledge Graph', link: '/guide/knowledge-graph' },
                        { text: 'Entity Types', link: '/guide/entity-types' },
                        { text: 'Semantic Search', link: '/guide/semantic-search' },
                        { text: 'Multi-Tenancy', link: '/guide/multi-tenancy' },
                    ],
                },
                {
                    text: 'Workflows',
                    items: [
                        { text: 'Task Management', link: '/guide/task-management' },
                        { text: 'Project Organization', link: '/guide/project-organization' },
                        { text: 'Capturing Knowledge', link: '/guide/capturing-knowledge' },
                        { text: 'External Sources', link: '/guide/sources' },
                    ],
                },
                {
                    text: 'Integrations',
                    items: [
                        { text: 'Claude Code', link: '/guide/claude-code' },
                        { text: 'MCP Configuration', link: '/guide/mcp-configuration' },
                        { text: 'Skills & Hooks', link: '/guide/skills' },
                        { text: 'Agent Collaboration', link: '/guide/agent-collaboration' },
                    ],
                },
            ],
            '/cli/': [
                { text: 'Overview', link: '/cli/' },
                {
                    text: 'Core Commands',
                    items: [
                        { text: 'search', link: '/cli/search' },
                        { text: 'add', link: '/cli/add' },
                        { text: 'explore', link: '/cli/explore' },
                    ],
                },
                {
                    text: 'Task Commands',
                    items: [
                        { text: 'task create', link: '/cli/task-create' },
                        { text: 'task list', link: '/cli/task-list' },
                        { text: 'task lifecycle', link: '/cli/task-lifecycle' },
                    ],
                },
                {
                    text: 'Project Commands',
                    items: [
                        { text: 'project', link: '/cli/project' },
                        { text: 'context', link: '/cli/context' },
                    ],
                },
                {
                    text: 'Entity Commands',
                    items: [
                        { text: 'entity', link: '/cli/entity' },
                        { text: 'epic', link: '/cli/epic' },
                    ],
                },
            ],
            '/api/': [
                { text: 'Overview', link: '/api/' },
                {
                    text: 'MCP Tools',
                    items: [
                        { text: 'search', link: '/api/mcp-search' },
                        { text: 'explore', link: '/api/mcp-explore' },
                        { text: 'add', link: '/api/mcp-add' },
                        { text: 'manage', link: '/api/mcp-manage' },
                    ],
                },
                {
                    text: 'REST Endpoints',
                    items: [
                        { text: 'Entities', link: '/api/rest-entities' },
                        { text: 'Tasks', link: '/api/rest-tasks' },
                        { text: 'Projects', link: '/api/rest-projects' },
                        { text: 'Search', link: '/api/rest-search' },
                    ],
                },
                {
                    text: 'Authentication',
                    items: [
                        { text: 'JWT Auth', link: '/api/auth-jwt' },
                        { text: 'API Keys', link: '/api/auth-api-keys' },
                    ],
                },
            ],
            '/deployment/': [
                { text: 'Overview', link: '/deployment/' },
                {
                    text: 'Local Development',
                    items: [
                        { text: 'Docker Compose', link: '/deployment/docker-compose' },
                        { text: 'Tilt & Minikube', link: '/deployment/tilt-minikube' },
                    ],
                },
                {
                    text: 'Production',
                    items: [
                        { text: 'Kubernetes', link: '/deployment/kubernetes' },
                        { text: 'Helm Chart', link: '/deployment/helm-chart' },
                        { text: 'Environment Variables', link: '/deployment/environment' },
                    ],
                },
                {
                    text: 'Operations',
                    items: [
                        { text: 'Monitoring', link: '/deployment/monitoring' },
                        { text: 'Troubleshooting', link: '/deployment/troubleshooting' },
                    ],
                },
            ],
        },

        socialLinks: [{ icon: 'github', link: 'https://github.com/hyperb1iss/sibyl' }],

        footer: {
            message: 'Released under the MIT License.',
            copyright: 'Copyright © 2024-2025 Stefanie Jane',
        },

        search: {
            provider: 'local',
        },
    },

    markdown: {
        theme: {
            light: 'github-light',
            dark: 'one-dark-pro',
        },
    },

    // Allow localhost links in docs (they reference local dev services)
    ignoreDeadLinks: [
        /^http:\/\/localhost/,
    ],
})
