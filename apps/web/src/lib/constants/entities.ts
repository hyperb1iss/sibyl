// =============================================================================
// Entity Types & Styling
// =============================================================================

// Entity types supported by Sibyl
export const ENTITY_TYPES = [
  'pattern',
  'rule',
  'template',
  'guide',
  'tool',
  'language',
  'topic',
  'episode',
  'procedure',
  'knowledge_source',
  'config_file',
  'slash_command',
  'task',
  'project',
  'epic',
  'team',
  'error_pattern',
  'milestone',
  'source',
  'document',
  'note',
  'concept', // Generic extracted entities
  'file', // File paths
  'function', // Functions/methods
] as const;

export type EntityType = (typeof ENTITY_TYPES)[number];

// Entity colors (neon hex) — the canonical canvas/fallback palette.
// DOM surfaces should prefer the themed `--entity-*` CSS variables (via the
// entity-* Tailwind utilities in ENTITY_STYLES) so they adapt to dawn; these
// hex values are the source for the force-graph canvas, which cannot read CSS
// variables, and a fallback for any inline-style consumer.
export const ENTITY_COLORS: Record<EntityType, string> = {
  pattern: '#e135ff', // Electric Purple
  rule: '#ff6363', // Error Red
  template: '#80ffea', // Neon Cyan
  guide: '#ffb86c', // Orange
  tool: '#f1fa8c', // Electric Yellow
  language: '#ff6ac1', // Coral
  topic: '#e94dff', // Magenta (on-palette)
  episode: '#50fa7b', // Success Green
  procedure: '#8be9fd', // Light Cyan
  knowledge_source: '#8b85a0', // Muted
  config_file: '#bd93f9', // Soft Purple
  slash_command: '#8be9fd', // Light Cyan
  task: '#e135ff', // Electric Purple (work items)
  project: '#80ffea', // Cyan (graph anchor; matches --entity-project)
  epic: '#ffb86c', // Orange
  team: '#ff6ac1', // Coral
  error_pattern: '#ff6363', // Error Red
  milestone: '#f1fa8c', // Electric Yellow
  source: '#ff9580', // Warm Orange
  document: '#6272a4', // Muted Blue
  note: '#9f95c2', // Muted Lilac
  concept: '#a8a8a8', // Neutral Gray (generic entities)
  file: '#61afef', // Sky Blue (files)
  function: '#c678dd', // Purple (code)
};

// Default color for unknown entity types
export const DEFAULT_ENTITY_COLOR = '#8b85a0';

// Entity icons - visual identity for each type (Unicode symbols, no emojis)
export const ENTITY_ICONS: Record<EntityType, string> = {
  pattern: '◈',
  rule: '⚡',
  template: '◇',
  guide: '§',
  tool: '⚙',
  language: '⟨⟩',
  topic: '●',
  episode: '◉',
  procedure: '⇶',
  knowledge_source: '▤',
  config_file: '⚙',
  slash_command: '/',
  task: '☐',
  project: '◆',
  epic: '◈',
  team: '⚑',
  error_pattern: '⚠',
  milestone: '◎',
  source: '⊕',
  document: '▤',
  note: '✎',
  concept: '○', // Generic entity
  file: '▢', // File
  function: 'ƒ', // Function
};

// Enhanced styling system for entity cards
export interface EntityStyle {
  badge: string;
  card: string;
  dot: string;
  accent: string;
  gradient: string;
  border: string;
  glow: string;
}

