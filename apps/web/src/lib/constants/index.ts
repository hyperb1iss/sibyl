// =============================================================================
// SilkCircuit Design System Constants
// Re-exports for backwards compatibility
// =============================================================================

// Agents
export {
  AGENT_STATUS_CONFIG,
  AGENT_STATUSES,
  AGENT_TYPE_CONFIG,
  AGENT_TYPES,
  type AgentStatusType,
  type AgentTypeValue,
} from './agents';
// Application
export { APP_CONFIG, TIMING } from './app';

// Entities
export {
  DEFAULT_ENTITY_COLOR,
  ENTITY_COLORS,
  ENTITY_ICONS,
  ENTITY_STYLES,
  ENTITY_TYPES,
  type EntityStyle,
  type EntityType,
  getEntityColor,
  getEntityStyles,
} from './entities';
// Epics
export { EPIC_STATUS_CONFIG, EPIC_STATUSES, type EpicStatusType } from './epics';
// Formatting
export { formatDateTime, formatDistanceToNow, formatUptime } from './formatting';
// Graph
export { CLUSTER_COLORS, GRAPH_DEFAULTS, getClusterColor } from './graph';
// Navigation
export { NAVIGATION, QUICK_ACTIONS } from './navigation';
// Relationships
export {
  getRelationshipConfig,
  RELATIONSHIP_CONFIG,
  RELATIONSHIP_TYPES,
  type RelationshipType,
} from './relationships';
// Sources
export {
  CRAWL_STATUS_CONFIG,
  CRAWL_STATUSES,
  type CrawlStatusType,
  SOURCE_TYPE_CONFIG,
  SOURCE_TYPES,
  type SourceTypeValue,
} from './sources';
// Tasks
export {
  TASK_PRIORITIES,
  TASK_PRIORITY_CONFIG,
  TASK_STATUS_CONFIG,
  TASK_STATUSES,
  type TaskPriorityType,
  type TaskStatusType,
} from './tasks';
