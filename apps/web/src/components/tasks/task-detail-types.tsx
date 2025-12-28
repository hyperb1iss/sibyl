/**
 * Shared types and constants for TaskDetailPanel sub-components.
 */

import type { ReactNode } from 'react';
import { CheckCircle2, Circle, Pause, Play, Send, Target, Zap } from '@/components/ui/icons';
import type { Entity } from '@/lib/api';
import {
  TASK_PRIORITIES,
  TASK_PRIORITY_CONFIG,
  TASK_STATUS_CONFIG,
  TASK_STATUSES,
  type TaskPriorityType,
  type TaskStatusType,
} from '@/lib/constants';

// Status icons mapping
export const STATUS_ICONS: Record<TaskStatusType, ReactNode> = {
  backlog: <Circle width={14} height={14} />,
  todo: <Target width={14} height={14} />,
  doing: <Play width={14} height={14} />,
  blocked: <Pause width={14} height={14} />,
  review: <Send width={14} height={14} />,
  done: <CheckCircle2 width={14} height={14} />,
};

// Linear status progression
export const STATUS_FLOW: TaskStatusType[] = ['backlog', 'todo', 'doing', 'review', 'done'];

// Select options for status dropdown
export const statusOptions = TASK_STATUSES.map(s => ({
  value: s,
  label: TASK_STATUS_CONFIG[s].label,
  icon: STATUS_ICONS[s],
  color: TASK_STATUS_CONFIG[s].textClass,
}));

// Select options for priority dropdown
export const priorityOptions = TASK_PRIORITIES.map(p => ({
  value: p,
  label: TASK_PRIORITY_CONFIG[p].label,
  icon: <Zap width={14} height={14} />,
  color: TASK_PRIORITY_CONFIG[p].textClass,
}));

// Props shared across task detail sub-components
export interface TaskDetailContext {
  task: Entity;
  status: TaskStatusType;
  priority: TaskPriorityType;
  updateField: (field: string, value: unknown, metadataField?: boolean) => Promise<void>;
  handleStatusChange: (newStatus: string) => Promise<void>;
  isUpdating?: boolean;
}

// Related knowledge item type
export interface RelatedKnowledgeItem {
  id: string;
  type: string;
  name: string;
  relationship: string;
}

// Project option for select
export interface ProjectOption {
  value: string;
  label: string;
  icon: ReactNode;
}
