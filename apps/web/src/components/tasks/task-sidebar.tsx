'use client';

import Link from 'next/link';
import { useState } from 'react';
import { EditableSelect, EditableTags, EditableText } from '@/components/editable';
import {
  Calendar,
  ChevronRight,
  Clock,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  Loader2,
  Target,
  Trash2,
  Users,
} from '@/components/ui/icons';
import type { Entity } from '@/lib/api';
import { formatDateTime } from '@/lib/constants';
import type { ProjectOption } from './task-detail-types';

interface TaskSidebarProps {
  task: Entity;
  projectId: string | undefined;
  assignees: string[];
  estimatedHours: number | undefined;
  actualHours: number | undefined;
  branchName: string | undefined;
  prUrl: string | undefined;
  projectOptions: ProjectOption[];
  isDeleting: boolean;
  onUpdateField: (field: string, value: unknown) => Promise<void>;
  onDelete: () => Promise<void>;
}

/**
 * Sidebar with Properties, Development info, Project link, and Danger Zone.
 */
export function TaskSidebar({
  task,
  projectId,
  assignees,
  estimatedHours,
  actualHours,
  branchName,
  prUrl,
  projectOptions,
  isDeleting,
  onUpdateField,
  onDelete,
}: TaskSidebarProps) {
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  return (
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
                onSave={v => onUpdateField('project_id', v || undefined)}
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
                onSave={v => onUpdateField('assignees', v.length > 0 ? v : undefined)}
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
                    onSave={v => onUpdateField('estimated_hours', v ? Number(v) : undefined)}
                    placeholder="—"
                    className="w-8 text-center"
                  />
                  <span className="text-sc-fg-subtle">h est</span>
                </span>
                <span className="text-sc-fg-muted">
                  <EditableText
                    value={actualHours?.toString() || ''}
                    onSave={v => onUpdateField('actual_hours', v ? Number(v) : undefined)}
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
                      <span className="text-sc-fg-primary">{formatDateTime(task.created_at)}</span>
                    </div>
                  )}
                  {task.updated_at && task.updated_at !== task.created_at && (
                    <div className="text-sc-fg-muted">
                      Updated{' '}
                      <span className="text-sc-fg-primary">{formatDateTime(task.updated_at)}</span>
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
                  onSave={v => onUpdateField('branch_name', v || undefined)}
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
                  onSave={v => onUpdateField('pr_url', v || undefined)}
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
                onClick={onDelete}
                disabled={isDeleting}
                className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-sc-red text-white rounded-lg text-sm font-medium hover:bg-sc-red/80 transition-colors disabled:opacity-50"
              >
                {isDeleting ? (
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
  );
}
