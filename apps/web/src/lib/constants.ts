// =============================================================================
// SilkCircuit Design System Constants
// =============================================================================

// Application configuration
export const APP_CONFIG = {
  VERSION: '0.1.0',
  NAME: 'Sibyl',
  TAGLINE: 'Knowledge Oracle',
} as const;

// Timing constants (in milliseconds)
export const TIMING = {
  REFETCH_DELAY: 2000,
  HEALTH_CHECK_INTERVAL: 30000,
  STATS_REFRESH_INTERVAL: 30000,
  STALE_TIME: 60000, // 1 minute stale time for React Query
} as const;

// Cluster colors palette - for coloring nodes by cluster membership
// Uses distinct, visually separable colors that work on dark background
export const CLUSTER_COLORS = [
  '#e135ff', // Electric Purple
  '#80ffea', // Neon Cyan
  '#ff6ac1', // Coral
  '#f1fa8c', // Electric Yellow
  '#50fa7b', // Success Green
  '#ff9580', // Warm Orange
  '#bd93f9', // Soft Purple
  '#8be9fd', // Light Cyan
  '#ffb86c', // Orange
  '#ff79c6', // Pink
  '#6272a4', // Muted Blue
  '#44475a', // Dark Gray (for unclustered)
] as const;

// Get cluster color by index (cycles through palette)
export function getClusterColor(clusterId: string, clusterIndex: number): string {
  if (clusterId === 'unclustered') return CLUSTER_COLORS[11]; // Dark gray for unclustered
  return CLUSTER_COLORS[clusterIndex % (CLUSTER_COLORS.length - 1)];
}

// Graph visualization defaults
export const GRAPH_DEFAULTS = {
  MAX_NODES: 1000, // Increased for hierarchical view
  MAX_EDGES: 5000, // Increased for hierarchical view
  // Node sizing
  NODE_SIZE_MIN: 3,
  NODE_SIZE_MAX: 10,
  NODE_SIZE_SELECTED: 12,
  NODE_SIZE_HIGHLIGHTED: 11,
  // Force simulation
  CHARGE_STRENGTH: -80, // Negative = repulsion (default -30)
  LINK_DISTANCE: 60, // Distance between connected nodes
  CENTER_STRENGTH: 0.05, // Pull toward center
  COLLISION_RADIUS: 15, // Prevent node overlap
  // Simulation timing
  WARMUP_TICKS: 100,
  COOLDOWN_TICKS: 200,
  ALPHA_DECAY: 0.015, // Slower decay = more stable layout
  VELOCITY_DECAY: 0.25, // Lower = more momentum
  // Initial view
  INITIAL_ZOOM: 1.2,
  FIT_PADDING: 60,
  // Labels
  LABEL_SIZE_MIN: 2,
  LABEL_SIZE_MAX: 4,
} as const;

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

// =============================================================================
// Task Status & Priority Styling
// =============================================================================

export const TASK_STATUSES = ['backlog', 'todo', 'doing', 'blocked', 'review', 'done'] as const;
export type TaskStatusType = (typeof TASK_STATUSES)[number];

export const TASK_STATUS_CONFIG: Record<
  TaskStatusType,
  { label: string; color: string; bgClass: string; textClass: string; icon: string }
> = {
  backlog: {
    label: 'Backlog',
    color: '#8b85a0',
    bgClass: 'bg-[#8b85a0]/20',
    textClass: 'text-[#8b85a0]',
    icon: '◇',
  },
  todo: {
    label: 'Todo',
    color: '#80ffea',
    bgClass: 'bg-[#80ffea]/20',
    textClass: 'text-[#80ffea]',
    icon: '○',
  },
  doing: {
    label: 'Doing',
    color: '#e135ff',
    bgClass: 'bg-[#e135ff]/20',
    textClass: 'text-[#e135ff]',
    icon: '◉',
  },
  blocked: {
    label: 'Blocked',
    color: '#ff6363',
    bgClass: 'bg-[#ff6363]/20',
    textClass: 'text-[#ff6363]',
    icon: '⊘',
  },
  review: {
    label: 'Review',
    color: '#f1fa8c',
    bgClass: 'bg-[#f1fa8c]/20',
    textClass: 'text-[#f1fa8c]',
    icon: '◈',
  },
  done: {
    label: 'Done',
    color: '#50fa7b',
    bgClass: 'bg-[#50fa7b]/20',
    textClass: 'text-[#50fa7b]',
    icon: '◆',
  },
};

