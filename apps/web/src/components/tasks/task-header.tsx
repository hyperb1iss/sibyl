'use client';

import type { ReactNode } from 'react';
import { EditableDate, EditableSelect, EditableText } from '@/components/editable';
import { Loader2, Zap } from '@/components/ui/icons';
import { TASK_PRIORITY_CONFIG, TASK_STATUS_CONFIG } from '@/lib/constants';
import {
  priorityOptions,
  STATUS_FLOW,
  STATUS_ICONS,
  statusOptions,
  type TaskDetailContext,
} from './task-detail-types';

interface TaskHeaderProps extends TaskDetailContext {
  feature: string | undefined;
  dueDate: string | undefined;
  isOverdue: boolean;
  children?: ReactNode;
}

/**
 * Task header with progress bar, status/priority badges, title, and description.
 */
export function TaskHeader({
  task,
  status,
  priority,
  feature,
  dueDate,
  isOverdue,
  updateField,
  handleStatusChange,
  isUpdating,
  children,
}: TaskHeaderProps) {
  const statusConfig = TASK_STATUS_CONFIG[status];
  const priorityConfig = TASK_PRIORITY_CONFIG[priority];
  const currentStatusIndex = STATUS_FLOW.indexOf(status);

  return (
    <div className="bg-gradient-to-br from-sc-bg-base to-sc-bg-elevated border border-sc-fg-subtle/20 rounded-2xl overflow-hidden shadow-xl shadow-black/20">
      {/* Status Progress Bar */}
      <div className="relative h-1 bg-sc-bg-dark">
        <div
          className="absolute inset-y-0 left-0 bg-gradient-to-r from-sc-purple via-sc-cyan to-sc-green transition-all duration-500 ease-out"
          style={{
            width: `${((currentStatusIndex + 1) / STATUS_FLOW.length) * 100}%`,
            opacity: status === 'blocked' ? 0.4 : 1,
          }}
        />
        {status === 'blocked' && <div className="absolute inset-0 bg-sc-red/50 animate-pulse" />}
      </div>

      {/* Header Section */}
      <div className="p-6 pb-4">
        {/* Top Row: Status + Priority + Feature + Due Date */}
        <div className="flex items-center gap-2 flex-wrap mb-4">
          {/* Status */}
          <EditableSelect
            value={status}
            options={statusOptions}
            onSave={handleStatusChange}
            renderValue={opt => (
              <span
                className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium ${statusConfig.bgClass} ${statusConfig.textClass} border border-current/20`}
              >
                {isUpdating ? (
                  <Loader2 width={14} height={14} className="animate-spin" />
                ) : (
                  STATUS_ICONS[status]
                )}
                {opt?.label}
              </span>
            )}
          />

          {/* Priority */}
          <EditableSelect
            value={priority}
            options={priorityOptions}
            onSave={v => updateField('priority', v)}
            renderValue={opt => (
              <span
                className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${priorityConfig.bgClass} ${priorityConfig.textClass}`}
              >
                <Zap width={12} height={12} />
                {opt?.label}
              </span>
            )}
          />

          {/* Feature */}
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-sc-purple/10 text-sc-purple border border-sc-purple/20">
            <EditableText
              value={feature || ''}
              onSave={v => updateField('feature', v || undefined)}
              placeholder="+ feature"
              className="text-xs"
            />
          </span>

          {/* Due Date */}
          <span
            className={`inline-flex items-center rounded-full text-xs font-medium px-2.5 py-1 ${
              isOverdue
                ? 'bg-sc-red/10 text-sc-red border border-sc-red/20'
                : dueDate
                  ? 'bg-sc-fg-subtle/10 text-sc-fg-muted'
                  : ''
            }`}
          >
            <EditableDate
              value={dueDate}
              onSave={v => updateField('due_date', v)}
              placeholder="+ due date"
              showIcon={!dueDate}
            />
          </span>
        </div>

        {/* Title */}
        <h1 className="text-2xl font-bold text-sc-fg-primary mb-2 leading-tight">
          <EditableText
            value={task.name}
            onSave={v => updateField('name', v, false)}
            placeholder="Task name"
            required
            className="text-2xl font-bold"
          />
        </h1>

        {/* Description */}
        <div className="text-sc-fg-muted leading-relaxed">
          <EditableText
            value={task.description || ''}
            onSave={v => updateField('description', v || undefined, false)}
            placeholder="Add a description..."
          />
        </div>
      </div>

      {/* Quick Actions (passed as children) */}
      {children}
    </div>
  );
}
