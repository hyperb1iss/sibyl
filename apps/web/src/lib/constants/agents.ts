// =============================================================================
// Agent Status & Type Styling
// =============================================================================

export const AGENT_STATUSES = [
  'initializing',
  'working',
  'paused',
  'waiting_approval',
  'waiting_dependency',
  'resuming',
  'completed',
  'failed',
  'terminated',
] as const;
export type AgentStatusType = (typeof AGENT_STATUSES)[number];

export const AGENT_STATUS_CONFIG: Record<
  AgentStatusType,
  { label: string; color: string; bgClass: string; textClass: string; icon: string }
> = {
  initializing: {
    label: 'Initializing',
    color: '#80ffea',
    bgClass: 'bg-[#80ffea]/20',
    textClass: 'text-[#80ffea]',
    icon: '○',
  },
  working: {
    label: 'Working',
    color: '#e135ff',
    bgClass: 'bg-[#e135ff]/20',
    textClass: 'text-[#e135ff]',
    icon: '◉',
  },
  paused: {
    label: 'Paused',
    color: '#f1fa8c',
    bgClass: 'bg-[#f1fa8c]/20',
    textClass: 'text-[#f1fa8c]',
    icon: '‖',
  },
  waiting_approval: {
    label: 'Awaiting Approval',
    color: '#ff6ac1',
    bgClass: 'bg-[#ff6ac1]/20',
    textClass: 'text-[#ff6ac1]',
    icon: '◈',
  },
  waiting_dependency: {
    label: 'Waiting',
    color: '#ffb86c',
    bgClass: 'bg-[#ffb86c]/20',
    textClass: 'text-[#ffb86c]',
    icon: '◇',
  },
  resuming: {
    label: 'Resuming',
    color: '#80ffea',
    bgClass: 'bg-[#80ffea]/20',
    textClass: 'text-[#80ffea]',
    icon: '↻',
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
  terminated: {
    label: 'Terminated',
    color: '#8b85a0',
    bgClass: 'bg-[#8b85a0]/20',
    textClass: 'text-[#8b85a0]',
    icon: '⊘',
  },
};

export const AGENT_TYPES = [
  'general',
  'planner',
  'implementer',
  'tester',
  'reviewer',
  'integrator',
  'orchestrator',
] as const;
export type AgentTypeValue = (typeof AGENT_TYPES)[number];

export const AGENT_TYPE_CONFIG: Record<
  AgentTypeValue,
  { label: string; color: string; icon: string }
> = {
  general: { label: 'General', color: '#80ffea', icon: '◎' },
  planner: { label: 'Planner', color: '#e135ff', icon: '◇' },
  implementer: { label: 'Implementer', color: '#50fa7b', icon: '◉' },
  tester: { label: 'Tester', color: '#f1fa8c', icon: '⚙' },
  reviewer: { label: 'Reviewer', color: '#ff6ac1', icon: '◈' },
  integrator: { label: 'Integrator', color: '#ffb86c', icon: '⬡' },
  orchestrator: { label: 'Orchestrator', color: '#ff6363', icon: '◆' },
};
