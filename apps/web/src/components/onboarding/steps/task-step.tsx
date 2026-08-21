'use client';

import { CheckSquare, Loader2 } from 'lucide-react';
import { useState } from 'react';
import { useCreateEntity } from '@/lib/hooks/graph';

interface TaskStepProps {
  projectId: string | null;
  onBack: () => void;
  onNext: () => void;
  onSkip: () => void;
}

const PRIORITY_OPTIONS = [
  {
    value: 'low',
    label: 'Low',
    color: 'text-sc-fg-muted',
    bg: 'bg-sc-fg-subtle/10 border-sc-fg-subtle/30',
  },
  {
    value: 'medium',
    label: 'Medium',
    color: 'text-sc-cyan',
    bg: 'bg-sc-cyan/10 border-sc-cyan/30',
  },
  {
    value: 'high',
    label: 'High',
    color: 'text-sc-yellow',
    bg: 'bg-sc-yellow/10 border-sc-yellow/30',
  },
  {
    value: 'critical',
    label: 'Critical',
    color: 'text-sc-coral',
    bg: 'bg-sc-coral/10 border-sc-coral/30',
  },
] as const;

export function TaskStep({ projectId, onBack, onNext, onSkip }: TaskStepProps) {
  const [title, setTitle] = useState('');
  const [priority, setPriority] = useState<string>('medium');
  const createEntity = useCreateEntity();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    await createEntity.mutateAsync({
      name: title.trim(),
      entity_type: 'task',
      metadata: {
        priority,
        status: 'todo',
        ...(projectId && { project_id: projectId }),
      },
    });
    onNext();
  };

  return (
    <div className="p-5">
      {/* Header */}
      <div className="text-center mb-6">
        <div className="relative inline-flex items-center justify-center mb-4">
          <div className="absolute w-16 h-16 rounded-full bg-sc-green/15 animate-pulse" />
          <div className="relative inline-flex items-center justify-center w-14 h-14 rounded-full bg-sc-green/20 text-sc-green ring-1 ring-sc-green/30">
            <CheckSquare className="w-7 h-7" />
          </div>
        </div>
        <h2 className="text-xl font-semibold text-sc-fg-primary mb-2">Add Your First Task</h2>
        <p className="text-sc-fg-muted text-sm">
          {projectId ? 'What would you like to work on first?' : 'Create a task to get started'}
        </p>
      </div>

      {/* Form */}
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="task-title" className="block text-sm font-medium text-sc-fg-muted mb-2">
            Task Title <span className="text-sc-coral">*</span>
          </label>
          <input
            id="task-title"
            type="text"
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder="e.g., Set up development environment"
            className="w-full px-4 py-2.5 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg
                       text-sc-fg-primary placeholder:text-sc-fg-muted
                       focus-visible:border-sc-cyan focus-visible:outline-none focus-visible:ring-2
                       focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated
                       transition-colors duration-200"
          />
        </div>

        <fieldset>
          <legend className="block text-sm font-medium text-sc-fg-muted mb-2">Priority</legend>
          <div className="flex gap-2">
            {PRIORITY_OPTIONS.map(opt => (
              <button
                key={opt.value}
                type="button"
                aria-pressed={priority === opt.value}
                onClick={() => setPriority(opt.value)}
                className={`flex-1 px-3 py-2.5 rounded-lg border text-sm font-medium transition-colors duration-200
                  focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan
                  focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated
                  ${
                    priority === opt.value
                      ? `${opt.bg} ${opt.color}`
                      : 'bg-sc-bg-highlight border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-fg-subtle/40'
                  }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </fieldset>

        {/* Actions */}
        <div className="flex items-center justify-between pt-4 border-t border-sc-fg-subtle/10">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={onBack}
              className="rounded px-4 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            >
              Back
            </button>
            <button
              type="button"
              onClick={onSkip}
              className="rounded text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            >
              Skip for now
            </button>
          </div>
          <button
            type="submit"
            disabled={!title.trim() || createEntity.isPending}
            className="flex items-center gap-2 px-5 py-2.5 bg-sc-green hover:bg-sc-green/80 text-sc-bg-dark rounded-lg font-medium transition-colors duration-200 disabled:opacity-50 disabled:cursor-not-allowed shadow-glow-green focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          >
            {createEntity.isPending ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Creating...
              </>
            ) : (
              <>
                <CheckSquare className="w-4 h-4" />
                Create Task
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
