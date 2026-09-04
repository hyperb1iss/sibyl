"""Curated, fictional content for Sibyl's public screenshots."""

from __future__ import annotations

from dataclasses import dataclass

SHOWCASE_TAG = "sibyl-showcase"


@dataclass(frozen=True)
class ProjectFixture:
    key: str
    name: str
    description: str
    languages: tuple[str, ...]
    technologies: tuple[str, ...]


@dataclass(frozen=True)
class TaskFixture:
    project: str
    title: str
    description: str
    status: str
    priority: str
    complexity: str
    feature: str
    technologies: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeFixture:
    project: str
    entity_type: str
    name: str
    description: str
    content: str
    category: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class SourceFixture:
    name: str
    url: str
    source_type: str
    description: str


PROJECTS = (
    ProjectFixture(
        key="sibyl",
        name="Sibyl",
        description=(
            "A graph-native memory system for durable knowledge, search, and task coordination."
        ),
        languages=("Python", "TypeScript"),
        technologies=("SurrealDB", "FastAPI", "Next.js"),
    ),
    ProjectFixture(
        key="hypercolor",
        name="Hypercolor",
        description=(
            "A real-time lighting engine that composes rich color across rooms and devices."
        ),
        languages=("Rust",),
        technologies=("Ratatui", "Tokio", "WebSocket"),
    ),
    ProjectFixture(
        key="chromacat",
        name="Chromacat",
        description=(
            "A playful desktop companion for shaping palettes, scenes, and reactive light."
        ),
        languages=("Swift", "Metal"),
        technologies=("SwiftUI", "Core ML", "Metal"),
    ),
)

