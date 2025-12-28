'use client';

import { CheckSquare, Loader2 } from 'lucide-react';
import { useState } from 'react';
import { useCreateEntity } from '@/lib/hooks';

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
    color: 'text-sc-fg-subtle',
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
            className="w-full px-4 py-2.5 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-xl
                       text-sc-fg-primary placeholder:text-sc-fg-subtle
                       focus:border-sc-green focus:outline-none focus:ring-1 focus:ring-sc-green/30
                       transition-colors"
          />
        </div>

        <fieldset>
          <legend className="block text-sm font-medium text-sc-fg-muted mb-2">Priority</legend>
          <div className="flex gap-2">
            {PRIORITY_OPTIONS.map(opt => (
              <button
                key={opt.value}
                type="button"
                onClick={() => setPriority(opt.value)}
                className={`flex-1 px-3 py-2.5 rounded-xl border text-sm font-medium transition-all
                  ${
                    priority === opt.value
                      ? `${opt.bg} ${opt.color}`
                      : 'bg-sc-bg-dark border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-fg-subtle/40'
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
              className="px-4 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
            >
              Back
            </button>
            <button
              type="button"
              onClick={onSkip}
              className="text-sc-fg-subtle hover:text-sc-fg-muted transition-colors text-sm"
            >
              Skip for now
            </button>
          </div>
          <button
            type="submit"
            disabled={!title.trim() || createEntity.isPending}
            className="flex items-center gap-2 px-5 py-2.5 bg-sc-green hover:bg-sc-green/80 text-sc-bg-dark rounded-xl font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-lg shadow-sc-green/25"
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
