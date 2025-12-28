"""LLM-enhanced content generator using Claude Sonnet/Opus."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from sibyl.generator.base import BaseGenerator, GeneratorResult
from sibyl.generator.config import GeneratorConfig, ModelType
from sibyl.generator.templates import (
    PATTERN_TEMPLATES,
    TASK_COMPONENTS,
    TASK_FEATURES,
    TEAM_NAMES,
    TemplateGenerator,
)
from sibyl.models.entities import Entity, EntityType

# Cache directory for LLM responses
CACHE_DIR = Path.home() / ".cache" / "sibyl" / "generator"


class LLMContentGenerator(BaseGenerator):
    """Generate rich content using Claude models.

    Uses Anthropic's Claude Sonnet 4.5 for balanced quality/cost,
    or Claude Opus 4.5 for highest quality complex generation.
    """

    def __init__(self, config: GeneratorConfig) -> None:
        super().__init__(config)
        self._client: Any = None
        self._template_gen = TemplateGenerator(config)
        self._cache: dict[str, str] = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def client(self) -> Any:
        """Lazy-load Anthropic client."""
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic()
            except ImportError as e:
                msg = "anthropic package required. Install with: uv add anthropic"
                raise ImportError(msg) from e
        return self._client

    def _cache_key(self, prompt: str, model: str) -> str:
        """Generate cache key for prompt + model."""
        content = f"{model}:{prompt}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _get_cached(self, prompt: str, model: str) -> str | None:
        """Get cached response if available."""
        key = self._cache_key(prompt, model)

        # Check memory cache first
        if key in self._cache:
            return self._cache[key]

        # Check disk cache
        cache_file = CACHE_DIR / f"{key}.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                self._cache[key] = data["response"]
                return data["response"]
            except (json.JSONDecodeError, KeyError):
                pass

        return None

    def _set_cached(self, prompt: str, model: str, response: str) -> None:
        """Cache response to disk and memory."""
        key = self._cache_key(prompt, model)
        self._cache[key] = response

        cache_file = CACHE_DIR / f"{key}.json"
        cache_file.write_text(
            json.dumps(
                {
                    "prompt": prompt[:500],  # Truncate for storage
                    "model": model,
                    "response": response,
                }
            )
        )

    async def _generate_content(
        self,
        prompt: str,
        model: ModelType | None = None,
        max_tokens: int = 1024,
    ) -> str:
        """Generate content using Claude.

        Args:
            prompt: The prompt to send to Claude.
            model: Model to use (defaults to config.model).
            max_tokens: Maximum tokens in response.

        Returns:
            Generated content string.
        """
        model = model or self.config.model
        model_id = model.model_id

        # Check cache first
        cached = self._get_cached(prompt, model_id)
        if cached:
            return cached

        # Call Anthropic API
        message = self.client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        response = message.content[0].text
        self._set_cached(prompt, model_id, response)
        return response

    async def generate_pattern_content(
        self,
        domain: str,
        language: str,
        name: str | None = None,
    ) -> tuple[str, str, str]:
        """Generate rich pattern content.

        Args:
            domain: The domain (e.g., "API Design", "Security").
            language: Programming language context.
            name: Optional pattern name hint.

        Returns:
            Tuple of (name, description, content).
        """
        prompt = f"""Generate a software development pattern for the following context:

Domain: {domain}
Language: {language}
{f"Name hint: {name}" if name else ""}

Respond in this exact JSON format:
{{
    "name": "Pattern Name",
    "description": "One-sentence description of the pattern",
    "content": "Detailed markdown content explaining the pattern, when to use it, trade-offs, and examples. Include code snippets if relevant. 200-400 words."
}}

