'use client';

import { Folder, Loader2 } from 'lucide-react';
import { useState } from 'react';
import { useCreateEntity } from '@/lib/hooks/graph';

interface ProjectStepProps {
  onBack: () => void;
  onNext: (projectId: string) => void;
  onSkip: () => void;
}

export function ProjectStep({ onBack, onNext, onSkip }: ProjectStepProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const createEntity = useCreateEntity();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    const project = await createEntity.mutateAsync({
      name: name.trim(),
      description: description.trim() || undefined,
      entity_type: 'project',
    });
    onNext(project.id);
  };

  return (
    <div className="p-5">
      {/* Header */}
      <div className="text-center mb-6">
        <div className="relative inline-flex items-center justify-center mb-4">
          <div className="absolute w-16 h-16 rounded-full bg-sc-cyan/15 animate-pulse" />
          <div className="relative inline-flex items-center justify-center w-14 h-14 rounded-full bg-sc-cyan/20 text-sc-cyan ring-1 ring-sc-cyan/30">
            <Folder className="w-7 h-7" />
          </div>
        </div>
        <h2 className="text-xl font-semibold text-sc-fg-primary mb-2">Create Your First Project</h2>
        <p className="text-sc-fg-muted text-sm">Projects organize your tasks and knowledge</p>
      </div>

      {/* Form */}
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="project-name" className="block text-sm font-medium text-sc-fg-muted mb-2">
            Project Name <span className="text-sc-coral">*</span>
          </label>
          <input
            id="project-name"
            type="text"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="My First Project"
            className="w-full px-4 py-2.5 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg
                       text-sc-fg-primary placeholder:text-sc-fg-muted
                       focus-visible:border-sc-cyan focus-visible:outline-none focus-visible:ring-2
                       focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated
                       transition-colors duration-200"
          />
        </div>

        <div>
          <label
            htmlFor="project-description"
            className="block text-sm font-medium text-sc-fg-muted mb-2"
          >
            Description
          </label>
          <textarea
            id="project-description"
            value={description}
            onChange={e => setDescription(e.target.value)}
            placeholder="What's this project about?"
            rows={3}
            className="w-full px-4 py-2.5 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg
                       text-sc-fg-primary placeholder:text-sc-fg-muted
                       focus-visible:border-sc-cyan focus-visible:outline-none focus-visible:ring-2
                       focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated
                       transition-colors duration-200 resize-none"
          />
        </div>

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
            disabled={!name.trim() || createEntity.isPending}
            className="flex items-center gap-2 px-5 py-2.5 bg-sc-cyan hover:bg-sc-cyan/80 text-sc-bg-dark rounded-lg font-medium transition-colors duration-200 disabled:opacity-50 disabled:cursor-not-allowed shadow-glow-cyan focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          >
            {createEntity.isPending ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Creating...
              </>
            ) : (
              <>
                <Folder className="w-4 h-4" />
                Create Project
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
