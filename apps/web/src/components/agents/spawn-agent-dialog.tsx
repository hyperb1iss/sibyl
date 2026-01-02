'use client';

import { useCallback, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
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
// Component
// =============================================================================

export function SpawnAgentDialog({ trigger, onSpawned }: SpawnAgentDialogProps) {
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState('');

  const { selectedProjects, isAll } = useProjectContext();
  const { data: projectsData } = useProjects();
  const spawnAgent = useSpawnAgent();

  // Get the current project (first selected, or null if "all")
  const projectId = isAll ? null : selectedProjects[0];
  const currentProject = projectsData?.entities.find(p => p.id === projectId);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();

      if (!projectId || !prompt.trim()) return;

      try {
        const result = await spawnAgent.mutateAsync({
          project_id: projectId,
          prompt: prompt.trim(),
          // Backend will auto-determine agent_type
        });

        if (result.success) {
          setOpen(false);
          setPrompt('');
          onSpawned?.(result.agent_id);
        }
      } catch (error) {
        console.error('Failed to spawn agent:', error);
      }
    },
    [projectId, prompt, spawnAgent, onSpawned]
  );

  const handleOpenChange = useCallback((newOpen: boolean) => {
    setOpen(newOpen);
    if (!newOpen) {
      setPrompt('');
    }
  }, []);

  // Check if we can spawn (need a single project selected)
  const canSpawn = projectId && prompt.trim();
  const needsProject = isAll || !projectId;

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
                'Describe what you want the agent to do'
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="my-6">
            {needsProject ? (
              <div className="text-center py-8">
                <p className="text-sc-fg-muted mb-2">Select a project first</p>
                <p className="text-sm text-sc-fg-subtle">
                  Use the project selector in the header to pick a project
                </p>
              </div>
            ) : (
              <Textarea
                id="prompt"
                value={prompt}
                onChange={e => setPrompt(e.target.value)}
                placeholder="What should the agent do?"
                rows={4}
                autoFocus
                className="resize-none"
              />
            )}
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
