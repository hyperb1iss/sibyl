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
    color: 'var(--sc-fg-subtle)',
    bgClass: 'bg-sc-fg-subtle/20',
    textClass: 'text-sc-fg-muted',
    icon: '◇',
  },
  todo: {
    label: 'Todo',
    color: 'var(--sc-cyan)',
    bgClass: 'bg-sc-cyan/20',
    textClass: 'text-sc-cyan',
    icon: '○',
  },
  doing: {
    label: 'Doing',
    color: 'var(--sc-purple)',
    bgClass: 'bg-sc-purple/20',
    textClass: 'text-sc-purple',
    icon: '◉',
  },
  blocked: {
    label: 'Blocked',
    color: 'var(--sc-red)',
    bgClass: 'bg-sc-red/20',
    textClass: 'text-sc-red',
    icon: '⊘',
  },
  review: {
    label: 'Review',
    color: 'var(--sc-yellow)',
    bgClass: 'bg-sc-yellow/20',
    textClass: 'text-sc-yellow',
    icon: '◈',
  },
  done: {
    label: 'Done',
    color: 'var(--sc-green)',
    bgClass: 'bg-sc-green/20',
    textClass: 'text-sc-green',
    icon: '◆',
  },
};

export const TASK_PRIORITIES = ['critical', 'high', 'medium', 'low', 'someday'] as const;
export type TaskPriorityType = (typeof TASK_PRIORITIES)[number];

export const TASK_PRIORITY_CONFIG: Record<
  TaskPriorityType,
  { label: string; color: string; bgClass: string; textClass: string; borderClass: string }
> = {
  critical: {
    label: 'Critical',
    color: 'var(--sc-red)',
    bgClass: 'bg-sc-red/20',
    textClass: 'text-sc-red',
    borderClass: 'border-sc-red/40',
  },
  high: {
    label: 'High',
    color: 'var(--sc-yellow)',
    bgClass: 'bg-sc-yellow/20',
    textClass: 'text-sc-yellow',
    borderClass: 'border-sc-yellow/40',
  },
  medium: {
    label: 'Medium',
    color: 'var(--sc-purple)',
    bgClass: 'bg-sc-purple/20',
    textClass: 'text-sc-purple',
    borderClass: 'border-sc-purple/40',
  },
  low: {
    label: 'Low',
    color: 'var(--sc-cyan)',
    bgClass: 'bg-sc-cyan/20',
    textClass: 'text-sc-cyan',
    borderClass: 'border-sc-cyan/40',
  },
  someday: {
    label: 'Someday',
    color: 'var(--sc-fg-subtle)',
    bgClass: 'bg-sc-fg-subtle/10',
    textClass: 'text-sc-fg-muted',
    borderClass: 'border-sc-fg-subtle/20',
  },
};
