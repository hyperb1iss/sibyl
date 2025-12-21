'use client';

import {
  AlertCircle,
  ArrowRight,
  Calendar,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  Clock,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  Loader2,
  Pause,
  Play,
  Send,
  Tag,
  Target,
  Users,
  Zap,
} from 'lucide-react';
import Link from 'next/link';
import { EntityBadge } from '@/components/ui/badge';
import type { Entity, TaskStatus } from '@/lib/api';
import {
  formatDateTime,
  TASK_PRIORITY_CONFIG,
  TASK_STATUS_CONFIG,
  type TaskPriorityType,
  type TaskStatusType,
} from '@/lib/constants';
import { useTaskUpdateStatus } from '@/lib/hooks';

interface TaskDetailPanelProps {
  task: Entity;
  relatedKnowledge?: Array<{
    id: string;
    type: string;
    name: string;
    relationship: string;
  }>;
}

// Status icon mapping
const STATUS_ICONS: Record<TaskStatusType, React.ReactNode> = {
  backlog: <Circle size={16} className="text-sc-fg-subtle" />,
  todo: <Target size={16} className="text-sc-cyan" />,
  doing: <Play size={16} className="text-sc-purple" />,
  blocked: <Pause size={16} className="text-sc-red" />,
  review: <Send size={16} className="text-sc-yellow" />,
  done: <CheckCircle2 size={16} className="text-sc-green" />,
};

// Status workflow steps
const STATUS_FLOW: TaskStatusType[] = ['backlog', 'todo', 'doing', 'review', 'done'];

