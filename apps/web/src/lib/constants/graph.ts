// =============================================================================
// Graph Visualization Constants
// =============================================================================

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
