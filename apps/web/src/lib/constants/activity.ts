// =============================================================================
// Team Activity: windows, kinds, roles
// =============================================================================

import {
  CheckCircle2,
  ClipboardCheck,
  Cube,
  Database,
  EditPencil,
  GitBranch,
  type IconComponent,
  PlusCircle,
  Settings,
  Star,
  User,
} from '@/components/ui/icons';
import type { TeamActivityCounts, TeamActivityKind, TeamActivityWindow } from '@/lib/api/activity';

export const TEAM_ACTIVITY_WINDOWS: readonly TeamActivityWindow[] = ['24h', '7d', '30d'];
export const DEFAULT_TEAM_ACTIVITY_WINDOW: TeamActivityWindow = '7d';
/** The server caps the feed at this many items. */
export const TEAM_ACTIVITY_RECENT_LIMIT = 100;

export const TEAM_ACTIVITY_WINDOW_CONFIG: Record<
  TeamActivityWindow,
  { label: string; phrase: string }
> = {
  '24h': { label: '24h', phrase: 'the last 24 hours' },
  '7d': { label: '7 days', phrase: 'the last 7 days' },
  '30d': { label: '30 days', phrase: 'the last 30 days' },
};

export function isTeamActivityWindow(value: string | null): value is TeamActivityWindow {
  return value !== null && (TEAM_ACTIVITY_WINDOWS as readonly string[]).includes(value);
}

export type TeamActivityCountKey = keyof TeamActivityCounts;

/** Display order for a person's counts, most telling first. */
export const TEAM_ACTIVITY_COUNT_KEYS: readonly TeamActivityCountKey[] = [
  'captures',
  'tasks_completed',
  'tasks_created',
  'decisions',
  'notes',
  'procedures',
  'other',
];

export function teamActivityTotal(counts: TeamActivityCounts): number {
  return TEAM_ACTIVITY_COUNT_KEYS.reduce((sum, key) => sum + (counts[key] ?? 0), 0);
}

export interface ActivityTone {
  icon: IconComponent;
  /** Text color for icons and numbers. */
  text: string;
  /** Tinted chip or tile: background plus border. */
  chip: string;
  /** Solid fill for bar segments and dots. */
  fill: string;
}

const TONES = {
  capture: {
    icon: Database,
    text: 'text-sc-purple',
    chip: 'bg-sc-purple/10 border-sc-purple/25',
    fill: 'bg-sc-purple',
  },
  taskCompleted: {
    icon: CheckCircle2,
    text: 'text-sc-green',
    chip: 'bg-sc-green/10 border-sc-green/25',
    fill: 'bg-sc-green',
  },
  taskCreated: {
    icon: PlusCircle,
    text: 'text-sc-cyan',
    chip: 'bg-sc-cyan/10 border-sc-cyan/25',
    fill: 'bg-sc-cyan',
  },
  decision: {
    icon: GitBranch,
    text: 'text-sc-coral',
    chip: 'bg-sc-coral/10 border-sc-coral/25',
    fill: 'bg-sc-coral',
  },
  note: {
    icon: EditPencil,
    text: 'text-sc-yellow',
    chip: 'bg-sc-yellow/10 border-sc-yellow/25',
    fill: 'bg-sc-yellow',
  },
  procedure: {
    icon: ClipboardCheck,
    text: 'text-sc-orange',
    chip: 'bg-sc-orange/10 border-sc-orange/25',
    fill: 'bg-sc-orange',
  },
  other: {
    icon: Cube,
    text: 'text-sc-fg-muted',
    chip: 'bg-sc-bg-highlight border-sc-fg-subtle/20',
    fill: 'bg-sc-fg-muted',
  },
} satisfies Record<string, ActivityTone>;

/** A person's counts: short chip label plus the sentence a screen reader hears. */
export const TEAM_ACTIVITY_COUNT_CONFIG: Record<
  TeamActivityCountKey,
  ActivityTone & { label: string; singular: string }
> = {
  captures: { ...TONES.capture, label: 'captures', singular: 'capture' },
  tasks_completed: { ...TONES.taskCompleted, label: 'tasks done', singular: 'task done' },
  tasks_created: { ...TONES.taskCreated, label: 'tasks created', singular: 'task created' },
  decisions: { ...TONES.decision, label: 'decisions', singular: 'decision' },
  notes: { ...TONES.note, label: 'notes', singular: 'note' },
  procedures: { ...TONES.procedure, label: 'procedures', singular: 'procedure' },
  other: { ...TONES.other, label: 'other', singular: 'other' },
};

/** Feed rows: what happened, in the same colors as the count it feeds. */
export const TEAM_ACTIVITY_KIND_CONFIG: Record<TeamActivityKind, ActivityTone & { label: string }> =
  {
    capture: { ...TONES.capture, label: 'Captured' },
    task_completed: { ...TONES.taskCompleted, label: 'Completed task' },
    task_created: { ...TONES.taskCreated, label: 'Created task' },
    decision: { ...TONES.decision, label: 'Decision' },
    note: { ...TONES.note, label: 'Note' },
    procedure: { ...TONES.procedure, label: 'Procedure' },
    entity: { ...TONES.other, label: 'Added' },
  };

export function teamActivityKindConfig(kind: string) {
  return TEAM_ACTIVITY_KIND_CONFIG[kind as TeamActivityKind] ?? TEAM_ACTIVITY_KIND_CONFIG.entity;
}

export const TEAM_ROLE_CONFIG: Record<
  string,
  { label: string; icon: IconComponent; badge: string }
> = {
  owner: {
    label: 'Owner',
    icon: Star,
    badge: 'bg-sc-yellow/10 text-sc-yellow border-sc-yellow/25',
  },
  admin: {
    label: 'Admin',
    icon: Settings,
    badge: 'bg-sc-purple/10 text-sc-purple border-sc-purple/25',
  },
  member: {
    label: 'Member',
    icon: User,
    badge: 'bg-sc-cyan/10 text-sc-cyan border-sc-cyan/25',
  },
};

export function teamRoleConfig(role: string) {
  return TEAM_ROLE_CONFIG[role] ?? { ...TEAM_ROLE_CONFIG.member, label: role || 'Member' };
}

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * "5m ago" for the last week, then a short date. Activity older than a week
 * reads better as "Sep 12" than as a full timestamp.
 */
export function formatActivityTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const seconds = Math.max(0, Math.floor((now - then) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (now - then < WEEK_MS) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(then).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/** "Today", "Yesterday", or "Mon, Oct 6", in the viewer's local time. */
export function formatActivityDay(iso: string, now: number = Date.now()): string {
  const day = startOfLocalDay(new Date(iso));
  const today = startOfLocalDay(new Date(now));
  const diffDays = Math.round((today - day) / 86_400_000);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return new Date(iso).toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}

/** Local calendar-day key for grouping, e.g. "2026-10-08". */
export function activityDayKey(iso: string): string {
  const date = new Date(iso);
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

function startOfLocalDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}
