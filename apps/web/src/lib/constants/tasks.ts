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
