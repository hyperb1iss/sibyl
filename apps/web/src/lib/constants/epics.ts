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