Be specific and practical. Focus on real-world applicability."""

        response = await self._generate_content(prompt)

        try:
            # Extract JSON from response
            start = response.find("{")
            end = response.rfind("}") + 1
            data = json.loads(response[start:end])
            return data["name"], data["description"], data["content"]
        except (json.JSONDecodeError, KeyError):
            # Fallback to template
            patterns = PATTERN_TEMPLATES.get(domain, [("Default Pattern", "A development pattern")])
            name, desc = self._template_gen.rng.choice(patterns)
            return name, desc, f"# {name}\n\n{desc}"

    async def generate_task_description(
        self,
        title: str,
        project_context: str,
        feature: str,
    ) -> str:
        """Generate detailed task description.

        Args:
            title: Task title.
            project_context: Context about the project.
            feature: Feature area (e.g., "authentication").

        Returns:
            Detailed task description.
        """
        prompt = f"""Write a detailed task description for a software development task:

Title: {title}
Project: {project_context}
Feature Area: {feature}

Write 2-3 paragraphs covering:
1. What needs to be done and why
2. Technical approach and considerations
3. Acceptance criteria

Be specific and actionable. No JSON, just plain text."""

        return await self._generate_content(prompt, max_tokens=512)

    async def generate_episode_learnings(
        self,
        context: str,
        domain: str,
    ) -> tuple[str, str]:
        """Generate episode learnings from context.

        Args:
            context: The situation/context for the learning.
            domain: Domain area (e.g., "Performance", "Security").

        Returns:
            Tuple of (summary, detailed_learnings).
        """
        prompt = f"""Generate a learning episode from a software development experience:

Context: {context}
Domain: {domain}

Respond in this exact JSON format:
{{
    "summary": "One-sentence summary of what was learned",
    "learnings": "Detailed narrative (150-250 words) about what happened, what was discovered, and how it can be applied in the future. Include specific technical details."
}}

Make it feel like a real experience from a development team."""

        response = await self._generate_content(
            prompt, model=ModelType.OPUS
        )  # Use Opus for richer narratives

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            data = json.loads(response[start:end])
            return data["summary"], data["learnings"]
        except (json.JSONDecodeError, KeyError):
            return (
                f"Learned about {domain} best practices",
                f"After working on {context}, the team discovered important insights about {domain}.",
            )

    async def generate(self) -> GeneratorResult:
        """Generate all entities with LLM-enhanced content."""
        import time

        start = time.time()
        result = GeneratorResult()

        # Generate projects with LLM descriptions
        projects = []
        for i in range(self.config.projects):
            project = await self._generate_llm_project(i)
            projects.append(project)
            result.entities.append(project)

        # Generate patterns with rich content
        for _ in range(self.config.patterns):
            pattern = await self._generate_llm_pattern()
            result.entities.append(pattern)

        # Generate tasks for each project
        for project in projects:
            for _ in range(self.config.tasks_per_project):
                task = await self._generate_llm_task(project)
                result.entities.append(task)

        # Generate episodes with narrative learnings
        for _ in range(self.config.episodes):
            episode = await self._generate_llm_episode()
            result.entities.append(episode)

        # Generate rules and templates (use template generator - less need for LLM)
        rules = await self._template_gen.generate_batch(self.config.rules, "rule")
        templates = await self._template_gen.generate_batch(self.config.templates, "template")
        result.entities.extend(rules)
        result.entities.extend(templates)

        result.duration_seconds = time.time() - start
        return result

    async def generate_batch(self, count: int, entity_type: str) -> list[Entity]:
        """Generate a batch of LLM-enhanced entities."""
        entities = []

        if entity_type == "pattern":
            for _ in range(count):
                entities.append(await self._generate_llm_pattern())  # noqa: PERF401
        elif entity_type == "episode":
            for _ in range(count):
                entities.append(await self._generate_llm_episode())  # noqa: PERF401
        elif entity_type == "task":
            for _ in range(count):
                entities.append(await self._generate_llm_task(None))  # noqa: PERF401
        else:
            # Fallback to template generator for other types
            entities = await self._template_gen.generate_batch(count, entity_type)

        return entities

    async def _generate_llm_project(self, index: int) -> Entity:
        """Generate a project with LLM-enhanced description."""
        team = self._template_gen.pick(TEAM_NAMES)
        language = self._template_gen.pick(self.config.languages)
        framework = self._template_gen.pick(self.config.frameworks)

        prompt = f"""Generate a software project description:

Team: {team}
Tech Stack: {language}, {framework}
Project Index: {index + 1}