TASKS = (
    TaskFixture(
        "sibyl",
        "Add relationship filters to graph search",
        "Let graph explorers narrow results by relationship type.",
        "doing",
        "high",
        "medium",
        "Graph exploration",
        ("SurrealDB", "Python"),
    ),
    TaskFixture(
        "sibyl",
        "Polish the memory detail timeline",
        "Make revisions and source lineage easier to scan.",
        "review",
        "medium",
        "medium",
        "Memory details",
        ("Next.js", "TypeScript"),
    ),
    TaskFixture(
        "sibyl",
        "Teach recall about exact identifiers",
        "Boost precise matches for symbols, flags, and error strings.",
        "todo",
        "high",
        "complex",
        "Retrieval",
        ("Python", "SurrealDB"),
    ),
    TaskFixture(
        "sibyl",
        "Render source health on the dashboard",
        "Show crawl freshness and failed imports at a glance.",
        "todo",
        "medium",
        "medium",
        "Dashboard",
        ("React", "TypeScript"),
    ),
    TaskFixture(
        "sibyl",
        "Verify organization export round trips",
        "Exercise export and restore against a representative graph.",
        "done",
        "high",
        "complex",
        "Backups",
        ("Python", "SurrealDB"),
    ),
    TaskFixture(
        "sibyl",
        "Design keyboard navigation for search",
        "Map a fast, accessible flow through ranked results.",
        "backlog",
        "low",
        "simple",
        "Search",
        ("React",),
    ),
    TaskFixture(
        "sibyl",
        "Document the local showcase workflow",
        "Keep public screenshots isolated from real organization data.",
        "done",
        "medium",
        "simple",
        "Documentation",
        ("Markdown",),
    ),
    TaskFixture(
        "hypercolor",
        "Blend room scenes across device boundaries",
        "Keep gradients continuous when fixtures use different color spaces.",
        "doing",
        "high",
        "complex",
        "Scene compositor",
        ("Rust", "Tokio"),
    ),
    TaskFixture(
        "hypercolor",
        "Add a live palette inspector",
        "Expose sampled colors and transition timing in the terminal UI.",
        "todo",
        "medium",
        "medium",
        "Diagnostics",
        ("Ratatui", "Rust"),
    ),
    TaskFixture(
        "hypercolor",
        "Preserve animation phase after reconnect",
        "Resume device streams without a visible jump in motion.",
        "review",
        "high",
        "medium",
        "Device transport",
        ("Rust", "WebSocket"),
    ),
    TaskFixture(
        "hypercolor",
        "Profile the sparkle compositor",
        "Find frame-time hot spots under a dense multi-room scene.",
        "todo",
        "high",
        "medium",
        "Performance",
        ("Rust",),
    ),
    TaskFixture(
        "hypercolor",
        "Ship scene presets as portable bundles",
        "Package palettes, timing, and mappings into one shareable artifact.",
        "backlog",
        "medium",
        "complex",
        "Scene library",
        ("Rust", "Serde"),
    ),
    TaskFixture(
        "hypercolor",
        "Cache device capability discovery",
        "Avoid repeating stable discovery work during warm starts.",
        "done",
        "medium",
        "simple",
        "Device transport",
        ("Rust",),
    ),
    TaskFixture(
        "hypercolor",
        "Recover cleanly from partial scene updates",
        "Keep unaffected devices moving when one adapter rejects a frame.",
        "blocked",
        "high",
        "complex",
        "Resilience",
        ("Rust", "Tokio"),
    ),
    TaskFixture(
        "chromacat",
        "Prototype the moonlight palette editor",
        "Shape cool gradients with immediate room previews.",
        "doing",
        "medium",
        "medium",
        "Palette editor",
        ("SwiftUI", "Metal"),
    ),
    TaskFixture(
        "chromacat",
        "Animate the scene picker",
        "Give scene transitions a crisp spatial rhythm.",
        "todo",
        "medium",
        "simple",
        "Scene library",
        ("SwiftUI",),
    ),
    TaskFixture(
        "chromacat",
        "Train ambient color suggestions",
        "Recommend palettes from time of day and recent scene choices.",
        "todo",
        "low",
        "complex",
        "Suggestions",
        ("Core ML", "Swift"),
    ),
    TaskFixture(
        "chromacat",
        "Add reduced-motion transitions",
        "Respect accessibility preferences without flattening the interface.",
        "done",
        "high",
        "medium",
        "Accessibility",
        ("SwiftUI",),
    ),
    TaskFixture(
        "chromacat",
        "Sync favorites between desktop and mobile",
        "Keep a small palette library available on every device.",
        "backlog",
        "medium",
        "complex",
        "Cloud sync",
        ("Swift", "CloudKit"),
    ),
    TaskFixture(
        "chromacat",
        "Tune Metal shader previews",
        "Hold a smooth preview frame rate on integrated graphics.",
        "doing",
        "high",
        "medium",
        "Rendering",
        ("Metal", "Swift"),
    ),
    TaskFixture(
        "chromacat",
        "Write the first-run scene tour",
        "Introduce palettes, rooms, and automations in under a minute.",
        "todo",
        "low",
        "simple",
        "Onboarding",
        ("SwiftUI",),
    ),
)

