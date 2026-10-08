import { defineConfig } from 'vitepress'
import llmstxt from 'vitepress-plugin-llms'

// Crawlers need absolute URLs for og:image and og:url; `base` does not apply to head tags.
const SITE_URL = 'https://hyperb1iss.github.io/sibyl/'
const SOCIAL_TITLE = 'Sibyl: Cross-agent memory for AI coding tools'
const SOCIAL_DESCRIPTION =
    'One self-hostable knowledge graph shared across Claude Code, Codex, OpenCode, Cursor, and the agents you build. If it runs a shell command, it speaks Sibyl.'
const SOCIAL_IMAGE = `${SITE_URL}og-image.png`
const SOCIAL_IMAGE_ALT = 'Sibyl: One CLI. One graph. Every AI tool you use, sharing memory.'

export default defineConfig({
    vite: {
        plugins: [llmstxt()],
        build: {
            chunkSizeWarningLimit: 1000,
        },
    },

    title: 'Sibyl',
    description:
        'Cross-agent memory for AI coding tools: one self-hostable knowledge graph shared across Claude Code, Codex, OpenCode, Cursor, and the agents you build.',
    base: '/sibyl/',

    // Internal planning, audits, and strategy stay in-repo but never publish.
    // retrieval-system.md is the one lowercase architecture doc and ships.
    srcExclude: [
        'architecture/[A-Z]*.md',
        'architecture/AUDIT_*/**',
        '_archive/**',
        'research/**',
        'SCREENSHOTS_NEEDED.md',
    ],

    head: [
        ['meta', { name: 'theme-color', content: '#e135ff' }],
        ['meta', { property: 'og:type', content: 'website' }],
        ['meta', { property: 'og:site_name', content: 'Sibyl' }],
        ['meta', { property: 'og:title', content: SOCIAL_TITLE }],
        ['meta', { property: 'og:description', content: SOCIAL_DESCRIPTION }],
        ['meta', { property: 'og:image', content: SOCIAL_IMAGE }],
        ['meta', { property: 'og:image:type', content: 'image/png' }],
        ['meta', { property: 'og:image:width', content: '1200' }],
        ['meta', { property: 'og:image:height', content: '630' }],
        ['meta', { property: 'og:image:alt', content: SOCIAL_IMAGE_ALT }],
        ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
        ['meta', { name: 'twitter:title', content: SOCIAL_TITLE }],
        ['meta', { name: 'twitter:description', content: SOCIAL_DESCRIPTION }],
        ['meta', { name: 'twitter:image', content: SOCIAL_IMAGE }],
        ['meta', { name: 'twitter:image:alt', content: SOCIAL_IMAGE_ALT }],
        ['link', { rel: 'icon', type: 'image/png', href: '/sibyl/favicon.png' }],
    ],

    // og:url has to name the page being shared, so it is set per page rather than in `head`.
    transformPageData(pageData) {
        const path = pageData.relativePath
            .replace(/(^|\/)index\.md$/, '$1')
            .replace(/\.md$/, '.html')
        pageData.frontmatter.head ??= []
        pageData.frontmatter.head.push([
            'meta',
            { property: 'og:url', content: `${SITE_URL}${path}` },
        ])
    },

    themeConfig: {
        logo: '/sibyl-logo.png',
        siteTitle: false,

        nav: [
            { text: 'Guide', link: '/guide/' },
            { text: 'Using Sibyl', link: '/users/cli-setup' },
            { text: 'Self-Hosting & Admin', link: '/admin/installing' },
            { text: 'CLI', link: '/cli/' },
            { text: 'API', link: '/api/' },
            { text: 'Benchmarks', link: '/testing/' },
            { text: 'Deployment', link: '/deployment/' },
        ],

        sidebar: {
            '/users/': [
                {
                    text: 'Using Sibyl',
                    items: [
                        { text: 'CLI Setup', link: '/users/cli-setup' },
                        { text: 'MCP Setup', link: '/users/mcp-setup' },
                        { text: 'Sharing Memory', link: '/users/sharing-memory' },
                        { text: 'Signing In', link: '/users/login' },
                    ],
                },
            ],
            '/admin/': [
                {
                    text: 'Self-Hosting & Admin',
                    items: [
                        { text: 'Installing Sibyl', link: '/admin/installing' },
                        { text: 'Inviting Users', link: '/admin/inviting-users' },
                        { text: 'Audit Log', link: '/admin/audit-log' },
                        { text: 'Backup And Restore', link: '/admin/backup-restore' },
                        { text: 'Release Runbook', link: '/admin/releasing' },
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
                        { text: 'Run Sibyl for Yourself', link: '/guide/self-hosting' },
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
                        { text: 'show', link: '/cli/show' },
                        { text: 'brief', link: '/cli/brief' },
                        { text: 'session', link: '/cli/session' },
                        { text: 'export', link: '/cli/export' },
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
                        { text: 'ingest', link: '/cli/ingest' },
                        { text: 'docs', link: '/cli/docs' },
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
                        { text: 'setup', link: '/cli/setup' },
                        { text: 'init', link: '/cli/init' },
                        { text: 'auth', link: '/cli/auth' },
                        { text: 'org', link: '/cli/org' },
                        { text: 'migrate', link: '/cli/migrate' },
                        { text: 'team', link: '/cli/team' },
                        { text: 'context', link: '/cli/context' },
                        { text: 'doctor', link: '/cli/doctor' },
                        { text: 'service', link: '/cli/service' },
                        { text: 'docker', link: '/cli/docker' },
                        { text: 'local', link: '/cli/local' },
                        { text: 'skill', link: '/cli/skill' },
                        { text: 'update', link: '/cli/update' },
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
                        { text: 'traversal verbs', link: '/api/mcp-traverse' },
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
            '/testing/': [
                { text: 'Overview', link: '/testing/' },
                {
                    text: 'Evaluation',
                    items: [
                        { text: 'LongMemEval-S', link: '/testing/longmemeval' },
                        { text: 'LongMemEval-V2', link: '/testing/longmemeval-v2' },
                        { text: 'AI Memory Landscape', link: '/testing/ai-memory-landscape' },
                        { text: 'Benchmark Methodology', link: '/testing/benchmark-methodology' },
                    ],
                },
                {
                    text: 'Architecture',
                    items: [
                        {
                            text: 'Retrieval System',
                            link: '/architecture/retrieval-system',
                        },
                    ],
                },
            ],
            '/architecture/': [
                {
                    text: 'Architecture',
                    items: [
                        {
                            text: 'Retrieval System',
                            link: '/architecture/retrieval-system',
                        },
                    ],
                },
                {
                    text: 'Benchmarks',
                    items: [
                        { text: 'Overview', link: '/testing/' },
                        { text: 'LongMemEval-S', link: '/testing/longmemeval' },
                        { text: 'AI Memory Landscape', link: '/testing/ai-memory-landscape' },
                        { text: 'Benchmark Methodology', link: '/testing/benchmark-methodology' },
                    ],
                },
            ],
            '/deployment/': [
                { text: 'Overview', link: '/deployment/' },
                {
                    text: 'Local Development',
                    items: [
                        { text: 'Docker Compose', link: '/deployment/docker-compose' },
                    ],
                },
                {
                    text: 'Production',
                    items: [
                        { text: 'Kubernetes', link: '/deployment/kubernetes' },
                        { text: 'Helm Chart', link: '/deployment/helm-chart' },
                        { text: 'Ansible', link: '/deployment/ansible' },
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
            message: 'Released under the Apache-2.0 License.',
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

    // Allow localhost links in docs (they reference local dev services).
    // The architecture/research notes below predate this config; they reference
    // sibling planning docs and the root CLAUDE.md that VitePress can't resolve
    // when building under /sibyl/. Narrow ignores keep the build passing without
    // hiding new dead links elsewhere.
    ignoreDeadLinks: [
        /^http:\/\/localhost/,
        /^\/(Users|home)\//,
        /ROADMAP_2026/,
        /(?:^|\/)(?:rust-port|memory-sota)(?:\/|$)/,
        /CLAUDE(\.md)?$/,
        /^\.\/\.$/,
    ],
})