export function TaskDetailPanel({ task, relatedKnowledge = [] }: TaskDetailPanelProps) {
  const updateStatus = useTaskUpdateStatus();

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
  const learnings = task.metadata.learnings as string | undefined;

  // Filter out items with empty IDs to prevent React key errors
  const validRelatedKnowledge = relatedKnowledge.filter(item => item.id && item.id.length > 0);

  // Get next valid status transitions
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

  const currentStatusIndex = STATUS_FLOW.indexOf(status);

  return (
    <div className="space-y-6">
      {/* Main Card */}
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
          {/* Top Row: Badges */}
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            <span
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${statusConfig.bgClass} ${statusConfig.textClass} border border-current/20`}
            >
              {STATUS_ICONS[status]}
              {statusConfig.label}
            </span>
            <span
              className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${priorityConfig.bgClass} ${priorityConfig.textClass}`}
            >
              <Zap size={12} />
              {priorityConfig.label}
            </span>
            {feature && (
              <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-sc-purple/10 text-sc-purple border border-sc-purple/20">
                <Tag size={12} />
                {feature}
              </span>
            )}
          </div>

          {/* Title */}
          <h1 className="text-2xl font-bold text-sc-fg-primary mb-2 leading-tight">{task.name}</h1>

          {/* Description */}
          {task.description && (
            <p className="text-sc-fg-muted leading-relaxed">{task.description}</p>
          )}
        </div>

        {/* Blocker Alert */}
        {status === 'blocked' && blockerReason && (
          <div className="mx-6 mb-4 p-4 bg-sc-red/10 border border-sc-red/30 rounded-xl">
            <div className="flex items-start gap-3">
              <AlertCircle size={20} className="text-sc-red shrink-0 mt-0.5" />
              <div>
                <span className="text-sm font-semibold text-sc-red">Blocked</span>
                <p className="text-sm text-sc-fg-muted mt-1">{blockerReason}</p>
              </div>
            </div>
          </div>
        )}

        {/* Quick Actions */}
        <div className="px-6 pb-6">
          <div className="flex items-center gap-2 flex-wrap">
            {getNextStatuses(status).map(nextStatus => {
              const nextConfig = TASK_STATUS_CONFIG[nextStatus as TaskStatusType];
              const isProgressing =
                STATUS_FLOW.indexOf(nextStatus as TaskStatusType) > currentStatusIndex;

              return (
                <button
                  key={nextStatus}
                  type="button"
                  onClick={() => handleStatusChange(nextStatus)}
                  disabled={updateStatus.isPending}
                  className={`
                    inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium
                    transition-all duration-200 disabled:opacity-50
                    ${
                      isProgressing
                        ? 'bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25'
                        : `bg-sc-bg-elevated border border-sc-fg-subtle/20 ${nextConfig.textClass} hover:border-sc-fg-subtle/40`
                    }
                  `}
                >
                  {updateStatus.isPending ? (
                    <Loader2 size={16} className="animate-spin" />
                  ) : (
                    STATUS_ICONS[nextStatus as TaskStatusType]
                  )}
                  {isProgressing ? 'Move to' : 'Back to'} {nextConfig.label}
                  {isProgressing && <ArrowRight size={14} />}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Content - 2 cols */}
        <div className="lg:col-span-2 space-y-6">
          {/* Full Content */}
          {task.content && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
              <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
                Details
              </h2>
              <div className="prose prose-sm prose-invert max-w-none">
                <p className="text-sc-fg-primary whitespace-pre-wrap leading-relaxed">
                  {task.content}
                </p>
              </div>
            </div>
          )}

          {/* Technologies */}
          {technologies.length > 0 && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
              <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
                Technologies
              </h2>
              <div className="flex flex-wrap gap-2">
                {technologies.map(tech => (
                  <span
                    key={tech}
                    className="px-3 py-1.5 text-sm rounded-lg bg-sc-cyan/10 text-sc-cyan border border-sc-cyan/20 font-medium"
                  >
                    {tech}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Learnings - Only show if completed */}
          {status === 'done' && learnings && (
            <div className="bg-gradient-to-br from-sc-green/10 to-sc-cyan/5 border border-sc-green/20 rounded-2xl p-6">
              <h2 className="text-sm font-semibold text-sc-green uppercase tracking-wide mb-4 flex items-center gap-2">
                <Check size={16} />
                Learnings Captured
              </h2>
              <p className="text-sc-fg-primary whitespace-pre-wrap leading-relaxed">{learnings}</p>
            </div>
          )}

          {/* Related Knowledge */}
          {validRelatedKnowledge.length > 0 && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
              <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
                Linked Knowledge
              </h2>
              <div className="space-y-2">
                {validRelatedKnowledge.map(item => (
                  <Link
                    key={item.id}
                    href={`/entities/${item.id}`}
                    className="flex items-center gap-3 p-3 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10 hover:border-sc-purple/30 hover:bg-sc-bg-highlight transition-all group"
                  >
                    <EntityBadge type={item.type} size="sm" />
                    <div className="flex-1 min-w-0">
                      <span className="text-sm text-sc-fg-primary truncate block group-hover:text-sc-purple transition-colors">
                        {item.name}
                      </span>
                      <span className="text-xs text-sc-fg-subtle">{item.relationship}</span>
                    </div>
                    <ChevronRight
                      size={16}
                      className="text-sc-fg-subtle group-hover:text-sc-purple transition-colors"
                    />
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Sidebar - 1 col */}
        <div className="space-y-6">
          {/* Properties Card */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Properties
            </h2>
            <div className="space-y-4">
              {/* Assignees */}
              <div className="flex items-start gap-3">
                <Users size={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-sc-fg-subtle mb-1">Assignees</div>
                  {assignees.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {assignees.map(assignee => (
                        <span
                          key={assignee}
                          className="inline-flex items-center px-2 py-0.5 rounded-md bg-sc-purple/10 text-sc-purple text-sm"
                        >
                          {assignee}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-sm text-sc-fg-muted">Unassigned</span>
                  )}
                </div>
              </div>

              {/* Time Tracking */}
              {(estimatedHours !== undefined || actualHours !== undefined) && (
                <div className="flex items-start gap-3">
                  <Clock size={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                  <div className="flex-1">
                    <div className="text-xs text-sc-fg-subtle mb-1">Time</div>
                    <div className="flex items-center gap-3 text-sm">
                      {estimatedHours !== undefined && (
                        <span className="text-sc-fg-muted">
                          <span className="text-sc-fg-primary font-medium">{estimatedHours}h</span>{' '}
                          est
                        </span>
                      )}
                      {actualHours !== undefined && (
                        <span className="text-sc-fg-muted">
                          <span className="text-sc-fg-primary font-medium">{actualHours}h</span>{' '}
                          actual
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {/* Dates */}
              {(task.created_at || task.updated_at) && (
                <div className="flex items-start gap-3">
                  <Calendar size={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                  <div className="flex-1">
                    <div className="text-xs text-sc-fg-subtle mb-1">Timeline</div>
                    <div className="space-y-1 text-sm">
                      {task.created_at && (
                        <div className="text-sc-fg-muted">
                          Created{' '}
                          <span className="text-sc-fg-primary">
                            {formatDateTime(task.created_at)}
                          </span>
                        </div>
                      )}
                      {task.updated_at && task.updated_at !== task.created_at && (
                        <div className="text-sc-fg-muted">
                          Updated{' '}
                          <span className="text-sc-fg-primary">
                            {formatDateTime(task.updated_at)}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Development Card */}
          {(branchName || prUrl) && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-5">
              <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
                Development
              </h2>
              <div className="space-y-4">
                {branchName && (
                  <div className="flex items-start gap-3">
                    <GitBranch size={16} className="text-sc-cyan mt-0.5 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs text-sc-fg-subtle mb-1">Branch</div>
                      <code className="block text-sm font-mono bg-sc-bg-dark px-2.5 py-1.5 rounded-lg text-sc-cyan truncate">
                        {branchName}
                      </code>
                    </div>
                  </div>
                )}
                {prUrl && (
                  <div className="flex items-start gap-3">
                    <GitPullRequest size={16} className="text-sc-purple mt-0.5 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs text-sc-fg-subtle mb-1">Pull Request</div>
                      <a
                        href={prUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm text-sc-purple hover:underline"
                      >
                        View PR
                        <ExternalLink size={12} />
                      </a>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Project Link */}
          {projectId && (
            <Link
              href={`/tasks?project=${projectId}`}
              className="flex items-center gap-3 p-4 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl hover:border-sc-cyan/30 hover:bg-sc-bg-elevated transition-all group"
            >
              <div className="w-10 h-10 rounded-xl bg-sc-cyan/10 border border-sc-cyan/20 flex items-center justify-center">
                <Target size={18} className="text-sc-cyan" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-sc-fg-primary group-hover:text-sc-cyan transition-colors">
                  View Project Tasks
                </div>
                <div className="text-xs text-sc-fg-subtle truncate">{projectId}</div>
              </div>
              <ChevronRight
                size={18}
                className="text-sc-fg-subtle group-hover:text-sc-cyan transition-colors"
              />
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}

export function TaskDetailSkeleton() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Main Card Skeleton */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl overflow-hidden">
        <div className="h-1 bg-sc-bg-dark" />
        <div className="p-6">
          <div className="flex gap-2 mb-4">
            <div className="h-6 w-20 bg-sc-fg-subtle/10 rounded-full" />
            <div className="h-6 w-16 bg-sc-fg-subtle/10 rounded-full" />
          </div>
          <div className="h-8 w-3/4 bg-sc-fg-subtle/10 rounded-lg mb-3" />
          <div className="h-4 w-1/2 bg-sc-fg-subtle/10 rounded" />
        </div>
        <div className="px-6 pb-6">
          <div className="h-10 w-40 bg-sc-fg-subtle/10 rounded-xl" />
        </div>
      </div>

      {/* Grid Skeleton */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
            <div className="h-4 w-20 bg-sc-fg-subtle/10 rounded mb-4" />
            <div className="space-y-2">
              <div className="h-4 w-full bg-sc-fg-subtle/10 rounded" />
              <div className="h-4 w-5/6 bg-sc-fg-subtle/10 rounded" />
              <div className="h-4 w-4/6 bg-sc-fg-subtle/10 rounded" />
            </div>
          </div>
        </div>
        <div className="space-y-6">
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-5">
            <div className="h-4 w-24 bg-sc-fg-subtle/10 rounded mb-4" />
            <div className="space-y-4">
              <div className="h-8 w-full bg-sc-fg-subtle/10 rounded" />
              <div className="h-8 w-full bg-sc-fg-subtle/10 rounded" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
