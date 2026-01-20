'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Check, ChevronDown, Folder } from '@/components/ui/icons';
import { Textarea } from '@/components/ui/input';
import { useProjects, useSpawnAgent } from '@/lib/hooks';
import { useProjectContext } from '@/lib/project-context';

// =============================================================================
// Types
// =============================================================================

interface SpawnAgentDialogProps {
  /** Trigger element for opening the dialog */
  trigger: React.ReactNode;
  /** Callback when agent is spawned */
  onSpawned?: (agentId: string) => void;
}

// =============================================================================
// Inline Project Selector (single-select)
// =============================================================================

interface ProjectSelectProps {
  projects: Array<{ id: string; name: string; metadata?: Record<string, unknown> }>;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function ProjectSelect({ projects, selectedId, onSelect }: ProjectSelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [isOpen]);

  const selectedProject = projects.find(p => p.id === selectedId);

  return (
    <div ref={dropdownRef} className="relative">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className={`
          w-full flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm font-medium
          transition-all duration-200
          ${
            selectedId
              ? 'bg-sc-purple/10 text-sc-purple border-sc-purple/30'
              : 'bg-sc-bg-elevated text-sc-fg-muted border-sc-fg-subtle/20 hover:border-sc-purple/40'
          }
          border
        `}
      >
        <Folder width={16} height={16} className="shrink-0" />
        <span className="flex-1 text-left truncate">
          {selectedProject?.name ?? 'Select a project...'}
        </span>
        <ChevronDown
          width={14}
          height={14}
          className={`shrink-0 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {isOpen && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg shadow-lg overflow-hidden z-50 animate-fade-in">
          <div className="max-h-48 overflow-y-auto">
            {projects.length === 0 ? (
              <div className="px-4 py-6 text-center text-sc-fg-subtle text-sm">No projects</div>
            ) : (
              projects.map(project => {
                const isSelected = project.id === selectedId;
                const taskCount = (project.metadata?.task_count as number) ?? 0;

                return (
                  <button
                    key={project.id}
                    type="button"
                    onClick={() => {
                      onSelect(project.id);
                      setIsOpen(false);
                    }}
                    className={`
                      w-full flex items-center gap-3 px-4 py-2.5 text-sm text-left
                      transition-colors
                      ${isSelected ? 'bg-sc-purple/10 text-sc-purple' : 'text-sc-fg-primary hover:bg-sc-bg-elevated'}
                    `}
                  >
                    <div
                      className={`
                        w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0
                        ${isSelected ? 'border-sc-purple bg-sc-purple' : 'border-sc-fg-subtle/40'}
                      `}
                    >
                      {isSelected && <Check width={10} height={10} className="text-white" />}
                    </div>
                    <span className="flex-1 truncate font-medium">{project.name}</span>
                    {taskCount > 0 && (
                      <span className="text-xs text-sc-fg-subtle shrink-0">{taskCount} tasks</span>
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Component
// =============================================================================

export function SpawnAgentDialog({ trigger, onSpawned }: SpawnAgentDialogProps) {
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [manualProjectId, setManualProjectId] = useState<string | null>(null);

  const { selectedProjects, isAll } = useProjectContext();
  const { data: projectsData, isLoading: projectsLoading, error: projectsError } = useProjects();
  const spawnAgent = useSpawnAgent();

  // Sort projects by recency
  const projects = useMemo(() => {
    return [...(projectsData?.entities ?? [])].sort((a, b) => {
      const aActivity = a.metadata?.last_activity_at || a.metadata?.updated_at;
      const bActivity = b.metadata?.last_activity_at || b.metadata?.updated_at;
      const aTime = aActivity ? new Date(aActivity as string).getTime() : 0;
      const bTime = bActivity ? new Date(bActivity as string).getTime() : 0;
      return bTime - aTime;
    });
  }, [projectsData]);

  // Need project selector if "All" is selected or multiple projects selected
  const needsProjectSelector = isAll || selectedProjects.length !== 1;

  // Get the effective project ID (from context if single, from manual selection if multi/all)
  const effectiveProjectId = needsProjectSelector ? manualProjectId : selectedProjects[0];
  const currentProject = projects.find(p => p.id === effectiveProjectId);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();

      if (!effectiveProjectId || !prompt.trim()) return;

      try {
        const result = await spawnAgent.mutateAsync({
          project_id: effectiveProjectId,
          prompt: prompt.trim(),
          // Backend will auto-determine agent_type
        });

        if (result.success) {
          setOpen(false);
          setPrompt('');
          setManualProjectId(null);
          onSpawned?.(result.agent_id);
        }
      } catch (error) {
        console.error('Failed to spawn agent:', error);
      }
    },
    [effectiveProjectId, prompt, spawnAgent, onSpawned]
  );

  const handleOpenChange = useCallback((newOpen: boolean) => {
    setOpen(newOpen);
    if (!newOpen) {
      setPrompt('');
      setManualProjectId(null);
    }
  }, []);

  // Check if we can spawn (need a project and prompt)
  const canSpawn = effectiveProjectId && prompt.trim();

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent size="md">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>New Agent</DialogTitle>
            <DialogDescription>
              {currentProject ? (
                <>
                  Start an agent to work on{' '}
                  <span className="text-sc-purple font-medium">{currentProject.name}</span>
                </>
              ) : (
                'Select a project and describe what you want the agent to do'
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="my-6 space-y-4">
            {/* Project Selector (shown when "All" or multiple projects selected) */}
            {needsProjectSelector && (
              <div>
                <span className="block text-sm font-medium text-sc-fg-muted mb-2">Project</span>
                {projectsLoading ? (
                  <div className="w-full px-3 py-2.5 rounded-lg text-sm bg-sc-bg-elevated text-sc-fg-muted border border-sc-fg-subtle/20">
                    Loading projects...
                  </div>
                ) : projectsError ? (
                  <div className="w-full px-3 py-2.5 rounded-lg text-sm bg-sc-red/10 text-sc-red border border-sc-red/20">
                    Failed to load projects
                  </div>
                ) : (
                  <ProjectSelect
                    projects={projects}
                    selectedId={manualProjectId}
                    onSelect={setManualProjectId}
                  />
                )}
              </div>
            )}

            {/* Prompt Input */}
            <div>
              {needsProjectSelector && (
                <label htmlFor="prompt" className="block text-sm font-medium text-sc-fg-muted mb-2">
                  Task
                </label>
              )}
              <Textarea
                id="prompt"
                value={prompt}
                onChange={e => setPrompt(e.target.value)}
                placeholder="What should the agent do?"
                rows={4}
                autoFocus={!needsProjectSelector}
                className="resize-none"
              />
            </div>
          </div>

          <DialogFooter>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="px-4 py-2 text-sm font-medium text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSpawn || spawnAgent.isPending}
              className="px-4 py-2 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 disabled:bg-sc-fg-subtle/20 disabled:text-sc-fg-muted text-white rounded-lg transition-colors"
            >
              {spawnAgent.isPending ? 'Starting...' : 'Start'}
            </button>
          </DialogFooter>
        </form>

        {/* Error message */}
        {spawnAgent.isError && (
          <div className="mt-4 p-3 bg-sc-red/10 border border-sc-red/20 rounded-lg text-sm text-sc-red">
            Failed to start agent:{' '}
            {spawnAgent.error instanceof Error ? spawnAgent.error.message : 'Unknown error'}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
