'use client';

import {
  AlertCircle,
  Calendar,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Clock,
  Edit3,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  Loader2,
  Pause,
  Play,
  RotateCcw,
  Save,
  Send,
  Tag,
  Target,
  Trash2,
  Users,
  X,
  Zap,
} from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useState } from 'react';
import { EntityBadge } from '@/components/ui/badge';
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
  const router = useRouter();
  const updateStatus = useTaskUpdateStatus();
  const updateEntity = useUpdateEntity();
  const deleteEntity = useDeleteEntity();
  const { data: projectsData } = useProjects();

  const [isEditing, setIsEditing] = useState(false);
  const [isStatusDropdownOpen, setIsStatusDropdownOpen] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  // Edit form state
  const [editName, setEditName] = useState(task.name);
  const [editDescription, setEditDescription] = useState(task.description || '');
  const [editContent, setEditContent] = useState(task.content || '');
  const [editPriority, setEditPriority] = useState(
    (task.metadata.priority as TaskPriorityType) || 'medium'
  );
  const [editProjectId, setEditProjectId] = useState((task.metadata.project_id as string) || '');
  const [editFeature, setEditFeature] = useState((task.metadata.feature as string) || '');
  const [editAssignees, setEditAssignees] = useState(
    ((task.metadata.assignees as string[]) || []).join(', ')
  );
  const [editTechnologies, setEditTechnologies] = useState(
    ((task.metadata.technologies as string[]) || []).join(', ')
  );
  const [editDueDate, setEditDueDate] = useState((task.metadata.due_date as string) || '');
  const [editEstimatedHours, setEditEstimatedHours] = useState(
    task.metadata.estimated_hours?.toString() || ''
  );
  const [editActualHours, setEditActualHours] = useState(
    task.metadata.actual_hours?.toString() || ''
  );
  const [editBranchName, setEditBranchName] = useState((task.metadata.branch_name as string) || '');
  const [editPrUrl, setEditPrUrl] = useState((task.metadata.pr_url as string) || '');
  const [editBlockerReason, setEditBlockerReason] = useState(
    (task.metadata.blocker_reason as string) || ''
  );
  const [editLearnings, setEditLearnings] = useState((task.metadata.learnings as string) || '');

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
  const dueDate = task.metadata.due_date as string | undefined;

  // Filter out items with empty IDs
  const validRelatedKnowledge = relatedKnowledge.filter(item => item.id && item.id.length > 0);

  const handleStatusChange = async (newStatus: TaskStatus) => {
    setIsStatusDropdownOpen(false);
    await updateStatus.mutateAsync({ id: task.id, status: newStatus });
  };

  const handleStartEdit = useCallback(() => {
    setEditName(task.name);
    setEditDescription(task.description || '');
    setEditContent(task.content || '');
    setEditPriority((task.metadata.priority as TaskPriorityType) || 'medium');
    setEditProjectId((task.metadata.project_id as string) || '');
    setEditFeature((task.metadata.feature as string) || '');
    setEditAssignees(((task.metadata.assignees as string[]) || []).join(', '));
    setEditTechnologies(((task.metadata.technologies as string[]) || []).join(', '));
    setEditDueDate((task.metadata.due_date as string) || '');
    setEditEstimatedHours(task.metadata.estimated_hours?.toString() || '');
    setEditActualHours(task.metadata.actual_hours?.toString() || '');
    setEditBranchName((task.metadata.branch_name as string) || '');
    setEditPrUrl((task.metadata.pr_url as string) || '');
    setEditBlockerReason((task.metadata.blocker_reason as string) || '');
    setEditLearnings((task.metadata.learnings as string) || '');
    setIsEditing(true);
  }, [task]);

  const handleCancelEdit = () => {
    setIsEditing(false);
    setShowDeleteConfirm(false);
  };

  const handleSave = async () => {
    const assigneesArray = editAssignees
      .split(',')
      .map(a => a.trim())
      .filter(Boolean);
    const techArray = editTechnologies
      .split(',')
      .map(t => t.trim())
      .filter(Boolean);

    await updateEntity.mutateAsync({
      id: task.id,
      updates: {
        name: editName,
        description: editDescription || undefined,
        content: editContent || undefined,
        metadata: {
          priority: editPriority,
          project_id: editProjectId || undefined,
          feature: editFeature || undefined,
          assignees: assigneesArray.length > 0 ? assigneesArray : undefined,
          technologies: techArray.length > 0 ? techArray : undefined,
          due_date: editDueDate || undefined,
          estimated_hours: editEstimatedHours ? Number(editEstimatedHours) : undefined,
          actual_hours: editActualHours ? Number(editActualHours) : undefined,
          branch_name: editBranchName || undefined,
          pr_url: editPrUrl || undefined,
          blocker_reason: editBlockerReason || undefined,
          learnings: editLearnings || undefined,
        },
      },
    });
    setIsEditing(false);
  };

  const handleDelete = async () => {
    await deleteEntity.mutateAsync(task.id);
    router.push('/tasks');
  };

  const currentStatusIndex = STATUS_FLOW.indexOf(status);
  const isOverdue = dueDate && new Date(dueDate) < new Date() && status !== 'done';

  const inputClass =
    'w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/30 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/20 transition-all';
  const labelClass = 'block text-xs text-sc-fg-subtle mb-1.5 uppercase tracking-wide';

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
          {/* Top Row: Status + Badges + Actions */}
          <div className="flex items-start justify-between gap-4 mb-4">
            <div className="flex items-center gap-2 flex-wrap">
              {/* Status Dropdown */}
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setIsStatusDropdownOpen(!isStatusDropdownOpen)}
                  disabled={updateStatus.isPending}
                  className={`
                    inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium
                    ${statusConfig.bgClass} ${statusConfig.textClass} border border-current/20
                    hover:opacity-80 transition-opacity disabled:opacity-50
                  `}
                >
                  {updateStatus.isPending ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    STATUS_ICONS[status]
                  )}
                  {statusConfig.label}
                  <ChevronDown size={14} />
                </button>

                {isStatusDropdownOpen && (
                  <>
                    <div
                      className="fixed inset-0 z-10"
                      onClick={() => setIsStatusDropdownOpen(false)}
                      onKeyDown={e => e.key === 'Escape' && setIsStatusDropdownOpen(false)}
                      role="presentation"
                    />
                    <div className="absolute top-full left-0 mt-1 z-20 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-xl overflow-hidden min-w-[160px]">
                      {TASK_STATUSES.map(s => {
                        const config = TASK_STATUS_CONFIG[s];
                        const isActive = s === status;
                        return (
                          <button
                            key={s}
                            type="button"
                            onClick={() => handleStatusChange(s as TaskStatus)}
                            className={`
                              w-full flex items-center gap-2 px-3 py-2 text-sm text-left transition-colors
                              ${isActive ? 'bg-sc-bg-highlight' : 'hover:bg-sc-bg-highlight/50'}
                            `}
                          >
                            <span className={config.textClass}>{STATUS_ICONS[s]}</span>
                            <span
                              className={
                                isActive ? 'text-sc-fg-primary font-medium' : 'text-sc-fg-muted'
                              }
                            >
                              {config.label}
                            </span>
                            {isActive && <Check size={14} className="ml-auto text-sc-green" />}
                          </button>
                        );
                      })}
                    </div>
                  </>
                )}
              </div>

              {/* Priority Badge (editable in edit mode) */}
              {isEditing ? (
                <select
                  value={editPriority}
                  onChange={e => setEditPriority(e.target.value as TaskPriorityType)}
                  className="px-2 py-1 text-xs rounded-full bg-sc-bg-highlight border border-sc-fg-subtle/30 text-sc-fg-primary focus:border-sc-purple focus:outline-none"
                >
                  {TASK_PRIORITIES.map(p => (
                    <option key={p} value={p}>
                      {TASK_PRIORITY_CONFIG[p].label}
                    </option>
                  ))}
                </select>
              ) : (
                <span
                  className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${priorityConfig.bgClass} ${priorityConfig.textClass}`}
                >
                  <Zap size={12} />
                  {priorityConfig.label}
                </span>
              )}

              {/* Feature Tag */}
              {!isEditing && feature && (
                <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-sc-purple/10 text-sc-purple border border-sc-purple/20">
                  <Tag size={12} />
                  {feature}
                </span>
              )}

              {/* Due Date Badge */}
              {!isEditing && dueDate && (
                <span
                  className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${
                    isOverdue
                      ? 'bg-sc-red/10 text-sc-red border border-sc-red/20'
                      : 'bg-sc-fg-subtle/10 text-sc-fg-muted'
                  }`}
                >
                  <Calendar size={12} />
                  {new Date(dueDate).toLocaleDateString()}
                  {isOverdue && ' (Overdue)'}
                </span>
              )}
            </div>

            {/* Edit/Save/Cancel Actions */}
            <div className="flex items-center gap-2">
              {isEditing ? (
                <>
                  <button
                    type="button"
                    onClick={handleCancelEdit}
                    disabled={updateEntity.isPending}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
                  >
                    <X size={14} />
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleSave}
                    disabled={updateEntity.isPending || !editName.trim()}
                    className="flex items-center gap-1.5 px-4 py-1.5 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                  >
                    {updateEntity.isPending ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : (
                      <Save size={14} />
                    )}
                    Save
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={handleStartEdit}
                  className="flex items-center gap-2 px-3 py-1.5 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-muted hover:text-sc-fg-primary hover:border-sc-fg-subtle/40 transition-colors"
                >
                  <Edit3 size={14} />
                  Edit
                </button>
              )}
            </div>
          </div>

          {/* Title */}
          {isEditing ? (
            <input
              type="text"
              value={editName}
              onChange={e => setEditName(e.target.value)}
              placeholder="Task name"
              className="w-full text-2xl font-bold bg-transparent border-b-2 border-sc-fg-subtle/30 focus:border-sc-purple text-sc-fg-primary placeholder:text-sc-fg-subtle/50 focus:outline-none pb-1 mb-2"
            />
          ) : (
            <h1 className="text-2xl font-bold text-sc-fg-primary mb-2 leading-tight">
              {task.name}
            </h1>
          )}

          {/* Description */}
          {isEditing ? (
            <textarea
              value={editDescription}
              onChange={e => setEditDescription(e.target.value)}
              placeholder="Brief description..."
              rows={2}
              className={`${inputClass} resize-none`}
            />
          ) : (
            task.description && (
              <p className="text-sc-fg-muted leading-relaxed">{task.description}</p>
            )
          )}
        </div>

        {/* Blocker Alert */}
        {status === 'blocked' && (isEditing || blockerReason) && (
          <div className="mx-6 mb-4 p-4 bg-sc-red/10 border border-sc-red/30 rounded-xl">
            <div className="flex items-start gap-3">
              <AlertCircle size={20} className="text-sc-red shrink-0 mt-0.5" />
              <div className="flex-1">
                <span className="text-sm font-semibold text-sc-red">Blocked</span>
                {isEditing ? (
                  <textarea
                    value={editBlockerReason}
                    onChange={e => setEditBlockerReason(e.target.value)}
                    placeholder="What's blocking this task?"
                    rows={2}
                    className={`${inputClass} mt-2 resize-none`}
                  />
                ) : (
                  blockerReason && <p className="text-sm text-sc-fg-muted mt-1">{blockerReason}</p>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Quick Actions (only in view mode) */}
        {!isEditing && (
          <div className="px-6 pb-6">
            <div className="flex items-center gap-2 flex-wrap">
              {status === 'todo' && (
                <button
                  type="button"
                  onClick={() => handleStatusChange('doing')}
                  disabled={updateStatus.isPending}
                  className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25 transition-all disabled:opacity-50"
                >
                  <Play size={16} />
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
                    <Send size={16} />
                    Submit for Review
                  </button>
                  <button
                    type="button"
                    onClick={() => handleStatusChange('blocked')}
                    className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-elevated border border-sc-fg-subtle/20 text-sc-red hover:border-sc-red/30 transition-all"
                  >
                    <Pause size={16} />
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
                  <CheckCircle2 size={16} />
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
                  <Play size={16} />
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
                  <RotateCcw size={16} />
                  Reopen Task
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Content - 2 cols */}
        <div className="lg:col-span-2 space-y-6">
          {/* Full Content */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
            <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Details
            </h2>
            {isEditing ? (
              <textarea
                value={editContent}
                onChange={e => setEditContent(e.target.value)}
                placeholder="Detailed task content, requirements, notes..."
                rows={8}
                className={`${inputClass} resize-y min-h-[200px]`}
              />
            ) : task.content ? (
              <div className="prose prose-sm prose-invert max-w-none">
                <p className="text-sc-fg-primary whitespace-pre-wrap leading-relaxed">
                  {task.content}
                </p>
              </div>
            ) : (
              <p className="text-sc-fg-subtle italic">No details added yet.</p>
            )}
          </div>

          {/* Technologies */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
            <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Technologies
            </h2>
            {isEditing ? (
              <input
                type="text"
                value={editTechnologies}
                onChange={e => setEditTechnologies(e.target.value)}
                placeholder="React, TypeScript, GraphQL... (comma separated)"
                className={inputClass}
              />
            ) : technologies.length > 0 ? (
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
            ) : (
              <p className="text-sc-fg-subtle italic">No technologies specified.</p>
            )}
          </div>

          {/* Learnings */}
          {(isEditing || (status === 'done' && learnings)) && (
            <div
              className={`border rounded-2xl p-6 ${
                isEditing
                  ? 'bg-sc-bg-base border-sc-fg-subtle/20'
                  : 'bg-gradient-to-br from-sc-green/10 to-sc-cyan/5 border-sc-green/20'
              }`}
            >
              <h2
                className={`text-sm font-semibold uppercase tracking-wide mb-4 flex items-center gap-2 ${
                  isEditing ? 'text-sc-fg-subtle' : 'text-sc-green'
                }`}
              >
                <Check size={16} />
                Learnings
              </h2>
              {isEditing ? (
                <textarea
                  value={editLearnings}
                  onChange={e => setEditLearnings(e.target.value)}
                  placeholder="What did you learn from this task? Capture insights for future reference..."
                  rows={4}
                  className={`${inputClass} resize-y`}
                />
              ) : (
                <p className="text-sc-fg-primary whitespace-pre-wrap leading-relaxed">
                  {learnings}
                </p>
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
              {/* Project */}
              {isEditing ? (
                <div>
                  <label htmlFor="edit-project" className={labelClass}>
                    Project
                  </label>
                  <select
                    id="edit-project"
                    value={editProjectId}
                    onChange={e => setEditProjectId(e.target.value)}
                    className={inputClass}
                  >
                    <option value="">No project</option>
                    {projectsData?.entities?.map(p => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              {/* Feature (edit mode) */}
              {isEditing && (
                <div>
                  <label htmlFor="edit-feature" className={labelClass}>
                    Feature / Tag
                  </label>
                  <input
                    id="edit-feature"
                    type="text"
                    value={editFeature}
                    onChange={e => setEditFeature(e.target.value)}
                    placeholder="e.g., auth, api, ui"
                    className={inputClass}
                  />
                </div>
              )}

              {/* Assignees */}
              <div className="flex items-start gap-3">
                <Users size={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-sc-fg-subtle mb-1">Assignees</div>
                  {isEditing ? (
                    <input
                      type="text"
                      value={editAssignees}
                      onChange={e => setEditAssignees(e.target.value)}
                      placeholder="Comma separated names"
                      className={inputClass}
                    />
                  ) : assignees.length > 0 ? (
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

              {/* Due Date */}
              <div className="flex items-start gap-3">
                <Calendar
                  size={16}
                  className={`mt-0.5 shrink-0 ${isOverdue && !isEditing ? 'text-sc-red' : 'text-sc-fg-subtle'}`}
                />
                <div className="flex-1">
                  <div className="text-xs text-sc-fg-subtle mb-1">Due Date</div>
                  {isEditing ? (
                    <input
                      type="date"
                      value={editDueDate}
                      onChange={e => setEditDueDate(e.target.value)}
                      className={inputClass}
                    />
                  ) : dueDate ? (
                    <span
                      className={`text-sm ${isOverdue ? 'text-sc-red font-medium' : 'text-sc-fg-primary'}`}
                    >
                      {new Date(dueDate).toLocaleDateString('en-US', {
                        weekday: 'short',
                        month: 'short',
                        day: 'numeric',
                      })}
                      {isOverdue && ' (Overdue)'}
                    </span>
                  ) : (
                    <span className="text-sm text-sc-fg-muted">Not set</span>
                  )}
                </div>
              </div>

              {/* Time Tracking */}
              <div className="flex items-start gap-3">
                <Clock size={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
                <div className="flex-1">
                  <div className="text-xs text-sc-fg-subtle mb-1">Time</div>
                  {isEditing ? (
                    <div className="flex gap-2">
                      <input
                        type="number"
                        value={editEstimatedHours}
                        onChange={e => setEditEstimatedHours(e.target.value)}
                        placeholder="Est."
                        min="0"
                        step="0.5"
                        className={`${inputClass} w-20`}
                      />
                      <input
                        type="number"
                        value={editActualHours}
                        onChange={e => setEditActualHours(e.target.value)}
                        placeholder="Actual"
                        min="0"
                        step="0.5"
                        className={`${inputClass} w-20`}
                      />
                    </div>
                  ) : (
                    <div className="flex items-center gap-3 text-sm">
                      {estimatedHours !== undefined ? (
                        <span className="text-sc-fg-muted">
                          <span className="text-sc-fg-primary font-medium">{estimatedHours}h</span>{' '}
                          est
                        </span>
                      ) : (
                        <span className="text-sc-fg-muted">—</span>
                      )}
                      {actualHours !== undefined && (
                        <span className="text-sc-fg-muted">
                          <span className="text-sc-fg-primary font-medium">{actualHours}h</span>{' '}
                          actual
                        </span>
                      )}
                    </div>
                  )}
                </div>
              </div>

              {/* Created/Updated */}
              {!isEditing && (task.created_at || task.updated_at) && (
                <div className="flex items-start gap-3">
                  <Clock size={16} className="text-sc-fg-subtle mt-0.5 shrink-0" />
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
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-5">
            <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Development
            </h2>
            <div className="space-y-4">
              {/* Branch */}
              <div className="flex items-start gap-3">
                <GitBranch size={16} className="text-sc-cyan mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-sc-fg-subtle mb-1">Branch</div>
                  {isEditing ? (
                    <input
                      type="text"
                      value={editBranchName}
                      onChange={e => setEditBranchName(e.target.value)}
                      placeholder="feature/task-name"
                      className={inputClass}
                    />
                  ) : branchName ? (
                    <code className="block text-sm font-mono bg-sc-bg-dark px-2.5 py-1.5 rounded-lg text-sc-cyan truncate">
                      {branchName}
                    </code>
                  ) : (
                    <span className="text-sm text-sc-fg-muted">Not set</span>
                  )}
                </div>
              </div>

              {/* PR */}
              <div className="flex items-start gap-3">
                <GitPullRequest size={16} className="text-sc-purple mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-sc-fg-subtle mb-1">Pull Request</div>
                  {isEditing ? (
                    <input
                      type="url"
                      value={editPrUrl}
                      onChange={e => setEditPrUrl(e.target.value)}
                      placeholder="https://github.com/..."
                      className={inputClass}
                    />
                  ) : prUrl ? (
                    <a
                      href={prUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-sm text-sc-purple hover:underline"
                    >
                      View PR
                      <ExternalLink size={12} />
                    </a>
                  ) : (
                    <span className="text-sm text-sc-fg-muted">Not set</span>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Project Link (view mode only) */}
          {!isEditing && projectId && (
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

          {/* Delete Button (edit mode only) */}
          {isEditing && (
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
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <Trash2 size={14} />
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
                  <Trash2 size={14} />
                  Delete Task
                </button>
              )}
            </div>
          )}
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