KNOWLEDGE = (
    KnowledgeFixture(
        "sibyl",
        "decision",
        "Project scope belongs on every graph write",
        "Project context is explicit at the storage boundary.",
        "Every project-scoped entity carries its project identifier into the graph client.",
        "architecture",
        ("multi-tenancy", "graph"),
    ),
    KnowledgeFixture(
        "sibyl",
        "procedure",
        "Rehearse a memory before relying on it",
        "Use retrieval probes to prove that important knowledge can be found.",
        "Attach concrete questions to a memory, write it synchronously, and inspect the ranks.",
        "retrieval",
        ("search", "verification"),
    ),
    KnowledgeFixture(
        "sibyl",
        "error_pattern",
        "A healthy queue can still hide stale search data",
        "Queue health and retrieval freshness are separate signals.",
        "Verify the stored entity, its derived passages, and a live search result after ingestion.",
        "operations",
        ("search", "freshness"),
    ),
    KnowledgeFixture(
        "sibyl",
        "pattern",
        "Summaries lead and evidence follows",
        "Memory cards stay scannable without hiding their provenance.",
        "Lead with the durable conclusion, then keep source and revision evidence one click away.",
        "product",
        ("ux", "provenance"),
    ),
    KnowledgeFixture(
        "sibyl",
        "claim",
        "Exact identifiers deserve a lexical retrieval path",
        "Symbols and error strings should not depend on semantic similarity.",
        "Normalize exact keys and search them directly before blending semantic and graph scores.",
        "retrieval",
        ("search", "identifiers"),
    ),
    KnowledgeFixture(
        "hypercolor",
        "decision",
        "Scene time is monotonic",
        "Every adapter samples the same compositor clock.",
        "A shared monotonic timeline prevents phase drift between lighting devices.",
        "rendering",
        ("animation", "timing"),
    ),
    KnowledgeFixture(
        "hypercolor",
        "procedure",
        "Profile a dense room scene",
        "Capture frame time before changing the compositor.",
        "Replay a fixed multi-room scene, sample each stage, and compare the p95 frame budget.",
        "performance",
        ("profiling", "compositor"),
    ),
    KnowledgeFixture(
        "hypercolor",
        "error_pattern",
        "Reconnects can reset animation phase",
        "A transport reconnect must not restart the visual timeline.",
        "Adapters rejoin at the compositor's current timestamp instead of creating a new local clock.",
        "resilience",
        ("animation", "reconnect"),
    ),
    KnowledgeFixture(
        "hypercolor",
        "artifact",
        "SparkleFlinger frame budget",
        "A reference trace for dense scene composition.",
        "The trace records sampling, blending, color conversion, and adapter publish time.",
        "performance",
        ("profiling", "reference"),
    ),
    KnowledgeFixture(
        "hypercolor",
        "idea",
        "Treat scene bundles as tiny programs",
        "Portable scenes can declare palettes, timing, and device mappings.",
        "A validated bundle format makes scenes shareable without coupling them to one room layout.",
        "design",
        ("scenes", "portability"),
    ),
    KnowledgeFixture(
        "chromacat",
        "decision",
        "Palette previews render on the GPU",
        "The editor keeps interaction smooth while gradients evolve live.",
        "Metal renders preview strips and room swatches from the same palette parameters.",
        "rendering",
        ("metal", "palette"),
    ),
    KnowledgeFixture(
        "chromacat",
        "pattern",
        "Motion communicates scene hierarchy",
        "Transitions should reveal navigation rather than decorate it.",
        "Scene cards move along the same spatial axis as the selection gesture.",
        "interaction",
        ("motion", "ux"),
    ),
    KnowledgeFixture(
        "chromacat",
        "procedure",
        "Check a palette for accessible contrast",
        "Preview text and controls against every generated surface color.",
        "Test contrast in light and dark appearances, then repeat with increased contrast enabled.",
        "accessibility",
        ("contrast", "palette"),
    ),
    KnowledgeFixture(
        "chromacat",
        "claim",
        "Reduced motion can preserve spatial meaning",
        "Accessibility does not require removing every transition.",
        "Short crossfades and restrained scale changes preserve hierarchy without sweeping movement.",
        "accessibility",
        ("motion", "swiftui"),
    ),
    KnowledgeFixture(
        "chromacat",
        "topic",
        "Ambient palette intelligence",
        "Color suggestions grounded in context and explicit taste.",
        "Explore local suggestions from time, room, and recent favorites without exporting private data.",
        "research",
        ("color", "privacy"),
    ),
)

SOURCES = (
    SourceFixture(
        "Sibyl on GitHub",
        "https://github.com/hyperb1iss/sibyl",
        "github",
        "Source code, releases, and project documentation for Sibyl.",
    ),
    SourceFixture(
        "SurrealDB Documentation",
        "https://surrealdb.com/docs/surrealdb",
        "api_docs",
        "Database concepts, SurrealQL reference, and deployment guidance.",
    ),
    SourceFixture(
        "Moonrepo Documentation",
        "https://moonrepo.dev/docs",
        "website",
        "Task orchestration and monorepo workflow documentation.",
    ),
    SourceFixture(
        "Ratatui Documentation",
        "https://ratatui.rs",
        "website",
        "Patterns and API guidance for expressive terminal interfaces.",
    ),
    SourceFixture(
        "Astral uv Documentation",
        "https://docs.astral.sh/uv",
        "website",
        "Python project, environment, and dependency management guidance.",
    ),
)
