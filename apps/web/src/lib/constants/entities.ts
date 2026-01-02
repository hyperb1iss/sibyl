// =============================================================================
// Entity Types & Styling
// =============================================================================

// Entity types supported by Sibyl
export const ENTITY_TYPES = [
  'pattern',
  'rule',
  'template',
  'convention',
  'tool',
  'language',
  'topic',
  'episode',
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
  'concept', // Generic extracted entities
  'file', // File paths
  'function', // Functions/methods
] as const;

export type EntityType = (typeof ENTITY_TYPES)[number];

// Entity colors - the soul of SilkCircuit
// Each type has a distinct color for visual identification
export const ENTITY_COLORS: Record<EntityType, string> = {
  pattern: '#e135ff', // Electric Purple
  rule: '#ff6363', // Error Red
  template: '#80ffea', // Neon Cyan
  convention: '#ffb86c', // Orange
  tool: '#f1fa8c', // Electric Yellow
  language: '#ff6ac1', // Coral
  topic: '#ff00ff', // Magenta
  episode: '#50fa7b', // Success Green
  knowledge_source: '#8b85a0', // Muted
  config_file: '#bd93f9', // Soft Purple
  slash_command: '#8be9fd', // Light Cyan
  task: '#e135ff', // Electric Purple (work items)
  project: '#ff79c6', // Bright Pink (distinct from others!)
  epic: '#ffb86c', // Orange
  team: '#ff6ac1', // Coral
  error_pattern: '#ff6363', // Error Red
  milestone: '#f1fa8c', // Electric Yellow
  source: '#ff9580', // Warm Orange
  document: '#6272a4', // Muted Blue
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
  convention: '§',
  tool: '⚙',
  language: '⟨⟩',
  topic: '●',
  episode: '◉',
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

// Pre-computed Tailwind class combinations for badges and cards
export const ENTITY_STYLES: Record<EntityType, EntityStyle> = {
  pattern: {
    badge: 'bg-[#e135ff]/20 text-[#e135ff] border-[#e135ff]/30',
    card: 'hover:border-[#e135ff]/50 hover:shadow-[#e135ff]/20',
    dot: 'bg-[#e135ff]',
    accent: 'bg-[#e135ff]',
    gradient: 'from-[#e135ff]/15 via-transparent to-transparent',
    border: 'border-[#e135ff]/30',
    glow: 'shadow-[#e135ff]/20',
  },
  rule: {
    badge: 'bg-[#ff6363]/20 text-[#ff6363] border-[#ff6363]/30',
    card: 'hover:border-[#ff6363]/50 hover:shadow-[#ff6363]/20',
    dot: 'bg-[#ff6363]',
    accent: 'bg-[#ff6363]',
    gradient: 'from-[#ff6363]/15 via-transparent to-transparent',
    border: 'border-[#ff6363]/30',
    glow: 'shadow-[#ff6363]/20',
  },
  template: {
    badge: 'bg-[#80ffea]/20 text-[#80ffea] border-[#80ffea]/30',
    card: 'hover:border-[#80ffea]/50 hover:shadow-[#80ffea]/20',
    dot: 'bg-[#80ffea]',
    accent: 'bg-[#80ffea]',
    gradient: 'from-[#80ffea]/15 via-transparent to-transparent',
    border: 'border-[#80ffea]/30',
    glow: 'shadow-[#80ffea]/20',
  },
  convention: {
    badge: 'bg-[#ffb86c]/20 text-[#ffb86c] border-[#ffb86c]/30',
    card: 'hover:border-[#ffb86c]/50 hover:shadow-[#ffb86c]/20',
    dot: 'bg-[#ffb86c]',
    accent: 'bg-[#ffb86c]',
    gradient: 'from-[#ffb86c]/15 via-transparent to-transparent',
    border: 'border-[#ffb86c]/30',
    glow: 'shadow-[#ffb86c]/20',
  },
  tool: {
    badge: 'bg-[#f1fa8c]/20 text-[#f1fa8c] border-[#f1fa8c]/30',
    card: 'hover:border-[#f1fa8c]/50 hover:shadow-[#f1fa8c]/20',
    dot: 'bg-[#f1fa8c]',
    accent: 'bg-[#f1fa8c]',
    gradient: 'from-[#f1fa8c]/15 via-transparent to-transparent',
    border: 'border-[#f1fa8c]/30',
    glow: 'shadow-[#f1fa8c]/20',
  },
  language: {
    badge: 'bg-[#ff6ac1]/20 text-[#ff6ac1] border-[#ff6ac1]/30',
    card: 'hover:border-[#ff6ac1]/50 hover:shadow-[#ff6ac1]/20',
    dot: 'bg-[#ff6ac1]',
    accent: 'bg-[#ff6ac1]',
    gradient: 'from-[#ff6ac1]/15 via-transparent to-transparent',
    border: 'border-[#ff6ac1]/30',
    glow: 'shadow-[#ff6ac1]/20',
  },
  topic: {
    badge: 'bg-[#ff00ff]/20 text-[#ff00ff] border-[#ff00ff]/30',
    card: 'hover:border-[#ff00ff]/50 hover:shadow-[#ff00ff]/20',
    dot: 'bg-[#ff00ff]',
    accent: 'bg-[#ff00ff]',
    gradient: 'from-[#ff00ff]/15 via-transparent to-transparent',
    border: 'border-[#ff00ff]/30',
    glow: 'shadow-[#ff00ff]/20',
  },
  episode: {
    badge: 'bg-[#50fa7b]/20 text-[#50fa7b] border-[#50fa7b]/30',
    card: 'hover:border-[#50fa7b]/50 hover:shadow-[#50fa7b]/20',
    dot: 'bg-[#50fa7b]',
    accent: 'bg-[#50fa7b]',
    gradient: 'from-[#50fa7b]/15 via-transparent to-transparent',
    border: 'border-[#50fa7b]/30',
    glow: 'shadow-[#50fa7b]/20',
  },
  knowledge_source: {
    badge: 'bg-[#8b85a0]/20 text-[#8b85a0] border-[#8b85a0]/30',
    card: 'hover:border-[#8b85a0]/50 hover:shadow-[#8b85a0]/20',
    dot: 'bg-[#8b85a0]',
    accent: 'bg-[#8b85a0]',
    gradient: 'from-[#8b85a0]/15 via-transparent to-transparent',
    border: 'border-[#8b85a0]/30',
    glow: 'shadow-[#8b85a0]/20',
  },
  config_file: {
    badge: 'bg-[#f1fa8c]/20 text-[#f1fa8c] border-[#f1fa8c]/30',
    card: 'hover:border-[#f1fa8c]/50 hover:shadow-[#f1fa8c]/20',
    dot: 'bg-[#f1fa8c]',
    accent: 'bg-[#f1fa8c]',
    gradient: 'from-[#f1fa8c]/15 via-transparent to-transparent',
    border: 'border-[#f1fa8c]/30',
    glow: 'shadow-[#f1fa8c]/20',
  },
  slash_command: {
    badge: 'bg-[#80ffea]/20 text-[#80ffea] border-[#80ffea]/30',
    card: 'hover:border-[#80ffea]/50 hover:shadow-[#80ffea]/20',
    dot: 'bg-[#80ffea]',
    accent: 'bg-[#80ffea]',
    gradient: 'from-[#80ffea]/15 via-transparent to-transparent',
    border: 'border-[#80ffea]/30',
    glow: 'shadow-[#80ffea]/20',
  },
  task: {
    badge: 'bg-[#e135ff]/20 text-[#e135ff] border-[#e135ff]/30',
    card: 'hover:border-[#e135ff]/50 hover:shadow-[#e135ff]/20',
    dot: 'bg-[#e135ff]',
    accent: 'bg-[#e135ff]',
    gradient: 'from-[#e135ff]/15 via-transparent to-transparent',
    border: 'border-[#e135ff]/30',
    glow: 'shadow-[#e135ff]/20',
  },
  project: {
    badge: 'bg-[#80ffea]/20 text-[#80ffea] border-[#80ffea]/30',
    card: 'hover:border-[#80ffea]/50 hover:shadow-[#80ffea]/20',
    dot: 'bg-[#80ffea]',
    accent: 'bg-[#80ffea]',
    gradient: 'from-[#80ffea]/15 via-transparent to-transparent',
    border: 'border-[#80ffea]/30',
    glow: 'shadow-[#80ffea]/20',
  },
  epic: {
    badge: 'bg-[#ffb86c]/20 text-[#ffb86c] border-[#ffb86c]/30',
    card: 'hover:border-[#ffb86c]/50 hover:shadow-[#ffb86c]/20',
    dot: 'bg-[#ffb86c]',
    accent: 'bg-[#ffb86c]',
    gradient: 'from-[#ffb86c]/15 via-transparent to-transparent',
    border: 'border-[#ffb86c]/30',
    glow: 'shadow-[#ffb86c]/20',
  },
  team: {
    badge: 'bg-[#ff6ac1]/20 text-[#ff6ac1] border-[#ff6ac1]/30',
    card: 'hover:border-[#ff6ac1]/50 hover:shadow-[#ff6ac1]/20',
    dot: 'bg-[#ff6ac1]',
    accent: 'bg-[#ff6ac1]',
    gradient: 'from-[#ff6ac1]/15 via-transparent to-transparent',
    border: 'border-[#ff6ac1]/30',
    glow: 'shadow-[#ff6ac1]/20',
  },
  error_pattern: {
    badge: 'bg-[#ff6363]/20 text-[#ff6363] border-[#ff6363]/30',
    card: 'hover:border-[#ff6363]/50 hover:shadow-[#ff6363]/20',
    dot: 'bg-[#ff6363]',
    accent: 'bg-[#ff6363]',
    gradient: 'from-[#ff6363]/15 via-transparent to-transparent',
    border: 'border-[#ff6363]/30',
    glow: 'shadow-[#ff6363]/20',
  },
  milestone: {
    badge: 'bg-[#f1fa8c]/20 text-[#f1fa8c] border-[#f1fa8c]/30',
    card: 'hover:border-[#f1fa8c]/50 hover:shadow-[#f1fa8c]/20',
    dot: 'bg-[#f1fa8c]',
    accent: 'bg-[#f1fa8c]',
    gradient: 'from-[#f1fa8c]/15 via-transparent to-transparent',
    border: 'border-[#f1fa8c]/30',
    glow: 'shadow-[#f1fa8c]/20',
  },
  source: {
    badge: 'bg-[#ff6ac1]/20 text-[#ff6ac1] border-[#ff6ac1]/30',
    card: 'hover:border-[#ff6ac1]/50 hover:shadow-[#ff6ac1]/20',
    dot: 'bg-[#ff6ac1]',
    accent: 'bg-[#ff6ac1]',
    gradient: 'from-[#ff6ac1]/15 via-transparent to-transparent',
    border: 'border-[#ff6ac1]/30',
    glow: 'shadow-[#ff6ac1]/20',
  },
  document: {
    badge: 'bg-[#f1fa8c]/20 text-[#f1fa8c] border-[#f1fa8c]/30',
    card: 'hover:border-[#f1fa8c]/50 hover:shadow-[#f1fa8c]/20',
    dot: 'bg-[#f1fa8c]',
    accent: 'bg-[#f1fa8c]',
    gradient: 'from-[#f1fa8c]/15 via-transparent to-transparent',
    border: 'border-[#f1fa8c]/30',
    glow: 'shadow-[#f1fa8c]/20',
  },
  concept: {
    badge: 'bg-[#a8a8a8]/20 text-[#a8a8a8] border-[#a8a8a8]/30',
    card: 'hover:border-[#a8a8a8]/50 hover:shadow-[#a8a8a8]/20',
    dot: 'bg-[#a8a8a8]',
    accent: 'bg-[#a8a8a8]',
    gradient: 'from-[#a8a8a8]/15 via-transparent to-transparent',
    border: 'border-[#a8a8a8]/30',
    glow: 'shadow-[#a8a8a8]/20',
  },
  file: {
    badge: 'bg-[#61afef]/20 text-[#61afef] border-[#61afef]/30',
    card: 'hover:border-[#61afef]/50 hover:shadow-[#61afef]/20',
    dot: 'bg-[#61afef]',
    accent: 'bg-[#61afef]',
    gradient: 'from-[#61afef]/15 via-transparent to-transparent',
    border: 'border-[#61afef]/30',
    glow: 'shadow-[#61afef]/20',
  },
  function: {
    badge: 'bg-[#c678dd]/20 text-[#c678dd] border-[#c678dd]/30',
    card: 'hover:border-[#c678dd]/50 hover:shadow-[#c678dd]/20',
    dot: 'bg-[#c678dd]',
    accent: 'bg-[#c678dd]',
    gradient: 'from-[#c678dd]/15 via-transparent to-transparent',
    border: 'border-[#c678dd]/30',
    glow: 'shadow-[#c678dd]/20',
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
