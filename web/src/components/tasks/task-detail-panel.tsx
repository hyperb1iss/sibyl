'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useState } from 'react';
import { toast } from 'sonner';
import { EditableDate, EditableSelect, EditableTags, EditableText } from '@/components/editable';
import { EntityBadge } from '@/components/ui/badge';
import {
  AlertCircle,
  Calendar,
  CheckCircle2,
  ChevronRight,
  Circle,
  Clock,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  Hash,
  Loader2,
  Pause,
  Pencil,
  Play,
  RotateCcw,
  Send,
  Target,
  Trash2,
  Users,
  Zap,
} from '@/components/ui/icons';
import { Markdown } from '@/components/ui/markdown';
import type { Entity, TaskStatus } from '@/lib/api';
import {
  formatDateTime,
  TASK_PRIORITIES,
  TASK_PRIORITY_CONFIG,
  TASK_STATUS_CONFIG,
  TASK_STATUSES,
  type TaskPriorityType,
  type TaskStatusType,
} from '@/lib/constants';
import { useDeleteEntity, useProjects, useTaskUpdateStatus, useUpdateEntity } from '@/lib/hooks';

interface TaskDetailPanelProps {
  task: Entity;
  relatedKnowledge?: Array<{
    id: string;
    type: string;
    name: string;
    relationship: string;
  }>;
}

// Status icons
const STATUS_ICONS: Record<TaskStatusType, React.ReactNode> = {
  backlog: <Circle width={14} height={14} />,
  todo: <Target width={14} height={14} />,
  doing: <Play width={14} height={14} />,
  blocked: <Pause width={14} height={14} />,
  review: <Send width={14} height={14} />,
  done: <CheckCircle2 width={14} height={14} />,
};

const STATUS_FLOW: TaskStatusType[] = ['backlog', 'todo', 'doing', 'review', 'done'];

// Build options for selects
const statusOptions = TASK_STATUSES.map(s => ({
  value: s,
  label: TASK_STATUS_CONFIG[s].label,
  icon: STATUS_ICONS[s],
  color: TASK_STATUS_CONFIG[s].textClass,
}));

const priorityOptions = TASK_PRIORITIES.map(p => ({
  value: p,
  label: TASK_PRIORITY_CONFIG[p].label,
  icon: <Zap width={14} height={14} />,
  color: TASK_PRIORITY_CONFIG[p].textClass,
}));

