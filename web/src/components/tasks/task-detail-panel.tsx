'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { EntityBadge } from '@/components/ui/badge';
import {
  TASK_STATUS_CONFIG,
  TASK_PRIORITY_CONFIG,
  type TaskStatusType,
  type TaskPriorityType,
  formatDateTime,
} from '@/lib/constants';
import type { Entity, TaskStatus } from '@/lib/api';
import { useTaskUpdateStatus } from '@/lib/hooks';

interface TaskDetailPanelProps {
  task: Entity;
  onClose?: () => void;
  relatedKnowledge?: Array<{
    id: string;
    type: string;
    name: string;
    relationship: string;
  }>;
}

export function TaskDetailPanel({ task, onClose, relatedKnowledge = [] }: TaskDetailPanelProps) {
  const updateStatus = useTaskUpdateStatus();
  const [isEditing, setIsEditing] = useState(false);

  const status = (task.metadata.status as TaskStatusType) || 'backlog';
  const priority = (task.metadata.priority as TaskPriorityType) || 'medium';
  const statusConfig = TASK_STATUS_CONFIG[status];
  const priorityConfig = TASK_PRIORITY_CONFIG[priority];

  const assignees = (task.metadata.assignees as string[]) || [];
  const feature = task.metadata.feature as string | undefined;
  const projectId = task.metadata.project_id as string | undefined;
  const branchName = task.metadata.branch_name as string | undefined;
  const prUrl = task.metadata.pr_url as string | undefined;
  const estimatedHours = task.metadata.estimated_hours as number | undefined;
  const actualHours = task.metadata.actual_hours as number | undefined;
  const technologies = (task.metadata.technologies as string[]) || [];
  const blockerReason = task.metadata.blocker_reason as string | undefined;

  // Status progression buttons
  const getNextStatuses = (current: TaskStatusType): TaskStatus[] => {
    const transitions: Record<TaskStatusType, TaskStatus[]> = {
      backlog: ['todo'],
      todo: ['doing'],
      doing: ['blocked', 'review'],
      blocked: ['doing'],
      review: ['doing', 'done'],
      done: [],
    };
    return transitions[current] || [];
  };

  const handleStatusChange = async (newStatus: TaskStatus) => {
    await updateStatus.mutateAsync({ id: task.id, status: newStatus });
  };

  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl overflow-hidden">
      {/* Header */}
      <div className="p-6 border-b border-sc-fg-subtle/10">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-2">
              <EntityBadge type="task" />
              {feature && (
                <span className="text-xs px-2 py-0.5 rounded bg-sc-purple/20 text-sc-purple border border-sc-purple/30">
                  {feature}
                </span>
              )}
            </div>
            <h2 className="text-xl font-semibold text-sc-fg-primary mb-1">{task.name}</h2>
            <p className="text-sm text-sc-fg-muted">{task.description}</p>
          </div>
          {onClose && (
            <button
              onClick={onClose}
              className="text-sc-fg-subtle hover:text-sc-fg-primary transition-colors p-1"
              aria-label="Close"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* Status & Priority Bar */}
      <div className="px-6 py-4 bg-sc-bg-elevated/50 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-4">
          {/* Current Status */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-sc-fg-subtle uppercase">Status</span>
            <span className={`px-2 py-1 rounded text-sm font-medium ${statusConfig.bgClass} ${statusConfig.textClass}`}>
              {statusConfig.icon} {statusConfig.label}
            </span>
          </div>

          {/* Priority */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-sc-fg-subtle uppercase">Priority</span>
            <span className={`px-2 py-1 rounded text-sm ${priorityConfig.bgClass} ${priorityConfig.textClass}`}>
              {priorityConfig.label}
            </span>
          </div>
        </div>

        {/* Status Actions */}
        <div className="flex items-center gap-2">
          {getNextStatuses(status).map((nextStatus) => {
            const nextConfig = TASK_STATUS_CONFIG[nextStatus as TaskStatusType];
            return (
              <Button
                key={nextStatus}
                size="sm"
                variant="secondary"
                onClick={() => handleStatusChange(nextStatus)}
                disabled={updateStatus.isPending}
                className={`${nextConfig.textClass} hover:${nextConfig.bgClass}`}
              >
                {nextConfig.icon} Move to {nextConfig.label}
              </Button>
            );
          })}
        </div>
      </div>

      {/* Blocker Alert */}
      {status === 'blocked' && blockerReason && (
        <div className="mx-6 mt-4 p-3 bg-[#ff6363]/10 border border-[#ff6363]/30 rounded-lg">
          <div className="flex items-start gap-2">
            <span className="text-[#ff6363]">⊘</span>
            <div>
              <span className="text-sm font-medium text-[#ff6363]">Blocked</span>
              <p className="text-sm text-sc-fg-muted mt-1">{blockerReason}</p>
            </div>
          </div>
        </div>
      )}

      {/* Content Grid */}
      <div className="p-6 grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Content */}
        <div className="lg:col-span-2 space-y-6">
          {/* Full Content/Details */}
          {task.content && (
            <section>
              <h3 className="text-sm font-semibold text-sc-fg-muted mb-3">Details</h3>
              <div className="prose prose-sm prose-invert max-w-none">
                <p className="text-sc-fg-primary whitespace-pre-wrap">{task.content}</p>
              </div>
            </section>
          )}

          {/* Technologies */}
          {technologies.length > 0 && (
            <section>
              <h3 className="text-sm font-semibold text-sc-fg-muted mb-3">Technologies</h3>
              <div className="flex flex-wrap gap-2">
                {technologies.map((tech) => (
                  <span
                    key={tech}
                    className="px-2 py-1 text-xs rounded bg-sc-bg-elevated border border-sc-fg-subtle/20 text-sc-fg-primary"
                  >
                    {tech}
                  </span>
                ))}
              </div>
            </section>
          )}

          {/* Related Knowledge */}
          {relatedKnowledge.length > 0 && (
            <section>
              <h3 className="text-sm font-semibold text-sc-fg-muted mb-3">Linked Knowledge</h3>
              <div className="space-y-2">
                {relatedKnowledge.map((item) => (
                  <Link
                    key={item.id}
                    href={`/entities/${item.id}`}
                    className="flex items-center gap-3 p-3 bg-sc-bg-elevated rounded-lg border border-sc-fg-subtle/10 hover:border-sc-purple/30 transition-colors"
                  >
                    <EntityBadge type={item.type} size="sm" />
                    <div className="flex-1 min-w-0">
                      <span className="text-sm text-sc-fg-primary truncate block">{item.name}</span>
                      <span className="text-xs text-sc-fg-subtle">{item.relationship}</span>
                    </div>
                  </Link>
                ))}
              </div>
            </section>
          )}
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* Metadata */}
          <section className="p-4 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10">
            <h3 className="text-sm font-semibold text-sc-fg-muted mb-4">Details</h3>
            <dl className="space-y-3 text-sm">
              {assignees.length > 0 && (
                <div className="flex justify-between">
                  <dt className="text-sc-fg-subtle">Assignees</dt>
                  <dd className="text-sc-fg-primary">{assignees.join(', ')}</dd>
                </div>
              )}
              {projectId && (
                <div className="flex justify-between">
                  <dt className="text-sc-fg-subtle">Project</dt>
                  <dd>
                    <Link href={`/projects?id=${projectId}`} className="text-sc-purple hover:underline">
                      View Project
                    </Link>
                  </dd>
                </div>
              )}
              {estimatedHours !== undefined && (
                <div className="flex justify-between">
                  <dt className="text-sc-fg-subtle">Estimated</dt>
                  <dd className="text-sc-fg-primary">{estimatedHours}h</dd>
                </div>
              )}
              {actualHours !== undefined && (
                <div className="flex justify-between">
                  <dt className="text-sc-fg-subtle">Actual</dt>
                  <dd className="text-sc-fg-primary">{actualHours}h</dd>
                </div>
              )}
              {task.created_at && (
                <div className="flex justify-between">
                  <dt className="text-sc-fg-subtle">Created</dt>
                  <dd className="text-sc-fg-primary">{formatDateTime(task.created_at)}</dd>
                </div>
              )}
              {task.updated_at && (
                <div className="flex justify-between">
                  <dt className="text-sc-fg-subtle">Updated</dt>
                  <dd className="text-sc-fg-primary">{formatDateTime(task.updated_at)}</dd>
                </div>
              )}
            </dl>
          </section>

          {/* Git Info */}
          {(branchName || prUrl) && (
            <section className="p-4 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10">
              <h3 className="text-sm font-semibold text-sc-fg-muted mb-4">Development</h3>
              <dl className="space-y-3 text-sm">
                {branchName && (
                  <div>
                    <dt className="text-sc-fg-subtle mb-1">Branch</dt>
                    <dd className="font-mono text-xs bg-sc-bg-dark px-2 py-1 rounded text-sc-cyan truncate">
                      {branchName}
                    </dd>
                  </div>
                )}
                {prUrl && (
                  <div>
                    <dt className="text-sc-fg-subtle mb-1">Pull Request</dt>
                    <dd>
                      <a
                        href={prUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sc-purple hover:underline text-sm flex items-center gap-1"
                      >
                        View PR <span className="text-xs">↗</span>
                      </a>
                    </dd>
                  </div>
                )}
              </dl>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

export function TaskDetailSkeleton() {
  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl overflow-hidden animate-pulse">
      <div className="p-6 border-b border-sc-fg-subtle/10">
        <div className="h-4 bg-sc-fg-subtle/10 rounded w-1/4 mb-3" />
        <div className="h-6 bg-sc-fg-subtle/10 rounded w-3/4 mb-2" />
        <div className="h-4 bg-sc-fg-subtle/10 rounded w-1/2" />
      </div>
      <div className="px-6 py-4 bg-sc-bg-elevated/50">
        <div className="h-8 bg-sc-fg-subtle/10 rounded w-1/3" />
      </div>
      <div className="p-6 grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-4">
          <div className="h-24 bg-sc-fg-subtle/10 rounded" />
          <div className="h-16 bg-sc-fg-subtle/10 rounded" />
        </div>
        <div className="h-48 bg-sc-fg-subtle/10 rounded" />
      </div>
    </div>
  );
}