// Pre-computed Tailwind class combinations for badges and cards.
//
// Every value uses the per-type `entity-*` color utilities, which resolve to
// the themed `--entity-<type>` CSS variables defined in globals.css — so these
// adapt automatically across neon and dawn. The classes are written as literals
// (not generated) because Tailwind v4 only emits utilities it can find as exact
// strings in source.
export const ENTITY_STYLES: Record<EntityType, EntityStyle> = {
  pattern: {
    badge: 'bg-entity-pattern/20 text-entity-pattern border-entity-pattern/30',
    card: 'hover:border-entity-pattern/50 hover:shadow-entity-pattern/20',
    dot: 'bg-entity-pattern',
    accent: 'bg-entity-pattern',
    gradient: 'from-entity-pattern/15 via-transparent to-transparent',
    border: 'border-entity-pattern/30',
    glow: 'shadow-entity-pattern/20',
  },
  rule: {
    badge: 'bg-entity-rule/20 text-entity-rule border-entity-rule/30',
    card: 'hover:border-entity-rule/50 hover:shadow-entity-rule/20',
    dot: 'bg-entity-rule',
    accent: 'bg-entity-rule',
    gradient: 'from-entity-rule/15 via-transparent to-transparent',
    border: 'border-entity-rule/30',
    glow: 'shadow-entity-rule/20',
  },
  template: {
    badge: 'bg-entity-template/20 text-entity-template border-entity-template/30',
    card: 'hover:border-entity-template/50 hover:shadow-entity-template/20',
    dot: 'bg-entity-template',
    accent: 'bg-entity-template',
    gradient: 'from-entity-template/15 via-transparent to-transparent',
    border: 'border-entity-template/30',
    glow: 'shadow-entity-template/20',
  },
  guide: {
    badge: 'bg-entity-guide/20 text-entity-guide border-entity-guide/30',
    card: 'hover:border-entity-guide/50 hover:shadow-entity-guide/20',
    dot: 'bg-entity-guide',
    accent: 'bg-entity-guide',
    gradient: 'from-entity-guide/15 via-transparent to-transparent',
    border: 'border-entity-guide/30',
    glow: 'shadow-entity-guide/20',
  },
  tool: {
    badge: 'bg-entity-tool/20 text-entity-tool border-entity-tool/30',
    card: 'hover:border-entity-tool/50 hover:shadow-entity-tool/20',
    dot: 'bg-entity-tool',
    accent: 'bg-entity-tool',
    gradient: 'from-entity-tool/15 via-transparent to-transparent',
    border: 'border-entity-tool/30',
    glow: 'shadow-entity-tool/20',
  },
  language: {
    badge: 'bg-entity-language/20 text-entity-language border-entity-language/30',
    card: 'hover:border-entity-language/50 hover:shadow-entity-language/20',
    dot: 'bg-entity-language',
    accent: 'bg-entity-language',
    gradient: 'from-entity-language/15 via-transparent to-transparent',
    border: 'border-entity-language/30',
    glow: 'shadow-entity-language/20',
  },
  topic: {
    badge: 'bg-entity-topic/20 text-entity-topic border-entity-topic/30',
    card: 'hover:border-entity-topic/50 hover:shadow-entity-topic/20',
    dot: 'bg-entity-topic',
    accent: 'bg-entity-topic',
    gradient: 'from-entity-topic/15 via-transparent to-transparent',
    border: 'border-entity-topic/30',
    glow: 'shadow-entity-topic/20',
  },
  episode: {
    badge: 'bg-entity-episode/20 text-entity-episode border-entity-episode/30',
    card: 'hover:border-entity-episode/50 hover:shadow-entity-episode/20',
    dot: 'bg-entity-episode',
    accent: 'bg-entity-episode',
    gradient: 'from-entity-episode/15 via-transparent to-transparent',
    border: 'border-entity-episode/30',
    glow: 'shadow-entity-episode/20',
  },
  procedure: {
    badge: 'bg-entity-procedure/20 text-entity-procedure border-entity-procedure/30',
    card: 'hover:border-entity-procedure/50 hover:shadow-entity-procedure/20',
    dot: 'bg-entity-procedure',
    accent: 'bg-entity-procedure',
    gradient: 'from-entity-procedure/15 via-transparent to-transparent',
    border: 'border-entity-procedure/30',
    glow: 'shadow-entity-procedure/20',
  },
  knowledge_source: {
    badge:
      'bg-entity-knowledge-source/20 text-entity-knowledge-source border-entity-knowledge-source/30',
    card: 'hover:border-entity-knowledge-source/50 hover:shadow-entity-knowledge-source/20',
    dot: 'bg-entity-knowledge-source',
    accent: 'bg-entity-knowledge-source',
    gradient: 'from-entity-knowledge-source/15 via-transparent to-transparent',
    border: 'border-entity-knowledge-source/30',
    glow: 'shadow-entity-knowledge-source/20',
  },
  config_file: {
    badge: 'bg-entity-config-file/20 text-entity-config-file border-entity-config-file/30',
    card: 'hover:border-entity-config-file/50 hover:shadow-entity-config-file/20',
    dot: 'bg-entity-config-file',
    accent: 'bg-entity-config-file',
    gradient: 'from-entity-config-file/15 via-transparent to-transparent',
    border: 'border-entity-config-file/30',
    glow: 'shadow-entity-config-file/20',
  },
  slash_command: {
    badge: 'bg-entity-slash-command/20 text-entity-slash-command border-entity-slash-command/30',
    card: 'hover:border-entity-slash-command/50 hover:shadow-entity-slash-command/20',
    dot: 'bg-entity-slash-command',
    accent: 'bg-entity-slash-command',
    gradient: 'from-entity-slash-command/15 via-transparent to-transparent',
    border: 'border-entity-slash-command/30',
    glow: 'shadow-entity-slash-command/20',
  },
  task: {
    badge: 'bg-entity-task/20 text-entity-task border-entity-task/30',
    card: 'hover:border-entity-task/50 hover:shadow-entity-task/20',
    dot: 'bg-entity-task',
    accent: 'bg-entity-task',
    gradient: 'from-entity-task/15 via-transparent to-transparent',
    border: 'border-entity-task/30',
    glow: 'shadow-entity-task/20',
  },
  project: {
    badge: 'bg-entity-project/20 text-entity-project border-entity-project/30',
    card: 'hover:border-entity-project/50 hover:shadow-entity-project/20',
    dot: 'bg-entity-project',
    accent: 'bg-entity-project',
    gradient: 'from-entity-project/15 via-transparent to-transparent',
    border: 'border-entity-project/30',
    glow: 'shadow-entity-project/20',
  },
  epic: {
    badge: 'bg-entity-epic/20 text-entity-epic border-entity-epic/30',
    card: 'hover:border-entity-epic/50 hover:shadow-entity-epic/20',
    dot: 'bg-entity-epic',
    accent: 'bg-entity-epic',
    gradient: 'from-entity-epic/15 via-transparent to-transparent',
    border: 'border-entity-epic/30',
    glow: 'shadow-entity-epic/20',
  },
  team: {
    badge: 'bg-entity-team/20 text-entity-team border-entity-team/30',
    card: 'hover:border-entity-team/50 hover:shadow-entity-team/20',
    dot: 'bg-entity-team',
    accent: 'bg-entity-team',
    gradient: 'from-entity-team/15 via-transparent to-transparent',
    border: 'border-entity-team/30',
    glow: 'shadow-entity-team/20',
  },
  error_pattern: {
    badge: 'bg-entity-error-pattern/20 text-entity-error-pattern border-entity-error-pattern/30',
    card: 'hover:border-entity-error-pattern/50 hover:shadow-entity-error-pattern/20',
    dot: 'bg-entity-error-pattern',
    accent: 'bg-entity-error-pattern',
    gradient: 'from-entity-error-pattern/15 via-transparent to-transparent',
    border: 'border-entity-error-pattern/30',
    glow: 'shadow-entity-error-pattern/20',
  },
  milestone: {
    badge: 'bg-entity-milestone/20 text-entity-milestone border-entity-milestone/30',
    card: 'hover:border-entity-milestone/50 hover:shadow-entity-milestone/20',
    dot: 'bg-entity-milestone',
    accent: 'bg-entity-milestone',
    gradient: 'from-entity-milestone/15 via-transparent to-transparent',
    border: 'border-entity-milestone/30',
    glow: 'shadow-entity-milestone/20',
  },
  source: {
    badge: 'bg-entity-source/20 text-entity-source border-entity-source/30',
    card: 'hover:border-entity-source/50 hover:shadow-entity-source/20',
    dot: 'bg-entity-source',
    accent: 'bg-entity-source',
    gradient: 'from-entity-source/15 via-transparent to-transparent',
    border: 'border-entity-source/30',
    glow: 'shadow-entity-source/20',
  },
  document: {
    badge: 'bg-entity-document/20 text-entity-document border-entity-document/30',
    card: 'hover:border-entity-document/50 hover:shadow-entity-document/20',
    dot: 'bg-entity-document',
    accent: 'bg-entity-document',
    gradient: 'from-entity-document/15 via-transparent to-transparent',
    border: 'border-entity-document/30',
    glow: 'shadow-entity-document/20',
  },
  note: {
    badge: 'bg-entity-note/20 text-entity-note border-entity-note/30',
    card: 'hover:border-entity-note/50 hover:shadow-entity-note/20',
    dot: 'bg-entity-note',
    accent: 'bg-entity-note',
    gradient: 'from-entity-note/15 via-transparent to-transparent',
    border: 'border-entity-note/30',
    glow: 'shadow-entity-note/20',
  },
  concept: {
    badge: 'bg-entity-concept/20 text-entity-concept border-entity-concept/30',
    card: 'hover:border-entity-concept/50 hover:shadow-entity-concept/20',
    dot: 'bg-entity-concept',
    accent: 'bg-entity-concept',
    gradient: 'from-entity-concept/15 via-transparent to-transparent',
    border: 'border-entity-concept/30',
    glow: 'shadow-entity-concept/20',
  },
  file: {
    badge: 'bg-entity-file/20 text-entity-file border-entity-file/30',
    card: 'hover:border-entity-file/50 hover:shadow-entity-file/20',
    dot: 'bg-entity-file',
    accent: 'bg-entity-file',
    gradient: 'from-entity-file/15 via-transparent to-transparent',
    border: 'border-entity-file/30',
    glow: 'shadow-entity-file/20',
  },
  function: {
    badge: 'bg-entity-function/20 text-entity-function border-entity-function/30',
    card: 'hover:border-entity-function/50 hover:shadow-entity-function/20',
    dot: 'bg-entity-function',
    accent: 'bg-entity-function',
    gradient: 'from-entity-function/15 via-transparent to-transparent',
    border: 'border-entity-function/30',
    glow: 'shadow-entity-function/20',
  },
};

// Get color for any entity type (with fallback)
export function getEntityColor(type: string): string {
  return ENTITY_COLORS[type as EntityType] ?? DEFAULT_ENTITY_COLOR;
}

// Get style classes for any entity type (with fallback)
export function getEntityStyles(type: string) {
  return ENTITY_STYLES[type as EntityType] ?? ENTITY_STYLES.knowledge_source;
}

// Resolve the themed CSS variable reference for an entity type, e.g.
// `var(--entity-error-pattern)`. Use for inline styles (dots, rings) that need
// the color to adapt per theme without a Tailwind utility.
export function getEntityColorVar(type: string): string {
  const token = (ENTITY_TYPES as readonly string[]).includes(type)
    ? type.replace(/_/g, '-')
    : 'knowledge-source';
  return `var(--entity-${token})`;
}