Respond in JSON:
{{
    "name": "Creative project name (no generic names)",
    "description": "2-3 sentence description of what this project does and its goals",
    "objectives": ["objective 1", "objective 2", "objective 3"]
}}"""

        response = await self._generate_content(prompt, max_tokens=300)

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            data = json.loads(response[start:end])

            return Entity(
                id=self._template_gen.next_id("proj"),
                name=data["name"],
                entity_type=EntityType.PROJECT,
                description=data["description"],
                content=f"# {data['name']}\n\n{data['description']}\n\n## Objectives\n"
                + "\n".join(f"- {obj}" for obj in data.get("objectives", [])),
                metadata=self._template_gen.mark_generated(
                    {
                        "team": team,
                        "language": language,
                        "framework": framework,
                        "status": "active",
                        "objectives": data.get("objectives", []),
                    }
                ),
                created_at=self._template_gen.now() - timedelta(days=self.rng.randint(7, 90)),
            )
        except (json.JSONDecodeError, KeyError):
            # Fallback
            return (await self._template_gen.generate_batch(1, "project"))[0]

    async def _generate_llm_pattern(self) -> Entity:
        """Generate a pattern with rich LLM content."""
        domain = self._template_gen.pick(list(PATTERN_TEMPLATES.keys()))
        language = self._template_gen.pick(self.config.languages)

        name, description, content = await self.generate_pattern_content(domain, language)

        return Entity(
            id=self._template_gen.next_id("pat"),
            name=name,
            entity_type=EntityType.PATTERN,
            description=description,
            content=content,
            metadata=self._template_gen.mark_generated(
                {
                    "domain": domain,
                    "language": language,
                    "llm_generated": True,
                    "model": self.config.model.value,
                }
            ),
            created_at=self._template_gen.now() - timedelta(days=self.rng.randint(30, 365)),
        )

    async def _generate_llm_task(self, project: Entity | None) -> Entity:
        """Generate a task with LLM-enhanced description."""
        feature = self._template_gen.pick(TASK_FEATURES)
        component = self._template_gen.pick(TASK_COMPONENTS)
        title = f"Implement {feature} for {component}"

        project_context = project.name if project else "General infrastructure project"
        description = await self.generate_task_description(title, project_context, feature)

        return Entity(
            id=self._template_gen.next_id("task"),
            name=title,
            entity_type=EntityType.TASK,
            description=description[:500],  # Truncate for entity description
            content=description,
            metadata=self._template_gen.mark_generated(
                {
                    "status": self._template_gen.pick(
                        ["backlog", "todo", "doing", "review", "done"]
                    ),
                    "priority": self._template_gen.pick(["critical", "high", "medium", "low"]),
                    "project_id": project.id if project else None,
                    "feature": feature,
                    "component": component,
                    "llm_generated": True,
                }
            ),
            created_at=self._template_gen.now() - timedelta(days=self.rng.randint(0, 30)),
        )

    async def _generate_llm_episode(self) -> Entity:
        """Generate an episode with rich narrative learnings."""
        domain = self._template_gen.pick(list(PATTERN_TEMPLATES.keys()))
        contexts = [
            "debugging a production outage",
            "reviewing a colleague's pull request",
            "optimizing database queries",
            "implementing a new feature",
            "migrating to a new framework",
            "setting up CI/CD pipelines",
        ]
        context = self._template_gen.pick(contexts)

        summary, learnings = await self.generate_episode_learnings(context, domain)

        return Entity(
            id=self._template_gen.next_id("ep"),
            name=f"Episode: {summary[:50]}...",
            entity_type=EntityType.EPISODE,
            description=summary,
            content=learnings,
            metadata=self._template_gen.mark_generated(
                {
                    "domain": domain,
                    "context": context,
                    "impact": self._template_gen.pick(["high", "medium", "low"]),
                    "llm_generated": True,
                    "model": ModelType.OPUS.value,  # Episodes use Opus
                }
            ),
            created_at=self._template_gen.now() - timedelta(days=self.rng.randint(1, 60)),
        )
