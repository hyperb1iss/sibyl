'use client';

import { useRouter } from 'next/navigation';
import { useCallback } from 'react';
import { toast } from 'sonner';
import { Circle, Target } from '@/components/ui/icons';
import type { Entity, TaskStatus } from '@/lib/api';
import { TASK_STATUS_CONFIG, type TaskPriorityType, type TaskStatusType } from '@/lib/constants';
import { useDeleteEntity, useProjects, useTaskUpdateStatus, useUpdateEntity } from '@/lib/hooks';
import { TaskContentSections } from './task-content-sections';
import type { RelatedKnowledgeItem } from './task-detail-types';
import { TaskHeader } from './task-header';
import { TaskQuickActions } from './task-quick-actions';
import { TaskSidebar } from './task-sidebar';

interface TaskDetailPanelProps {
  task: Entity;
  relatedKnowledge?: RelatedKnowledgeItem[];
}

/**
 * Full task detail view with editable fields, status actions, and related knowledge.
 * Composed of: TaskHeader, TaskQuickActions, TaskContentSections, TaskSidebar.
 */
export function TaskDetailPanel({ task, relatedKnowledge = [] }: TaskDetailPanelProps) {
  const router = useRouter();
  const updateStatus = useTaskUpdateStatus();
  const updateEntity = useUpdateEntity();
  const deleteEntity = useDeleteEntity();
  const { data: projectsData } = useProjects();

  // Extract metadata fields
  const status = (task.metadata.status as TaskStatusType) || 'backlog';
  const priority = (task.metadata.priority as TaskPriorityType) || 'medium';
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
      {/* Header with progress bar, badges, title, description */}
      <TaskHeader
        task={task}
        status={status}
        priority={priority}
        feature={feature}
        dueDate={dueDate}
        isOverdue={!!isOverdue}
        updateField={updateField}
        handleStatusChange={handleStatusChange}
        isUpdating={updateStatus.isPending}
      >
        {/* Quick Actions inside header card */}
        <TaskQuickActions
          status={status}
          blockerReason={blockerReason}
          isUpdating={updateStatus.isPending}
          onStatusChange={handleStatusChange}
          onUpdateField={updateField}
        />
      </TaskHeader>

      {/* Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 sm:gap-6">
        {/* Main Content - 2 cols */}
        <TaskContentSections
          task={task}
          status={status}
          technologies={technologies}
          tags={tags}
          learnings={learnings}
          relatedKnowledge={relatedKnowledge}
          onUpdateField={updateField}
        />

        {/* Sidebar - 1 col */}
        <TaskSidebar
          task={task}
          projectId={projectId}
          assignees={assignees}
          estimatedHours={estimatedHours}
          actualHours={actualHours}
          branchName={branchName}
          prUrl={prUrl}
          projectOptions={projectOptions}
          isDeleting={deleteEntity.isPending}
          onUpdateField={updateField}
          onDelete={handleDelete}
        />
      </div>
    </div>
  );
}

/**
 * Loading skeleton for TaskDetailPanel.
 */
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
