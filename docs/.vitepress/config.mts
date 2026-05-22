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
    description: 'Knowledge graph, semantic search, and task workflow for durable project memory',
    base: '/sibyl/',

    head: [
        ['meta', { name: 'theme-color', content: '#e135ff' }],
        ['meta', { property: 'og:type', content: 'website' }],
        ['meta', { property: 'og:title', content: 'Sibyl - Knowledge Graph + Task Workflow' }],
        [
            'meta',
            {
                property: 'og:description',
                content:
                    'Give your projects durable memory with a knowledge graph, semantic search, and task workflow.',
            },
        ],
        ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
        ['meta', { name: 'twitter:title', content: 'Sibyl - Knowledge Graph + Task Workflow' }],
        [
            'meta',
            {
                name: 'twitter:description',
                content: 'Durable project memory with semantic search and task workflow.',
            },
        ],
        ['link', { rel: 'icon', type: 'image/svg+xml', href: '/sibyl/favicon.svg' }],
    ],

    themeConfig: {
        logo: '/sibyl-logo.png',
        siteTitle: false,

        nav: [
            { text: 'Guide', link: '/guide/' },
            { text: 'Users', link: '/users/login' },
            { text: 'Admins', link: '/admin/installing' },
            { text: 'CLI', link: '/cli/' },
            { text: 'API', link: '/api/' },
            { text: 'Deployment', link: '/deployment/' },
        ],

        sidebar: {
            '/users/': [
                {
                    text: 'Using Sibyl',
                    items: [
                        { text: 'Enterprise Sign-In', link: '/users/login' },
                        { text: 'CLI Setup', link: '/users/cli-setup' },
                        { text: 'MCP Setup', link: '/users/mcp-setup' },
                        { text: 'Sharing Memory', link: '/users/sharing-memory' },
                    ],
                },
            ],
            '/admin/': [
                {
                    text: 'Enterprise Administration',
                    items: [
                        { text: 'Installing Sibyl', link: '/admin/installing' },
                        { text: 'Inviting Users', link: '/admin/inviting-users' },
                        { text: 'Audit Log', link: '/admin/audit-log' },
                        { text: 'Backup And Restore', link: '/admin/backup-restore' },
                        { text: 'Break-Glass Access', link: '/admin/break-glass' },
                    ],
                },
            ],
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
                    text: 'Working with Agents',
                    items: [
                        { text: 'Overview', link: '/guide/working-with-agents' },
                        { text: 'Setting Up Prompts', link: '/guide/setting-up-prompts' },
                        { text: 'Agents & MCP', link: '/guide/claude-code' },
                        { text: 'MCP Configuration', link: '/guide/mcp-configuration' },
                        { text: 'Skills & Hooks', link: '/guide/skills' },
                        { text: 'Agent Collaboration', link: '/guide/agent-collaboration' },
                        { text: 'Knowledge Repository', link: '/guide/knowledge-repository' },
                    ],
                },
                {
                    text: 'Core Concepts',
                    items: [
                        { text: 'Knowledge Graph', link: '/guide/knowledge-graph' },
                        { text: 'Entity Types', link: '/guide/entity-types' },
                        { text: 'The Memory Loop', link: '/guide/memory-loop' },
                        { text: 'Semantic Search', link: '/guide/semantic-search' },
                        { text: 'Multi-Tenancy', link: '/guide/multi-tenancy' },
                    ],
                },
                {
                    text: 'Storage',
                    items: [
                        { text: 'Storage Modes', link: '/guide/storage-modes' },
                        { text: 'Why SurrealDB', link: '/guide/why-surreal' },
                        { text: 'Migrating from FalkorDB', link: '/guide/migrating-from-falkor' },
                        {
                            text: 'SurrealDB Release Notes',
                            link: '/guide/surrealdb-migration-release-notes',
                        },
                    ],
                },
                {
                    text: 'Workflows',
                    items: [
                        { text: 'Task Management', link: '/guide/task-management' },
                        { text: 'Project Organization', link: '/guide/project-organization' },
                        { text: 'Capturing Knowledge', link: '/guide/capturing-knowledge' },
                        { text: 'Synthesis', link: '/guide/synthesis' },
                        { text: 'Memory Workspace', link: '/guide/memory-workspace' },
                        { text: 'External Sources', link: '/guide/sources' },
                    ],
                },
            ],
            '/cli/': [
                { text: 'Overview', link: '/cli/' },
                {
                    text: 'Memory Loop',
                    items: [
                        { text: 'recall', link: '/cli/recall' },
                        { text: 'remember', link: '/cli/remember' },
                        { text: 'reflect', link: '/cli/reflect' },
                        { text: 'capture', link: '/cli/capture' },
                        { text: 'search', link: '/cli/search' },
                        { text: 'add', link: '/cli/add' },
                        { text: 'session', link: '/cli/session' },
                        { text: 'archive', link: '/cli/archive' },
                    ],
                },
                {
                    text: 'Work Tracking',
                    items: [
                        { text: 'task create', link: '/cli/task-create' },
                        { text: 'task list', link: '/cli/task-list' },
                        { text: 'task lifecycle', link: '/cli/task-lifecycle' },
                        { text: 'epic', link: '/cli/epic' },
                        { text: 'project', link: '/cli/project' },
                        { text: 'entity', link: '/cli/entity' },
                        { text: 'explore', link: '/cli/explore' },
                    ],
                },
                {
                    text: 'Sources & Synthesis',
                    items: [
                        { text: 'crawl', link: '/cli/crawl' },
                        { text: 'synthesis', link: '/cli/synthesis' },
                    ],
                },
                {
                    text: 'Memory Governance',
                    items: [
                        { text: 'memory', link: '/cli/memory' },
                        { text: 'pending-writes', link: '/cli/pending-writes' },
                    ],
                },
                {
                    text: 'System',
                    items: [
                        { text: 'auth', link: '/cli/auth' },
                        { text: 'org', link: '/cli/org' },
                        { text: 'context', link: '/cli/context' },
                    ],
                },
            ],
            '/api/': [
                { text: 'Overview', link: '/api/' },
                {
                    text: 'MCP Tools',
                    items: [
                        { text: 'search', link: '/api/mcp-search' },
                        { text: 'context', link: '/api/mcp-context' },
                        { text: 'explore', link: '/api/mcp-explore' },
                        { text: 'add', link: '/api/mcp-add' },
                        { text: 'remember', link: '/api/mcp-remember' },
                        { text: 'reflect', link: '/api/mcp-reflect' },
                        { text: 'synthesis', link: '/api/mcp-synthesis' },
                        { text: 'manage', link: '/api/mcp-manage' },
                        { text: 'logs', link: '/api/mcp-logs' },
                    ],
                },
                {
                    text: 'REST Endpoints',
                    items: [
                        { text: 'Entities', link: '/api/rest-entities' },
                        { text: 'Tasks', link: '/api/rest-tasks' },
                        { text: 'Projects', link: '/api/rest-projects' },
                        { text: 'Search', link: '/api/rest-search' },
                        { text: 'Memory', link: '/api/rest-memory' },
                        { text: 'Synthesis', link: '/api/rest-synthesis' },
                    ],
                },
                {
                    text: 'Authentication',
                    items: [
                        { text: 'JWT Auth', link: '/api/auth-jwt' },
                        { text: 'API Keys', link: '/api/auth-api-keys' },
                        { text: 'Authorization', link: '/api/auth-authorization' },
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
            message: 'Released under the AGPL-3.0 License.',
            copyright: 'Copyright © 2024-2026 Stefanie Jane',
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
        /^\/(Users|home)\//,
    ],
})
