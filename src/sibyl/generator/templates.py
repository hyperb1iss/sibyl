"""Template-based generators - fast, no LLM required."""

from datetime import timedelta

from sibyl.generator.base import BaseGenerator, GeneratorResult
from sibyl.generator.config import GeneratorConfig
from sibyl.models.entities import Entity, EntityType

# Template data for realistic generation
PATTERN_TEMPLATES = {
    "API Design": [
        ("RESTful Resource Naming", "Use plural nouns for resource endpoints, kebab-case for multi-word resources"),
        ("API Versioning Strategy", "Prefix API paths with version number (v1, v2) for backward compatibility"),
        ("Error Response Format", "Return structured error objects with code, message, and optional details"),
        ("Pagination Pattern", "Use cursor-based pagination for large collections, include total count"),
        ("Rate Limiting Headers", "Include X-RateLimit-* headers to communicate limits to clients"),
    ],
    "Authentication": [
        ("JWT Token Structure", "Use short-lived access tokens with longer refresh tokens"),
        ("OAuth2 PKCE Flow", "Implement PKCE for public clients to prevent authorization code interception"),
        ("Session Management", "Store sessions server-side with secure, httpOnly cookies"),
        ("Multi-factor Authentication", "Require MFA for sensitive operations, support TOTP and WebAuthn"),
        ("API Key Rotation", "Implement automatic key rotation with grace period for old keys"),
    ],
    "Database": [
        ("Connection Pooling", "Use connection pools with appropriate min/max sizes for workload"),
        ("Query Optimization", "Add indices for frequently queried columns, use EXPLAIN to verify"),
        ("Migration Strategy", "Use versioned migrations with up/down scripts, never modify existing"),
        ("Soft Delete Pattern", "Add deleted_at column instead of hard deletes for audit trail"),
        ("Denormalization Strategy", "Denormalize for read-heavy paths, maintain via triggers or events"),
    ],
    "Testing": [
        ("Test Pyramid", "More unit tests than integration, more integration than E2E"),
        ("Fixture Factories", "Use factories (Factory Boy, Fishery) instead of static fixtures"),
        ("Mock Boundaries", "Mock at system boundaries (HTTP, DB), not internal interfaces"),
        ("Snapshot Testing", "Use snapshots for complex outputs, review diffs carefully"),
        ("Property-Based Testing", "Use hypothesis/fast-check for edge case discovery"),
    ],
    "CI/CD": [
        ("Pipeline Stages", "Separate build, test, security scan, and deploy stages"),
        ("Artifact Caching", "Cache dependencies and build artifacts between runs"),
        ("Environment Parity", "Match production environment in CI as closely as possible"),
        ("Deployment Gates", "Require approval gates for production deployments"),
        ("Rollback Strategy", "Maintain ability to rollback within 5 minutes"),
    ],
    "Observability": [
        ("Structured Logging", "Use JSON logs with consistent field names across services"),
        ("Distributed Tracing", "Propagate trace IDs across service boundaries"),
        ("Metric Naming", "Use namespace_subsystem_metric_unit naming convention"),
        ("Alert Thresholds", "Set alerts based on SLOs, not arbitrary thresholds"),
        ("Dashboard Layout", "Golden signals (latency, traffic, errors, saturation) first"),
    ],
    "Security": [
        ("Input Validation", "Validate and sanitize all user input at system boundaries"),
        ("Secret Management", "Use secret managers (Vault, AWS Secrets), never commit secrets"),
        ("CORS Configuration", "Restrict CORS origins to known domains, avoid wildcards"),
        ("Content Security Policy", "Implement strict CSP headers to prevent XSS"),
        ("Dependency Scanning", "Scan dependencies for vulnerabilities in CI pipeline"),
    ],
    "Performance": [
        ("Caching Strategy", "Cache at multiple levels: CDN, application, database query"),
        ("Lazy Loading", "Defer loading of non-critical resources until needed"),
        ("Connection Reuse", "Reuse HTTP connections with keep-alive and connection pooling"),
        ("Batch Operations", "Batch multiple operations to reduce round trips"),
        ("Async Processing", "Move heavy operations to background queues"),
    ],
}