export function TaskDetailPanel({ task, relatedKnowledge = [] }: TaskDetailPanelProps) {
  const router = useRouter();
  const updateStatus = useTaskUpdateStatus();
  const updateEntity = useUpdateEntity();
  const deleteEntity = useDeleteEntity();
  const { data: projectsData } = useProjects();

  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [editingContent, setEditingContent] = useState(false);
  const [editingLearnings, setEditingLearnings] = useState(false);

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
  const tags = (task.metadata.tags as string[]) || [];
  const blockerReason = task.metadata.blocker_reason as string | undefined;
  const learnings = task.metadata.learnings as string | undefined;
  const dueDate = task.metadata.due_date as string | undefined;

  const validRelatedKnowledge = relatedKnowledge.filter(item => item.id?.length > 0);
  const currentStatusIndex = STATUS_FLOW.indexOf(status);
  const isOverdue = dueDate && new Date(dueDate) < new Date() && status !== 'done';

  // Project options for select
  const projectOptions = [
    { value: '', label: 'No project', icon: <Circle width={14} height={14} /> },
    ...(projectsData?.entities?.map(p => ({
      value: p.id,
      label: p.name,
      icon: <Target width={14} height={14} />,
    })) || []),
  ];

  // Generic field update helper
  const updateField = useCallback(
    async (field: string, value: unknown, metadataField = true) => {
      try {
        if (metadataField) {
          await updateEntity.mutateAsync({
            id: task.id,
            updates: { metadata: { [field]: value } },
          });
        } else {
          await updateEntity.mutateAsync({
            id: task.id,
            updates: { [field]: value },
          });
        }
        toast.success('Updated');
      } catch {
        toast.error('Failed to update');
      }
    },
    [task.id, updateEntity]
  );

  const handleStatusChange = useCallback(
    async (newStatus: string) => {
      try {
        await updateStatus.mutateAsync({ id: task.id, status: newStatus as TaskStatus });
        toast.success(`Status → ${TASK_STATUS_CONFIG[newStatus as TaskStatusType].label}`);
      } catch {
        toast.error('Failed to update status');
      }
    },
    [task.id, updateStatus]
  );

  const handleDelete = useCallback(async () => {
    try {
      await deleteEntity.mutateAsync(task.id);
      toast.success('Task deleted');
      router.push('/tasks');
    } catch {
      toast.error('Failed to delete');
    }
  }, [task.id, deleteEntity, router]);

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
                  {updateStatus.isPending ? (
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

          {/* Title - Big inline editable */}
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

        {/* Blocker Alert */}
        {status === 'blocked' && (
          <div className="mx-6 mb-4 p-4 bg-sc-red/10 border border-sc-red/30 rounded-xl">
            <div className="flex items-start gap-3">
              <AlertCircle width={20} height={20} className="text-sc-red shrink-0 mt-0.5" />
              <div className="flex-1">
                <span className="text-sm font-semibold text-sc-red">Blocked</span>
                <div className="text-sm text-sc-fg-muted mt-1">
                  <EditableText
                    value={blockerReason || ''}
                    onSave={v => updateField('blocker_reason', v || undefined)}
                    placeholder="What's blocking this task?"
                    multiline
                    rows={2}
                  />
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Quick Actions */}
        <div className="px-6 pb-6">
          <div className="flex items-center gap-2 flex-wrap">
            {status === 'todo' && (
              <button
                type="button"
                onClick={() => handleStatusChange('doing')}
                disabled={updateStatus.isPending}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25 transition-all disabled:opacity-50"
              >
                <Play width={16} height={16} />
                Start Working
              </button>
            )}

            {status === 'doing' && (
              <>
                <button
                  type="button"
                  onClick={() => handleStatusChange('review')}
                  disabled={updateStatus.isPending}
                  className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25 transition-all disabled:opacity-50"
                >
                  <Send width={16} height={16} />
                  Submit for Review
                </button>
                <button
                  type="button"
                  onClick={() => handleStatusChange('blocked')}
                  className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-elevated border border-sc-fg-subtle/20 text-sc-red hover:border-sc-red/30 transition-all"
                >
                  <Pause width={16} height={16} />
                  Mark Blocked
                </button>
              </>
            )}

            {status === 'review' && (
              <button
                type="button"
                onClick={() => handleStatusChange('done')}
                disabled={updateStatus.isPending}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-green text-sc-bg-dark hover:bg-sc-green/80 shadow-lg shadow-sc-green/25 transition-all disabled:opacity-50"
              >
                <CheckCircle2 width={16} height={16} />
                Complete Task
              </button>
            )}

            {status === 'blocked' && (
              <button
                type="button"
                onClick={() => handleStatusChange('doing')}
                disabled={updateStatus.isPending}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25 transition-all disabled:opacity-50"
              >
                <Play width={16} height={16} />
                Unblock & Resume
              </button>
            )}

            {status === 'done' && (
              <button
                type="button"
                onClick={() => handleStatusChange('todo')}
                disabled={updateStatus.isPending}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-elevated border border-sc-fg-subtle/20 text-sc-fg-muted hover:text-sc-fg-primary hover:border-sc-fg-subtle/40 transition-all disabled:opacity-50"
              >
                <RotateCcw width={16} height={16} />
                Reopen Task
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Content Grid - sidebar first on mobile for quick actions */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 sm:gap-6">
        {/* Main Content - 2 cols */}
        <div className="lg:col-span-2 space-y-6">
          {/* Details */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide">
                Details
              </h2>
              <button
                type="button"
                onClick={() => setEditingContent(!editingContent)}
                className={`p-1.5 rounded-lg transition-all ${
                  editingContent
                    ? 'bg-sc-purple/20 text-sc-purple'
                    : 'text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50'
                }`}
                title={editingContent ? 'View markdown' : 'Edit'}
              >
                <Pencil width={14} height={14} />
              </button>
            </div>
            {editingContent ? (
              <EditableText
                value={task.content || ''}
                onSave={async v => {
                  await updateField('content', v || undefined, false);
                  setEditingContent(false);
                }}
                placeholder="Add detailed content, requirements, notes... (Markdown supported)"
                multiline
                rows={10}
              />
            ) : task.content ? (
              <Markdown content={task.content} />
            ) : (
              <button
                type="button"
                onClick={() => setEditingContent(true)}
                className="text-sc-fg-subtle italic hover:text-sc-fg-muted transition-colors"
              >
                Add detailed content, requirements, notes...
              </button>
            )}
          </div>

          {/* Technologies */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
            <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Technologies
            </h2>
            <EditableTags
              values={technologies}
              onSave={v => updateField('technologies', v.length > 0 ? v : undefined)}
              tagClassName="bg-sc-cyan/10 text-sc-cyan border-sc-cyan/20"
              placeholder="Add technology"
              suggestions={['React', 'TypeScript', 'Python', 'Next.js', 'GraphQL', 'Tailwind']}
            />
          </div>

          {/* Tags */}
          <div className="bg-gradient-to-br from-sc-bg-base to-sc-purple/5 border border-sc-purple/20 rounded-2xl p-6">
            <h2 className="text-sm font-semibold text-sc-purple uppercase tracking-wide mb-4 flex items-center gap-2">
              <Hash width={16} height={16} />
              Tags
            </h2>
            <EditableTags
              values={tags}
              onSave={v => updateField('tags', v.length > 0 ? v : undefined)}
              tagClassName="bg-sc-purple/10 text-sc-purple border-sc-purple/20"
              placeholder="Add tag"
              addPlaceholder="Type tag and press Enter"
              suggestions={[
                'frontend',
                'backend',
                'database',
                'devops',
                'testing',
                'docs',
                'security',
                'performance',
                'feature',
                'bug',
                'refactor',
                'chore',
                'research',
              ]}
            />
            {tags.length === 0 && (
              <p className="text-xs text-sc-fg-subtle mt-3 italic">
                Tags are auto-generated when creating tasks. Add more to help organize and filter.
              </p>
            )}
          </div>

          {/* Learnings - show when done or has content */}
          {(status === 'done' || learnings) && (
            <div className="bg-gradient-to-br from-sc-green/10 to-sc-cyan/5 border border-sc-green/20 rounded-2xl p-6">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold text-sc-green uppercase tracking-wide flex items-center gap-2">
                  <CheckCircle2 width={16} height={16} />
                  Learnings
                </h2>
                <button
                  type="button"
                  onClick={() => setEditingLearnings(!editingLearnings)}
                  className={`p-1.5 rounded-lg transition-all ${
                    editingLearnings
                      ? 'bg-sc-green/20 text-sc-green'
                      : 'text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50'
                  }`}
                  title={editingLearnings ? 'View markdown' : 'Edit'}
                >
                  <Pencil width={14} height={14} />
                </button>
              </div>
              {editingLearnings ? (
                <EditableText
                  value={learnings || ''}
                  onSave={async v => {
                    await updateField('learnings', v || undefined);
                    setEditingLearnings(false);
                  }}
                  placeholder="What did you learn? Capture insights... (Markdown supported)"
                  multiline
                  rows={6}
                />
              ) : learnings ? (
                <Markdown content={learnings} />
              ) : (
                <button
                  type="button"
                  onClick={() => setEditingLearnings(true)}
                  className="text-sc-fg-subtle italic hover:text-sc-fg-muted transition-colors"
                >
                  What did you learn? Capture insights...
                </button>
              )}
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
                      width={16}
                      height={16}
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
          {/* Properties */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Properties
            </h2>
            <div className="space-y-4">
              {/* Project */}
              <div className="flex items-start gap-3">
                <Target width={16} height={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                <div className="flex-1">
                  <div className="text-xs text-sc-fg-subtle mb-1">Project</div>
                  <EditableSelect
                    value={projectId || ''}
                    options={projectOptions}
                    onSave={v => updateField('project_id', v || undefined)}
                    renderValue={opt => (
                      <span className="text-sm text-sc-fg-primary">
                        {opt?.label || 'Select project'}
                      </span>
                    )}
                  />
                </div>
              </div>

              {/* Assignees */}
              <div className="flex items-start gap-3">
                <Users width={16} height={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                <div className="flex-1">
                  <div className="text-xs text-sc-fg-subtle mb-1">Assignees</div>
                  <EditableTags
                    values={assignees}
                    onSave={v => updateField('assignees', v.length > 0 ? v : undefined)}
                    tagClassName="bg-sc-purple/10 text-sc-purple border-sc-purple/20"
                    placeholder="Add assignee"
                  />
                </div>
              </div>

              {/* Time Tracking */}
              <div className="flex items-start gap-3">
                <Clock width={16} height={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                <div className="flex-1">
                  <div className="text-xs text-sc-fg-subtle mb-1">Time</div>
                  <div className="flex items-center gap-3 text-sm">
                    <span className="text-sc-fg-muted">
                      <EditableText
                        value={estimatedHours?.toString() || ''}
                        onSave={v => updateField('estimated_hours', v ? Number(v) : undefined)}
                        placeholder="—"
                        className="w-8 text-center"
                      />
                      <span className="text-sc-fg-subtle">h est</span>
                    </span>
                    <span className="text-sc-fg-muted">
                      <EditableText
                        value={actualHours?.toString() || ''}
                        onSave={v => updateField('actual_hours', v ? Number(v) : undefined)}
                        placeholder="—"
                        className="w-8 text-center"
                      />
                      <span className="text-sc-fg-subtle">h actual</span>
                    </span>
                  </div>
                </div>
              </div>

              {/* Timeline */}
              {(task.created_at || task.updated_at) && (
                <div className="flex items-start gap-3">
                  <Calendar width={16} height={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
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

          {/* Development */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Development
            </h2>
            <div className="space-y-4">
              {/* Branch */}
              <div className="flex items-start gap-3">
                <GitBranch width={16} height={16} className="text-sc-cyan mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-sc-fg-subtle mb-1">Branch</div>
                  <div className="text-sm font-mono bg-sc-bg-dark px-2.5 py-1.5 rounded-lg text-sc-cyan">
                    <EditableText
                      value={branchName || ''}
                      onSave={v => updateField('branch_name', v || undefined)}
                      placeholder="feature/..."
                      className="font-mono text-sm"
                    />
                  </div>
                </div>
              </div>

              {/* PR */}
              <div className="flex items-start gap-3">
                <GitPullRequest width={16} height={16} className="text-sc-purple mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-sc-fg-subtle mb-1">Pull Request</div>
                  {prUrl ? (
                    <a
                      href={prUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-sm text-sc-purple hover:underline"
                    >
                      View PR
                      <ExternalLink width={12} height={12} />
                    </a>
                  ) : (
                    <EditableText
                      value=""
                      onSave={v => updateField('pr_url', v || undefined)}
                      placeholder="Add PR link..."
                      className="text-sm"
                    />
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Project Link */}
          {projectId && (
            <Link
              href={`/tasks?project=${projectId}`}
              className="flex items-center gap-3 p-4 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl hover:border-sc-cyan/30 hover:bg-sc-bg-elevated transition-all group"
            >
              <div className="w-10 h-10 rounded-xl bg-sc-cyan/10 border border-sc-cyan/20 flex items-center justify-center">
                <Target width={18} height={18} className="text-sc-cyan" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-sc-fg-primary group-hover:text-sc-cyan transition-colors">
                  View Project Tasks
                </div>
                <div className="text-xs text-sc-fg-subtle truncate">{projectId}</div>
              </div>
              <ChevronRight
                width={18}
                height={18}
                className="text-sc-fg-subtle group-hover:text-sc-cyan transition-colors"
              />
            </Link>
          )}

          {/* Danger Zone */}
          <div className="bg-sc-bg-base border border-sc-red/20 rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-sc-red uppercase tracking-wide mb-3">
              Danger Zone
            </h2>
            {showDeleteConfirm ? (
              <div className="space-y-3">
                <p className="text-sm text-sc-fg-muted">Are you sure? This cannot be undone.</p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setShowDeleteConfirm(false)}
                    className="flex-1 px-3 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleDelete}
                    disabled={deleteEntity.isPending}
                    className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-sc-red text-white rounded-lg text-sm font-medium hover:bg-sc-red/80 transition-colors disabled:opacity-50"
                  >
                    {deleteEntity.isPending ? (
                      <Loader2 width={14} height={14} className="animate-spin" />
                    ) : (
                      <Trash2 width={14} height={14} />
                    )}
                    Delete
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setShowDeleteConfirm(true)}
                className="w-full flex items-center justify-center gap-2 px-3 py-2 border border-sc-red/30 text-sc-red rounded-lg text-sm hover:bg-sc-red/10 transition-colors"
              >
                <Trash2 width={14} height={14} />
                Delete Task
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function TaskDetailSkeleton() {
  return (
    <div className="space-y-6 animate-pulse">
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
