'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Button,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { ChevronRight, Layers, Menu, X } from '@/components/ui/icons';
import type { TaskPriority } from '@/lib/api';
import { TASK_PRIORITIES, TASK_PRIORITY_CONFIG } from '@/lib/constants';

// Radix Select forbids an empty-string item value, so the "none" options use a
// sentinel that maps back to '' when read.
const NONE_VALUE = '__none__';

export interface QuickTaskData {
  title: string;
  description?: string;
  priority: TaskPriority;
  projectId?: string;
  epicId?: string;
  feature?: string;
  assignees?: string[];
  dueDate?: string;
  estimatedHours?: number;
}

interface QuickTaskModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (task: QuickTaskData) => void;
  projects?: Array<{ id: string; name: string }>;
  epics?: Array<{ id: string; name: string; projectId?: string }>;
  defaultProjectId?: string;
  defaultEpicId?: string;
  isSubmitting?: boolean;
}

export function QuickTaskModal({
  isOpen,
  onClose,
  onSubmit,
  projects,
  epics,
  defaultProjectId,
  defaultEpicId,
  isSubmitting,
}: QuickTaskModalProps) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [priority, setPriority] = useState<TaskPriority>('medium');
  const [projectId, setProjectId] = useState(defaultProjectId ?? '');
  const [epicId, setEpicId] = useState(defaultEpicId ?? '');
  const [feature, setFeature] = useState('');
  const [assigneesInput, setAssigneesInput] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [estimatedHours, setEstimatedHours] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset form when opened
  useEffect(() => {
    if (isOpen) {
      setTitle('');
      setDescription('');
      setPriority('medium');
      setProjectId(defaultProjectId ?? '');
      setEpicId(defaultEpicId ?? '');
      setFeature('');
      setAssigneesInput('');
      setDueDate('');
      setEstimatedHours('');
      setShowAdvanced(false);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [isOpen, defaultProjectId, defaultEpicId]);

  // Filter epics by selected project
  const filteredEpics = epics?.filter(e => !projectId || e.projectId === projectId) ?? [];

  // Reset epic if it doesn't belong to selected project
  useEffect(() => {
    if (epicId && projectId) {
      const epicBelongsToProject = epics?.find(e => e.id === epicId)?.projectId === projectId;
      if (!epicBelongsToProject) {
        setEpicId('');
      }
    }
  }, [projectId, epicId, epics]);

  const handleSubmit = useCallback(
    (e?: React.FormEvent) => {
      e?.preventDefault();
      if (!title.trim()) return;

      const assignees = assigneesInput
        .split(',')
        .map(a => a.trim())
        .filter(Boolean);

      onSubmit({
        title: title.trim(),
        description: description.trim() || undefined,
        priority,
        projectId: projectId || undefined,
        epicId: epicId || undefined,
        feature: feature.trim() || undefined,
        assignees: assignees.length > 0 ? assignees : undefined,
        dueDate: dueDate || undefined,
        estimatedHours: estimatedHours ? Number(estimatedHours) : undefined,
      });
    },
    [
      title,
      description,
      priority,
      projectId,
      epicId,
      feature,
      assigneesInput,
      dueDate,
      estimatedHours,
      onSubmit,
    ]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
      // Cmd/Ctrl+Enter to submit
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        handleSubmit();
      }
    },
    [onClose, handleSubmit]
  );

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[10vh]"
      role="presentation"
    >
      {/* Backdrop */}
      <button
        type="button"
        className="absolute inset-0 bg-sc-bg-dark/80 backdrop-blur-sm cursor-default"
        onClick={onClose}
        aria-label="Close modal"
      />

      {/* Modal */}
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="quick-task-title"
        className="relative w-full max-w-lg bg-sc-bg-elevated border border-sc-fg-subtle/30 rounded-xl shadow-card-elevated overflow-hidden"
        onKeyDown={handleKeyDown}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-sc-fg-subtle/20">
          <h2
            id="quick-task-title"
            className="text-lg font-semibold text-sc-fg-primary flex items-center gap-2"
          >
            <Menu width={18} height={18} className="text-sc-purple" />
            Quick Task
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-1 text-sc-fg-muted hover:text-sc-fg-primary transition-colors duration-200
                       focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          >
            <X width={18} height={18} />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          {/* Title */}
          <div>
            <input
              ref={inputRef}
              type="text"
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="What needs to be done?"
              aria-label="Task title"
              className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-muted transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            />
          </div>

          {/* Description (optional) */}
          <div>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Add description (optional)"
              rows={2}
              aria-label="Task description"
              className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-muted transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated resize-none"
            />
          </div>

          {/* Project & Priority row */}
          <div className="flex gap-3">
            {/* Project select */}
            {projects && projects.length > 0 && (
              <div className="flex-1">
                <label htmlFor="quick-task-project" className="block text-xs text-sc-fg-muted mb-1">
                  Project
                </label>
                <Select
                  value={projectId || NONE_VALUE}
                  onValueChange={v => setProjectId(v === NONE_VALUE ? '' : v)}
                >
                  <SelectTrigger id="quick-task-project" aria-label="Project" className="min-w-0">
                    <SelectValue placeholder="No project" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={NONE_VALUE}>No project</SelectItem>
                    {projects.map(p => (
                      <SelectItem key={p.id} value={p.id}>
                        {p.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* Epic select - only show if epics exist */}
            {filteredEpics.length > 0 && (
              <div className="flex-1">
                <label htmlFor="quick-task-epic" className="block text-xs text-sc-fg-muted mb-1">
                  <Layers width={12} height={12} className="inline text-sc-orange" /> Epic
                </label>
                <Select
                  value={epicId || NONE_VALUE}
                  onValueChange={v => setEpicId(v === NONE_VALUE ? '' : v)}
                >
                  <SelectTrigger id="quick-task-epic" aria-label="Epic" className="min-w-0">
                    <SelectValue placeholder="No epic" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={NONE_VALUE}>No epic</SelectItem>
                    {filteredEpics.map(e => (
                      <SelectItem key={e.id} value={e.id}>
                        {e.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* Priority select */}
            <div className={projects && projects.length > 0 ? 'w-32' : 'flex-1'}>
              <label htmlFor="quick-task-priority" className="block text-xs text-sc-fg-muted mb-1">
                Priority
              </label>
              <Select value={priority} onValueChange={v => setPriority(v as TaskPriority)}>
                <SelectTrigger id="quick-task-priority" aria-label="Priority" className="min-w-0">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TASK_PRIORITIES.map(p => (
                    <SelectItem key={p} value={p}>
                      {TASK_PRIORITY_CONFIG[p]?.label ?? p}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {/* Toggle advanced fields */}
          <button
            type="button"
            onClick={() => setShowAdvanced(!showAdvanced)}
            aria-expanded={showAdvanced}
            className="rounded-lg text-sm text-sc-fg-muted hover:text-sc-purple transition-colors duration-200 flex items-center gap-1
                       focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          >
            <ChevronRight
              width={14}
              height={14}
              className={`transition-transform duration-200 ${showAdvanced ? 'rotate-90' : ''}`}
            />
            {showAdvanced ? 'Hide' : 'Show'} more options
          </button>

          {/* Advanced fields */}
          {showAdvanced && (
            <div className="space-y-4 pt-2 border-t border-sc-fg-subtle/10">
              {/* Feature & Due Date row */}
              <div className="flex gap-3">
                <div className="flex-1">
                  <label
                    htmlFor="quick-task-feature"
                    className="block text-xs text-sc-fg-muted mb-1"
                  >
                    Feature / Tag
                  </label>
                  <input
                    id="quick-task-feature"
                    type="text"
                    value={feature}
                    onChange={e => setFeature(e.target.value)}
                    placeholder="e.g., auth, api, ui"
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-muted transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
                  />
                </div>
                <div className="w-40">
                  <label htmlFor="quick-task-due" className="block text-xs text-sc-fg-muted mb-1">
                    Due Date
                  </label>
                  <input
                    id="quick-task-due"
                    type="date"
                    value={dueDate}
                    onChange={e => setDueDate(e.target.value)}
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
                  />
                </div>
              </div>

              {/* Assignees & Hours row */}
              <div className="flex gap-3">
                <div className="flex-1">
                  <label
                    htmlFor="quick-task-assignees"
                    className="block text-xs text-sc-fg-muted mb-1"
                  >
                    Assignees
                  </label>
                  <input
                    id="quick-task-assignees"
                    type="text"
                    value={assigneesInput}
                    onChange={e => setAssigneesInput(e.target.value)}
                    placeholder="Comma-separated names"
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-muted transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
                  />
                </div>
                <div className="w-24">
                  <label htmlFor="quick-task-hours" className="block text-xs text-sc-fg-muted mb-1">
                    Est. Hours
                  </label>
                  <input
                    id="quick-task-hours"
                    type="number"
                    min="0"
                    step="0.5"
                    value={estimatedHours}
                    onChange={e => setEstimatedHours(e.target.value)}
                    placeholder="0"
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-muted transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
                  />
                </div>
              </div>
            </div>
          )}

          {/* Actions */}
          <div className="flex items-center justify-between pt-2">
            <div className="text-xs text-sc-fg-muted">
              <kbd className="bg-sc-bg-highlight px-1.5 py-0.5 rounded">⌘</kbd>
              <span className="mx-1">+</span>
              <kbd className="bg-sc-bg-highlight px-1.5 py-0.5 rounded">↵</kbd>
              <span className="ml-1">to submit</span>
            </div>
            <div className="flex items-center gap-2">
              <Button type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                disabled={!title.trim()}
                loading={isSubmitting}
              >
                {isSubmitting ? 'Creating...' : 'Create Task'}
              </Button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