TASK_TITLE_TEMPLATES = [
    "Implement {feature} for {component}",
    "Add {feature} support to {component}",
    "Refactor {component} to use {pattern}",
    "Fix {issue} in {component}",
    "Optimize {component} performance",
    "Add tests for {component}",
    "Document {component} API",
    "Migrate {component} to {technology}",
    "Set up {feature} infrastructure",
    "Configure {feature} for {environment}",
    "Review and improve {component}",
    "Integrate {service} with {component}",
]

TASK_FEATURES = [
    "authentication",
    "authorization",
    "caching",
    "logging",
    "monitoring",
    "rate limiting",
    "pagination",
    "search",
    "filtering",
    "sorting",
    "validation",
    "error handling",
]

TASK_COMPONENTS = [
    "user service",
    "API gateway",
    "auth module",
    "database layer",
    "cache layer",
    "message queue",
    "background worker",
    "frontend app",
    "admin dashboard",
    "CLI tool",
]

EPISODE_LEARNING_TEMPLATES = [
    "Discovered that {observation} when {context}. Solution: {solution}.",
    "After debugging {issue}, found that {root_cause}. Fixed by {fix}.",
    "Performance improved by {improvement} after {change}.",
    "Learned that {insight} is critical for {domain}.",
    "Refactored {component} because {reason}. Result: {outcome}.",
]

TEAM_NAMES = [
    "Platform",
    "Infrastructure",
    "Core API",
    "Frontend",
    "Mobile",
    "Data",
    "Security",
    "DevOps",
    "Growth",
    "Payments",
]

PROJECT_PREFIXES = [
    "Project",
    "Initiative",
    "Epic",
    "Sprint",
    "Milestone",
]


