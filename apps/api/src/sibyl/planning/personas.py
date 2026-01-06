"""Persona generation for multi-agent brainstorming.

Uses Claude to dynamically generate personas based on the brainstorming topic.
"""

from __future__ import annotations

import anthropic
import structlog

log = structlog.get_logger()

# Use Haiku for fast, cheap persona generation
_HAIKU_MODEL = "claude-3-5-haiku-latest"


async def generate_personas(
    prompt: str,
    count: int = 4,
) -> list[dict]:
    """Generate personas for a brainstorming session.

    Uses Claude Haiku to analyze the prompt and generate diverse
    personas that will provide complementary perspectives.

    Args:
        prompt: The brainstorming prompt/topic
        count: Number of personas to generate (2-6)

    Returns:
        List of persona dicts with role, name, focus, system_prompt
    """
    count = max(2, min(6, count))

    generation_prompt = f"""Analyze this brainstorming request and generate {count} diverse AI personas that will provide complementary perspectives.

REQUEST:
{prompt}

For each persona, provide:
1. role: A short archetype (e.g., "devil_advocate", "pragmatist", "visionary", "user_champion", "technical_expert")
2. name: A memorable name for this persona
3. focus: What this persona specifically focuses on (1 sentence)
4. system_prompt: Full system instructions for this persona (2-3 sentences)

Guidelines:
- Personas should DISAGREE and CHALLENGE each other
- Include at least one skeptic/devil's advocate
- Include at least one user/customer champion
- Balance between visionary and practical perspectives
- Each persona should bring unique expertise

Return ONLY valid JSON in this exact format:
[
  {{
    "role": "devil_advocate",
    "name": "The Challenger",
    "focus": "Finding flaws, risks, and overlooked problems",
    "system_prompt": "You are The Challenger. Your job is to find holes in ideas, identify risks, and push back on assumptions. Be constructively critical - don't just criticize, suggest what would address your concerns."
  }},
  ...
]"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": generation_prompt}],
        )

        # Parse JSON response
        import json

        text = response.content[0].text.strip()

        # Handle markdown code blocks
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        personas = json.loads(text)

        log.info(
            "Generated personas",
            count=len(personas),
            roles=[p.get("role") for p in personas],
        )

        return personas

    except Exception as e:
        log.warning(f"Persona generation failed: {e}, using defaults")
        return _default_personas(count)


def single_brainstormer() -> dict:
    """Default single brainstormer for non-persona mode."""
    return {
        "role": "brainstormer",
        "name": "Brainstormer",
        "focus": "Exploring the topic thoroughly from all angles",
        "system_prompt": (
            "You are a thoughtful brainstormer. Explore this topic thoroughly - "
            "consider different angles, identify challenges and opportunities, "
            "suggest concrete approaches, and think through implications. "
            "Be direct and specific, not abstract."
        ),
    }


def _default_personas(count: int) -> list[dict]:
    """Fallback personas if generation fails."""
    defaults = [
        {
            "role": "devil_advocate",
            "name": "The Skeptic",
            "focus": "Finding flaws, risks, and overlooked problems",
            "system_prompt": (
                "You are The Skeptic. Your job is to find holes in ideas, identify risks, "
                "and push back on assumptions. Be constructively critical."
            ),
        },
        {
            "role": "user_champion",
            "name": "The User Voice",
            "focus": "Representing user needs, pain points, and experience",
            "system_prompt": (
                "You are The User Voice. Champion the end user's perspective. "
                "What would frustrate them? What would delight them? Focus on real user problems."
            ),
        },
        {
            "role": "visionary",
            "name": "The Dreamer",
            "focus": "Exploring bold possibilities and future potential",
            "system_prompt": (
                "You are The Dreamer. Think big and explore what's possible without constraints. "
                "Generate creative, innovative ideas that push boundaries."
            ),
        },
        {
            "role": "pragmatist",
            "name": "The Builder",
            "focus": "Practical implementation, resources, and timeline",
            "system_prompt": (
                "You are The Builder. Focus on what can actually be built with available resources. "
                "Break ideas into actionable steps and identify implementation challenges."
            ),
        },
        {
            "role": "analyst",
            "name": "The Analyst",
            "focus": "Data, metrics, and measurable outcomes",
            "system_prompt": (
                "You are The Analyst. Ground discussions in data and measurable outcomes. "
                "What metrics would prove success? What does the data suggest?"
            ),
        },
        {
            "role": "connector",
            "name": "The Synthesizer",
            "focus": "Finding patterns and connecting ideas across perspectives",
            "system_prompt": (
                "You are The Synthesizer. Find connections between different viewpoints. "
                "Identify common themes and bridge opposing perspectives."
            ),
        },
    ]
    return defaults[:count]


def build_persona_system_prompt(
    persona: dict,
    session_prompt: str,
    round_number: int = 1,
) -> str:
    """Build the full system prompt for a persona agent.

    Args:
        persona: Persona definition dict
        session_prompt: The main brainstorming prompt
        round_number: Which brainstorming round this is

    Returns:
        Complete system prompt for the agent
    """
    base_prompt = persona.get("system_prompt", "You are a brainstorming participant.")
    name = persona.get("name", "Participant")
    focus = persona.get("focus", "contributing to the discussion")

    return f"""# {name} - Brainstorming Session

{base_prompt}

## Your Focus
{focus}

## Brainstorming Topic
{session_prompt}

## Guidelines
- Provide your UNIQUE perspective based on your role
- Be direct and specific - no hedging or generic advice
- Challenge other viewpoints constructively
- Propose concrete ideas, not abstract concepts
- If you disagree with the premise, say so and explain why

## Round {round_number} Instructions
{"Share your initial thoughts and key concerns." if round_number == 1 else "Build on previous discussion, respond to other perspectives, and refine your position."}

Respond in 200-400 words with clear, actionable insights from your perspective."""
