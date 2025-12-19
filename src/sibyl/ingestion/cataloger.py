"""Template and config file cataloger for the conventions repository."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import yaml


@dataclass
class CatalogedTemplate:
    """A cataloged template file."""

    file_path: Path
    name: str
    template_type: str  # project, config, code, workflow
    language: str | None
    description: str
    variables: list[str]  # Placeholder variables found
    content: str
    word_count: int
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class CatalogedConfig:
    """A cataloged configuration file."""

    file_path: Path
    name: str
    config_type: str  # pyproject, tsconfig, docker, ci, etc.
    language: str | None
    description: str
    key_fields: list[str]  # Important config keys
    content: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class CatalogedSlashCommand:
    """A cataloged Claude Code slash command."""

    file_path: Path
    command_name: str
    description: str
    prompt_content: str
    word_count: int


class TemplateCataloger:
    """Catalogs templates, configs, and slash commands from the conventions repo.

    Scans language-specific directories and extracts metadata
    for searchable indexing.
    """

    # Variable patterns for different file types
    VARIABLE_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"\{\{(\w+)\}\}"),  # {{variable}}
        re.compile(r"\$\{(\w+)\}"),  # ${variable}
        re.compile(r"<(\w+)>"),  # <variable> (in comments)
    ]

    # Language directory mappings
    LANGUAGE_DIRS: ClassVar[dict[str, list[str]]] = {
        "python": ["python"],
        "typescript": ["typescript", "ts"],
        "javascript": ["javascript", "js"],
        "rust": ["rust"],
        "swift": ["swift"],
        "go": ["go", "golang"],
    }

    # Config file patterns and their types
    CONFIG_PATTERNS: ClassVar[dict[str, tuple[str, str | None]]] = {
        "pyproject.toml": ("pyproject", "python"),
        "setup.py": ("setup", "python"),
        "setup.cfg": ("setup", "python"),
        "tsconfig.json": ("tsconfig", "typescript"),
        "package.json": ("package", "javascript"),
        "Cargo.toml": ("cargo", "rust"),
        "go.mod": ("gomod", "go"),
        "docker-compose.yml": ("docker", None),
        "docker-compose.yaml": ("docker", None),
        "Dockerfile": ("docker", None),
        ".pre-commit-config.yaml": ("precommit", None),
        ".github/workflows/*.yml": ("ci", None),
        ".github/workflows/*.yaml": ("ci", None),
    }

    def __init__(self, repo_root: Path) -> None:
        """Initialize the cataloger.

        Args:
            repo_root: Root path of the conventions repository.
        """
        self.repo_root = repo_root

    def catalog_templates(self) -> list[CatalogedTemplate]:
        """Catalog all template files in the repository.

        Returns:
            List of cataloged templates.
        """
        templates: list[CatalogedTemplate] = []

        # Scan language directories
        for language, dirs in self.LANGUAGE_DIRS.items():
            for dir_name in dirs:
                lang_dir = self.repo_root / dir_name
                if lang_dir.exists():
                    templates.extend(self._scan_template_directory(lang_dir, language))

        # Scan shared templates
        shared_dir = self.repo_root / "shared"
        if shared_dir.exists():
            templates.extend(self._scan_template_directory(shared_dir, None))

        # Scan templates directory if exists
        templates_dir = self.repo_root / "templates"
        if templates_dir.exists():
            templates.extend(self._scan_template_directory(templates_dir, None))

        return templates

    def _scan_template_directory(
        self,
        directory: Path,
        language: str | None,
    ) -> list[CatalogedTemplate]:
        """Scan a directory for template files.

        Args:
            directory: Directory to scan.
            language: Associated language.

        Returns:
            List of templates found.
        """
        templates: list[CatalogedTemplate] = []

        # Template file extensions
        template_extensions = {
            ".template",
            ".tmpl",
            ".tpl",
            ".example",
            ".md",
            ".py",
            ".ts",
            ".js",
            ".toml",
            ".yaml",
            ".yml",
            ".json",
        }

        for file_path in directory.rglob("*"):
            if not file_path.is_file():
                continue

            # Check if it's a template file
            is_template = (
                file_path.suffix in template_extensions
                or ".template" in file_path.name
                or ".example" in file_path.name
            )

            if not is_template:
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                variables = self._extract_variables(content)

                # Determine template type
                template_type = self._determine_template_type(file_path)

                # Generate description
                description = self._generate_description(file_path, content)

                templates.append(
                    CatalogedTemplate(
                        file_path=file_path,
                        name=file_path.stem,
                        template_type=template_type,
                        language=language or self._detect_language(file_path),
                        description=description,
                        variables=variables,
                        content=content,
                        word_count=len(content.split()),
                    )
                )
            except Exception:
                # Skip files that can't be read
                continue

        return templates

    def _extract_variables(self, content: str) -> list[str]:
        """Extract template variables from content.

        Args:
            content: File content.

        Returns:
            List of variable names.
        """
        variables = set()
        for pattern in self.VARIABLE_PATTERNS:
            for match in pattern.finditer(content):
                variables.add(match.group(1))
        return sorted(variables)

    def _determine_template_type(self, file_path: Path) -> str:
        """Determine the type of template.

        Args:
            file_path: Path to template file.

        Returns:
            Template type string.
        """
        name = file_path.name.lower()

        if "project" in name or file_path.parent.name == "templates":
            return "project"
        if any(cfg in name for cfg in ["config", "toml", "yaml", "json"]):
            return "config"
        if any(ext in name for ext in [".py", ".ts", ".js", ".rs"]):
            return "code"
        if "workflow" in str(file_path) or "ci" in name:
            return "workflow"
        if "docker" in name:
            return "docker"

        return "other"

    def _detect_language(self, file_path: Path) -> str | None:
        """Detect language from file extension or path.

        Args:
            file_path: Path to file.

        Returns:
            Detected language or None.
        """
        ext_map = {
            ".py": "python",
            ".ts": "typescript",
            ".js": "javascript",
            ".rs": "rust",
            ".swift": "swift",
            ".go": "go",
        }
        return ext_map.get(file_path.suffix)

    def _generate_description(self, file_path: Path, content: str) -> str:
        """Generate a description for the template.

        Args:
            file_path: Path to template.
            content: File content.

        Returns:
            Description string.
        """
        # Try to extract from first comment or docstring
        lines = content.split("\n")
        for line in lines[:10]:
            line = line.strip()
            if line.startswith("#") and not line.startswith("#!"):
                return line[1:].strip()
            if line.startswith("//"):
                return line[2:].strip()
            if line.startswith('"""') or line.startswith("'''"):
                return line[3:].strip().rstrip('"""').rstrip("'''")

        return f"Template: {file_path.name}"

    def catalog_configs(self) -> list[CatalogedConfig]:
        """Catalog all configuration files in the repository.

        Returns:
            List of cataloged configs.
        """
        configs: list[CatalogedConfig] = []

        for pattern, (config_type, language) in self.CONFIG_PATTERNS.items():
            if "*" in pattern:
                # Glob pattern
                for file_path in self.repo_root.glob(pattern):
                    if file_path.is_file():
                        config = self._catalog_config_file(file_path, config_type, language)
                        if config:
                            configs.append(config)
            else:
                # Exact file
                file_path = self.repo_root / pattern
                if file_path.exists():
                    config = self._catalog_config_file(file_path, config_type, language)
                    if config:
                        configs.append(config)

        # Also check language subdirectories
        for language, dirs in self.LANGUAGE_DIRS.items():
            for dir_name in dirs:
                lang_dir = self.repo_root / dir_name
                if lang_dir.exists():
                    for pattern, (config_type, _) in self.CONFIG_PATTERNS.items():
                        if "*" not in pattern:
                            file_path = lang_dir / pattern
                            if file_path.exists():
                                config = self._catalog_config_file(file_path, config_type, language)
                                if config:
                                    configs.append(config)

        return configs

    def _catalog_config_file(
        self,
        file_path: Path,
        config_type: str,
        language: str | None,
    ) -> CatalogedConfig | None:
        """Catalog a single config file.

        Args:
            file_path: Path to config file.
            config_type: Type of configuration.
            language: Associated language.

        Returns:
            CatalogedConfig or None if failed.
        """
        try:
            content = file_path.read_text(encoding="utf-8")
            key_fields = self._extract_key_fields(content, file_path.suffix)

            return CatalogedConfig(
                file_path=file_path,
                name=file_path.name,
                config_type=config_type,
                language=language,
                description=f"{config_type.title()} configuration file",
                key_fields=key_fields,
                content=content,
            )
        except Exception:
            return None

    def _extract_key_fields(self, content: str, suffix: str) -> list[str]:
        """Extract key configuration fields.

        Args:
            content: File content.
            suffix: File extension.

        Returns:
            List of key field names.
        """
        key_fields = []

        if suffix in (".yaml", ".yml"):
            try:
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    key_fields = list(data.keys())[:20]
            except yaml.YAMLError:
                pass
        elif suffix == ".toml":
            # Simple TOML section extraction
            for line in content.split("\n"):
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].split(".")[0]
                    if section not in key_fields:
                        key_fields.append(section)
        elif suffix == ".json":
            # Simple JSON key extraction
            import json

            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    key_fields = list(data.keys())[:20]
            except json.JSONDecodeError:
                pass

        return key_fields

    def catalog_slash_commands(self) -> list[CatalogedSlashCommand]:
        """Catalog Claude Code slash commands.

        Returns:
            List of cataloged slash commands.
        """
        commands: list[CatalogedSlashCommand] = []

        # Check for claude-skills directory
        skills_dir = self.repo_root / "claude-skills"
        if not skills_dir.exists():
            # Try alternate locations
            for alt in [".claude/commands", "claude/skills", ".claude-skills"]:
                alt_dir = self.repo_root / alt
                if alt_dir.exists():
                    skills_dir = alt_dir
                    break

        if not skills_dir.exists():
            return commands

        # Scan for markdown files (slash command definitions)
        for file_path in skills_dir.glob("*.md"):
            try:
                content = file_path.read_text(encoding="utf-8")

                # Extract description from first line or heading
                lines = content.split("\n")
                description = ""
                for line in lines[:5]:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line[:200]
                        break
                    if line.startswith("# "):
                        description = line[2:]
                        break

                commands.append(
                    CatalogedSlashCommand(
                        file_path=file_path,
                        command_name=file_path.stem,
                        description=description or f"Slash command: {file_path.stem}",
                        prompt_content=content,
                        word_count=len(content.split()),
                    )
                )
            except Exception:
                continue

        return commands


def catalog_repository(
    repo_root: Path,
) -> tuple[
    list[CatalogedTemplate],
    list[CatalogedConfig],
    list[CatalogedSlashCommand],
]:
    """Catalog all templates, configs, and slash commands in a repository.

    Args:
        repo_root: Root path of the repository.

    Returns:
        Tuple of (templates, configs, slash_commands).
    """
    cataloger = TemplateCataloger(repo_root)
    return (
        cataloger.catalog_templates(),
        cataloger.catalog_configs(),
        cataloger.catalog_slash_commands(),
    )
