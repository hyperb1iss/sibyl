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
    color: 'var(--sc-cyan)',
    bgClass: 'bg-sc-cyan/20',
    textClass: 'text-sc-cyan',
    icon: '◇',
  },
  in_progress: {
    label: 'In Progress',
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
  completed: {
    label: 'Completed',
    color: 'var(--sc-green)',
    bgClass: 'bg-sc-green/20',
    textClass: 'text-sc-green',
    icon: '◆',
  },
  archived: {
    label: 'Archived',
    color: 'var(--sc-fg-subtle)',
    bgClass: 'bg-sc-fg-subtle/20',
    textClass: 'text-sc-fg-muted',
    icon: '▣',
  },
};