export const TASK_PRIORITIES = ['critical', 'high', 'medium', 'low', 'someday'] as const;

// =============================================================================
// Epic Status Styling
// =============================================================================

export const EPIC_STATUSES = [
  'planning',
  'in_progress',
  'blocked',
  'completed',
  'archived',
] as const;
export type EpicStatusType = (typeof EPIC_STATUSES)[number];

export const EPIC_STATUS_CONFIG: Record<
  EpicStatusType,
  { label: string; color: string; bgClass: string; textClass: string; icon: string }
> = {
  planning: {
    label: 'Planning',
    color: '#80ffea',
    bgClass: 'bg-[#80ffea]/20',
    textClass: 'text-[#80ffea]',
    icon: '◇',
  },
  in_progress: {
    label: 'In Progress',
    color: '#e135ff',
    bgClass: 'bg-[#e135ff]/20',
    textClass: 'text-[#e135ff]',
    icon: '◉',
  },
  blocked: {
    label: 'Blocked',
    color: '#ff6363',
    bgClass: 'bg-[#ff6363]/20',
    textClass: 'text-[#ff6363]',
    icon: '⊘',
  },
  completed: {
    label: 'Completed',
    color: '#50fa7b',
    bgClass: 'bg-[#50fa7b]/20',
    textClass: 'text-[#50fa7b]',
    icon: '◆',
  },
  archived: {
    label: 'Archived',
    color: '#8b85a0',
    bgClass: 'bg-[#8b85a0]/20',
    textClass: 'text-[#8b85a0]',
    icon: '▣',
  },
};
export type TaskPriorityType = (typeof TASK_PRIORITIES)[number];

export const TASK_PRIORITY_CONFIG: Record<
  TaskPriorityType,
  { label: string; color: string; bgClass: string; textClass: string }
> = {
  critical: {
    label: 'Critical',
    color: '#ff6363',
    bgClass: 'bg-[#ff6363]/20',
    textClass: 'text-[#ff6363]',
  },
  high: {
    label: 'High',
    color: '#ff6ac1',
    bgClass: 'bg-[#ff6ac1]/20',
    textClass: 'text-[#ff6ac1]',
  },
  medium: {
    label: 'Medium',
    color: '#f1fa8c',
    bgClass: 'bg-[#f1fa8c]/20',
    textClass: 'text-[#f1fa8c]',
  },
  low: {
    label: 'Low',
    color: '#80ffea',
    bgClass: 'bg-[#80ffea]/20',
    textClass: 'text-[#80ffea]',
  },
  someday: {
    label: 'Someday',
    color: '#8b85a0',
    bgClass: 'bg-[#8b85a0]/20',
    textClass: 'text-[#8b85a0]',
  },
};

// =============================================================================
// Source/Crawl Status Styling
// =============================================================================

export const CRAWL_STATUSES = ['pending', 'in_progress', 'completed', 'failed', 'partial'] as const;
export type CrawlStatusType = (typeof CRAWL_STATUSES)[number];

export const CRAWL_STATUS_CONFIG: Record<
  CrawlStatusType,
  { label: string; color: string; bgClass: string; textClass: string; icon: string }
> = {
  pending: {
    label: 'Pending',
    color: '#8b85a0',
    bgClass: 'bg-[#8b85a0]/20',
    textClass: 'text-[#8b85a0]',
    icon: '○',
  },
  in_progress: {
    label: 'Crawling',
    color: '#e135ff',
    bgClass: 'bg-[#e135ff]/20',
    textClass: 'text-[#e135ff]',
    icon: '◉',
  },
  completed: {
    label: 'Completed',
    color: '#50fa7b',
    bgClass: 'bg-[#50fa7b]/20',
    textClass: 'text-[#50fa7b]',
    icon: '◆',
  },
  failed: {
    label: 'Failed',
    color: '#ff6363',
    bgClass: 'bg-[#ff6363]/20',
    textClass: 'text-[#ff6363]',
    icon: '✕',
  },
  partial: {
    label: 'Partial',
    color: '#f1fa8c',
    bgClass: 'bg-[#f1fa8c]/20',
    textClass: 'text-[#f1fa8c]',
    icon: '◈',
  },
};