class TemplateGenerator(BaseGenerator):
    """Fast template-based generator without LLM calls."""

    def __init__(self, config: GeneratorConfig) -> None:
        super().__init__(config)
        self._entity_counter = 0

    def next_id(self, prefix: str) -> str:
        """Generate sequential ID for deterministic ordering."""
        self._entity_counter += 1
        return f"{prefix}_{self._entity_counter:06d}"

    def pick(self, items: list) -> str:
        """Pick a random item from a list."""
        return self.rng.choice(items)

    def pick_n(self, items: list, n: int) -> list:
        """Pick n random items from a list."""
        return self.rng.sample(items, min(n, len(items)))

    async def generate(self) -> GeneratorResult:
        """Generate all entities based on config."""
        import time

        start = time.time()
        result = GeneratorResult()

        # Generate projects first
        projects = await self.generate_batch(self.config.projects, "project")
        result.entities.extend(projects)

        # Generate patterns
        patterns = await self.generate_batch(self.config.patterns, "pattern")
        result.entities.extend(patterns)

        # Generate rules
        rules = await self.generate_batch(self.config.rules, "rule")
        result.entities.extend(rules)

        # Generate templates
        templates = await self.generate_batch(self.config.templates, "template")
        result.entities.extend(templates)

        # Generate tasks for each project
        for project in projects:
            tasks = await self._generate_tasks_for_project(project, self.config.tasks_per_project)
            result.entities.extend(tasks)

        # Generate episodes
        episodes = await self.generate_batch(self.config.episodes, "episode")
        result.entities.extend(episodes)

        result.duration_seconds = time.time() - start
        return result

    async def generate_batch(self, count: int, entity_type: str) -> list[Entity]:
        """Generate a batch of entities of a specific type."""
        generators = {
            "project": self.generate_project,
            "task": self.generate_task,
            "pattern": self.generate_pattern,
            "rule": self.generate_rule,
            "template": self.generate_template,
            "episode": self.generate_episode,
        }

        generator = generators.get(entity_type)
        if not generator:
            return []

        return [generator() for _ in range(count)]

    async def _generate_tasks_for_project(self, project: Entity, count: int) -> list[Entity]:
        """Generate tasks belonging to a project."""
        tasks = []
        for _ in range(count):
            task = self.generate_task(project_id=project.id)
            tasks.append(task)
        return tasks

    def generate_project(self) -> Entity:
        """Generate a project entity."""
        team = self.pick(TEAM_NAMES)
        prefix = self.pick(PROJECT_PREFIXES)
        language = self.pick(self.config.languages)
        framework = self.pick(self.config.frameworks)

        name = f"{prefix}: {team} {framework} Initiative"
        description = f"A {language}/{framework} project for the {team} team focusing on core infrastructure improvements."

        return Entity(
            id=self.next_id("proj"),
            name=name,
            entity_type=EntityType.PROJECT,
            description=description,
            content=f"# {name}\n\n{description}\n\n## Tech Stack\n- {language}\n- {framework}",
            metadata=self.mark_generated({
                "team": team,
                "language": language,
                "framework": framework,
                "status": self.pick(["active", "planning", "completed"]),
            }),
            created_at=self.now() - timedelta(days=self.rng.randint(1, 90)),
        )

    def generate_task(self, project_id: str | None = None) -> Entity:
        """Generate a task entity."""
        template = self.pick(TASK_TITLE_TEMPLATES)
        feature = self.pick(TASK_FEATURES)
        component = self.pick(TASK_COMPONENTS)

        title = template.format(
            feature=feature,
            component=component,
            pattern=self.pick(list(PATTERN_TEMPLATES.keys())),
            issue="edge case handling",
            technology=self.pick(self.config.frameworks),
            service=self.pick(["Redis", "PostgreSQL", "Kafka", "ElasticSearch"]),
            environment=self.pick(["staging", "production", "development"]),
        )

        status = self.pick(["backlog", "todo", "doing", "review", "done"])
        priority = self.pick(["critical", "high", "medium", "low"])

        return Entity(
            id=self.next_id("task"),
            name=title,
            entity_type=EntityType.TASK,
            description=f"Task to {title.lower()}. Part of ongoing {feature} improvements.",
            metadata=self.mark_generated({
                "status": status,
                "priority": priority,
                "project_id": project_id,
                "feature": feature,
                "component": component,
                "assignees": self.pick_n(["alice", "bob", "charlie", "diana", "eve"], self.rng.randint(1, 2)),
                "estimated_hours": self.rng.randint(1, 16),
            }),
            created_at=self.now() - timedelta(days=self.rng.randint(0, 30)),
        )

    def _generate_pattern(self) -> Entity:
        """Generate a pattern entity."""
        domain = self.pick(list(PATTERN_TEMPLATES.keys()))
        name, description = self.pick(PATTERN_TEMPLATES[domain])
        language = self.pick(self.config.languages)

        content = f"""# {name}

## Overview
{description}

## Applies To
- Language: {language}
- Domain: {domain}

## Implementation Notes
This pattern should be applied consistently across the codebase.
Consider the trade-offs before applying in performance-critical paths.

## Examples
See related code examples in the repository.
"""

        return Entity(
            id=self.next_id("pat"),
            name=name,
            entity_type=EntityType.PATTERN,
            description=description,
            content=content,
            metadata=self.mark_generated({
                "domain": domain,
                "language": language,
                "confidence": self.rng.uniform(0.7, 1.0),
            }),
            created_at=self.now() - timedelta(days=self.rng.randint(30, 365)),
        )

    def _generate_rule(self) -> Entity:
        """Generate a rule entity."""
        domain = self.pick(list(PATTERN_TEMPLATES.keys()))
        language = self.pick(self.config.languages)

        rules = [
            ("Always validate input", "Input must be validated before processing"),
            ("Use typed exceptions", "Prefer typed exceptions over generic Exception"),
            ("Document public APIs", "All public methods must have docstrings"),
            ("Limit function length", "Functions should not exceed 50 lines"),
            ("Prefer composition", "Favor composition over inheritance"),
            ("Immutable by default", "Prefer immutable data structures"),
            ("Fail fast", "Validate early and fail with clear errors"),
            ("Single responsibility", "Each module should have one reason to change"),
        ]

        name, description = self.pick(rules)

        return Entity(
            id=self.next_id("rule"),
            name=f"{name} ({language})",
            entity_type=EntityType.RULE,
            description=description,
            content=f"# {name}\n\n{description}\n\nApplies to: {language} code in {domain} domain.",
            metadata=self.mark_generated({
                "domain": domain,
                "language": language,
                "severity": self.pick(["error", "warning", "info"]),
                "auto_fixable": self.rng.random() > 0.7,
            }),
            created_at=self.now() - timedelta(days=self.rng.randint(30, 180)),
        )

    def _generate_template(self) -> Entity:
        """Generate a template entity."""
        templates = [
            ("API Endpoint Template", "Standard template for RESTful API endpoints"),
            ("Database Migration", "Template for database schema migrations"),
            ("Test File Structure", "Standard test file with fixtures and assertions"),
            ("Service Class", "Template for service layer classes"),
            ("Configuration Module", "Environment-based configuration template"),
            ("CLI Command", "Template for CLI subcommands"),
            ("Background Job", "Template for async background workers"),
            ("GraphQL Resolver", "Template for GraphQL query/mutation resolvers"),
        ]

        name, description = self.pick(templates)
        language = self.pick(self.config.languages)
        framework = self.pick(self.config.frameworks)

        return Entity(
            id=self.next_id("tmpl"),
            name=f"{name} ({framework})",
            entity_type=EntityType.TEMPLATE,
            description=description,
            content=f"# {name}\n\n{description}\n\nFor use with {framework} ({language}).",
            metadata=self.mark_generated({
                "language": language,
                "framework": framework,
                "category": self.pick(["backend", "frontend", "infrastructure", "testing"]),
            }),
            created_at=self.now() - timedelta(days=self.rng.randint(60, 365)),
        )

    def _generate_episode(self) -> Entity:
        """Generate an episode (learning/experience) entity."""
        template = self.pick(EPISODE_LEARNING_TEMPLATES)
        domain = self.pick(list(PATTERN_TEMPLATES.keys()))

        # Fill in template
        content = template.format(
            observation=self.pick([
                "caching significantly reduced latency",
                "connection pooling was exhausted",
                "memory usage spiked during peak hours",
                "the retry logic caused cascading failures",
            ]),
            context=self.pick([
                "deploying to production",
                "running load tests",
                "debugging a customer issue",
                "reviewing the logs",
            ]),
            solution=self.pick([
                "implement circuit breaker pattern",
                "add proper timeout handling",
                "increase resource limits",
                "refactor to use async operations",
            ]),
            issue=self.pick([
                "slow response times",
                "memory leaks",
                "race conditions",
                "deadlocks",
            ]),
            root_cause=self.pick([
                "N+1 query problem",
                "missing index",
                "synchronous I/O blocking",
                "improper error handling",
            ]),
            fix=self.pick([
                "adding batch queries",
                "creating composite index",
                "switching to async/await",
                "implementing proper retry logic",
            ]),
            improvement=self.pick(["50%", "3x", "10x", "80%"]),
            change=self.pick([
                "implementing caching",
                "optimizing queries",
                "adding connection pooling",
                "enabling compression",
            ]),
            insight=self.pick([
                "observability",
                "proper error handling",
                "performance testing",
                "incremental rollouts",
            ]),
            component=self.pick(TASK_COMPONENTS),
            reason=self.pick([
                "code had become unmaintainable",
                "performance was degrading",
                "new requirements emerged",
                "security audit findings",
            ]),
            outcome=self.pick([
                "cleaner architecture",
                "better performance",
                "improved maintainability",
                "reduced complexity",
            ]),
        )

        return Entity(
            id=self.next_id("ep"),
            name=f"Episode: {domain} Learning",
            entity_type=EntityType.EPISODE,
            description=content[:200],
            content=content,
            metadata=self.mark_generated({
                "domain": domain,
                "impact": self.pick(["high", "medium", "low"]),
                "source": self.pick(["production_incident", "code_review", "design_discussion", "retrospective"]),
            }),
            created_at=self.now() - timedelta(days=self.rng.randint(1, 60)),
        )
