'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { TaskPriority } from '@/lib/api';
import { TASK_PRIORITIES, TASK_PRIORITY_CONFIG } from '@/lib/constants';

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
        className="relative w-full max-w-lg bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl shadow-2xl overflow-hidden"
        onKeyDown={handleKeyDown}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-sc-fg-subtle/20">
          <h2
            id="quick-task-title"
            className="text-lg font-semibold text-sc-fg-primary flex items-center gap-2"
          >
            <span className="text-sc-purple">☰</span>
            Quick Task
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            ✕
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
              className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
            />
          </div>

          {/* Description (optional) */}
          <div>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Add description (optional)"
              rows={2}
              className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all resize-none"
            />
          </div>

          {/* Project & Priority row */}
          <div className="flex gap-3">
            {/* Project select */}
            {projects && projects.length > 0 && (
              <div className="flex-1">
                <label
                  htmlFor="quick-task-project"
                  className="block text-xs text-sc-fg-subtle mb-1"
                >
                  Project
                </label>
                <select
                  id="quick-task-project"
                  value={projectId}
                  onChange={e => setProjectId(e.target.value)}
                  className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
                >
                  <option value="">No project</option>
                  {projects.map(p => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {/* Epic select - only show if epics exist */}
            {filteredEpics.length > 0 && (
              <div className="flex-1">
                <label htmlFor="quick-task-epic" className="block text-xs text-sc-fg-subtle mb-1">
                  <span className="text-[#ffb86c]">◈</span> Epic
                </label>
                <select
                  id="quick-task-epic"
                  value={epicId}
                  onChange={e => setEpicId(e.target.value)}
                  className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
                >
                  <option value="">No epic</option>
                  {filteredEpics.map(e => (
                    <option key={e.id} value={e.id}>
                      {e.name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {/* Priority select */}
            <div className={projects && projects.length > 0 ? 'w-32' : 'flex-1'}>
              <label htmlFor="quick-task-priority" className="block text-xs text-sc-fg-subtle mb-1">
                Priority
              </label>
              <select
                id="quick-task-priority"
                value={priority}
                onChange={e => setPriority(e.target.value as TaskPriority)}
                className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
              >
                {TASK_PRIORITIES.map(p => (
                  <option key={p} value={p}>
                    {TASK_PRIORITY_CONFIG[p]?.label ?? p}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Toggle advanced fields */}
          <button
            type="button"
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="text-sm text-sc-fg-subtle hover:text-sc-purple transition-colors flex items-center gap-1"
          >
            <span
              className="transition-transform duration-200"
              style={{ transform: showAdvanced ? 'rotate(90deg)' : 'rotate(0deg)' }}
            >
              ▶
            </span>
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
                    className="block text-xs text-sc-fg-subtle mb-1"
                  >
                    Feature / Tag
                  </label>
                  <input
                    id="quick-task-feature"
                    type="text"
                    value={feature}
                    onChange={e => setFeature(e.target.value)}
                    placeholder="e.g., auth, api, ui"
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
                  />
                </div>
                <div className="w-40">
                  <label htmlFor="quick-task-due" className="block text-xs text-sc-fg-subtle mb-1">
                    Due Date
                  </label>
                  <input
                    id="quick-task-due"
                    type="date"
                    value={dueDate}
                    onChange={e => setDueDate(e.target.value)}
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
                  />
                </div>
              </div>

              {/* Assignees & Hours row */}
              <div className="flex gap-3">
                <div className="flex-1">
                  <label
                    htmlFor="quick-task-assignees"
                    className="block text-xs text-sc-fg-subtle mb-1"
                  >
                    Assignees
                  </label>
                  <input
                    id="quick-task-assignees"
                    type="text"
                    value={assigneesInput}
                    onChange={e => setAssigneesInput(e.target.value)}
                    placeholder="Comma-separated names"
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
                  />
                </div>
                <div className="w-24">
                  <label
                    htmlFor="quick-task-hours"
                    className="block text-xs text-sc-fg-subtle mb-1"
                  >
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
                    className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
                  />
                </div>
              </div>
            </div>
          )}

          {/* Actions */}
          <div className="flex items-center justify-between pt-2">
            <div className="text-xs text-sc-fg-subtle">
              <kbd className="bg-sc-bg-highlight px-1.5 py-0.5 rounded">⌘</kbd>
              <span className="mx-1">+</span>
              <kbd className="bg-sc-bg-highlight px-1.5 py-0.5 rounded">↵</kbd>
              <span className="ml-1">to submit</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!title.trim() || isSubmitting}
                className="px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg font-medium transition-colors"
              >
                {isSubmitting ? 'Creating...' : 'Create Task'}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