export const SOURCE_TYPES = ['website', 'github', 'local', 'api_docs'] as const;
export type SourceTypeValue = (typeof SOURCE_TYPES)[number];

export const SOURCE_TYPE_CONFIG: Record<SourceTypeValue, { label: string; icon: string }> = {
  website: { label: 'Website', icon: '⊕' },
  github: { label: 'GitHub', icon: '◈' },
  local: { label: 'Local', icon: '◇' },
  api_docs: { label: 'API Docs', icon: '⚙' },
};

// Navigation items for sidebar (legacy - use COMMAND_NAV in command-palette.tsx)
export const NAVIGATION = [
  { name: 'Dashboard', href: '/', icon: '◆' },
  { name: 'Projects', href: '/projects', icon: '◇' },
  { name: 'Tasks', href: '/tasks', icon: '☰' },
  { name: 'Sources', href: '/sources', icon: '▤' },
  { name: 'Graph', href: '/graph', icon: '⬡' },
  { name: 'Entities', href: '/entities', icon: '▣' },
  { name: 'Search', href: '/search', icon: '⌕' },
] as const;

// Quick actions for dashboard
export const QUICK_ACTIONS = [
  { label: 'Explore Graph', href: '/graph', icon: '⬡', color: 'purple' as const },
  { label: 'Browse Entities', href: '/entities', icon: '▣', color: 'cyan' as const },
  { label: 'Search Knowledge', href: '/search', icon: '⌕', color: 'coral' as const },
  { label: 'Add Source', href: '/sources', icon: '▤', color: 'yellow' as const },
] as const;

// =============================================================================
// Relationship Type Styling
// =============================================================================

export const RELATIONSHIP_TYPES = [
  'APPLIES_TO',
  'REQUIRES',
  'CONFLICTS_WITH',
  'SUPERSEDES',
  'ENABLES',
  'BREAKS',
  'BELONGS_TO',
  'DEPENDS_ON',
  'BLOCKS',
  'ASSIGNED_TO',
  'REFERENCES',
  'MENTIONS',
  'ENCOUNTERED',
  'RELATED_TO',
] as const;

export type RelationshipType = (typeof RELATIONSHIP_TYPES)[number];

export const RELATIONSHIP_CONFIG: Record<string, { color: string; label: string; icon: string }> = {
  APPLIES_TO: { color: '#e135ff', label: 'Applies to', icon: '→' },
  REQUIRES: { color: '#80ffea', label: 'Requires', icon: '←' },
  CONFLICTS_WITH: { color: '#ff6363', label: 'Conflicts', icon: '⊗' },
  SUPERSEDES: { color: '#f1fa8c', label: 'Supersedes', icon: '↑' },
  ENABLES: { color: '#50fa7b', label: 'Enables', icon: '⚡' },
  BREAKS: { color: '#ff6363', label: 'Breaks', icon: '✕' },
  BELONGS_TO: { color: '#ff6ac1', label: 'Belongs to', icon: '⊂' },
  DEPENDS_ON: { color: '#80ffea', label: 'Depends on', icon: '⟵' },
  BLOCKS: { color: '#ff6363', label: 'Blocks', icon: '⊘' },
  ASSIGNED_TO: { color: '#e135ff', label: 'Assigned to', icon: '◎' },
  REFERENCES: { color: '#8b85a0', label: 'References', icon: '↗' },
  MENTIONS: { color: '#8b85a0', label: 'Mentions', icon: '↗' },
  ENCOUNTERED: { color: '#ffb86c', label: 'Encountered', icon: '◈' },
  RELATED_TO: { color: '#8b85a0', label: 'Related', icon: '↔' },
};

// Get relationship config with fallback
export function getRelationshipConfig(type: string) {
  return (
    RELATIONSHIP_CONFIG[type.toUpperCase()] ?? {
      color: '#8b85a0',
      label: type.replace(/_/g, ' ').toLowerCase(),
      icon: '↔',
    }
  );
}

// =============================================================================
// Utility Functions
// =============================================================================

export function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export function formatDateTime(date: string | Date): string {
  return new Date(date).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}
