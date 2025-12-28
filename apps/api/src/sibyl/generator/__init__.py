"""Synthetic data generator for Sibyl.

Generate realistic development data for stress testing and demos.
Supports both template-based (fast, no API) and LLM-enhanced (Claude) generation.
"""

from sibyl.generator.base import BaseGenerator, GeneratorResult
from sibyl.generator.config import GeneratorConfig, ModelType, ScenarioConfig
from sibyl.generator.llm import LLMContentGenerator
from sibyl.generator.relationships import RelationshipWeaver
from sibyl.generator.scenarios import SCENARIOS, ScenarioRunner
from sibyl.generator.stress import StressTestGenerator
from sibyl.generator.templates import TemplateGenerator

__all__ = [
    # Config
    "GeneratorConfig",
    "ModelType",
    "ScenarioConfig",
    # Base
    "BaseGenerator",
    "GeneratorResult",
    # Generators
    "TemplateGenerator",
    "LLMContentGenerator",
    "StressTestGenerator",
    # Utilities
    "RelationshipWeaver",
    "ScenarioRunner",
    "SCENARIOS",
]
