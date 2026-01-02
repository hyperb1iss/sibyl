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
import { Label, Textarea } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { AgentType } from '@/lib/api';
import { AGENT_TYPE_CONFIG, type AgentTypeValue } from '@/lib/constants';
import { useProjects, useSpawnAgent, useTasks } from '@/lib/hooks';

// =============================================================================
// Types
// =============================================================================

interface SpawnAgentDialogProps {
  /** Trigger element for opening the dialog */
  trigger: React.ReactNode;
  /** Optional pre-selected project ID */
  defaultProjectId?: string;
  /** Optional pre-selected task ID */
  defaultTaskId?: string;
  /** Callback when agent is spawned */
  onSpawned?: (agentId: string) => void;
}

// =============================================================================
// Component
// =============================================================================

export function SpawnAgentDialog({
  trigger,
  defaultProjectId,
  defaultTaskId,
  onSpawned,
}: SpawnAgentDialogProps) {
  const [open, setOpen] = useState(false);
  const [projectId, setProjectId] = useState(defaultProjectId ?? '');
  const [taskId, setTaskId] = useState(defaultTaskId ?? '');
  const [agentType, setAgentType] = useState<AgentType>('general');
  const [prompt, setPrompt] = useState('');
  const [createWorktree, setCreateWorktree] = useState(false);
  const [requestReview, setRequestReview] = useState(true);

  const { data: projectsData, isLoading: projectsLoading } = useProjects();
  const { data: tasksData, isLoading: tasksLoading } = useTasks(
    projectId ? { project: projectId, status: 'doing' } : undefined
  );
  const spawnAgent = useSpawnAgent();

  const projects = projectsData?.entities ?? [];
  const tasks = tasksData?.entities ?? [];

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();

      if (!projectId || !prompt.trim()) return;

      try {
        const result = await spawnAgent.mutateAsync({
          project_id: projectId,
          prompt: prompt.trim(),
          agent_type: agentType,
          task_id: taskId || undefined,
        });

        if (result.success) {
          setOpen(false);
          setPrompt('');
          setTaskId('');
          onSpawned?.(result.agent_id);
        }
      } catch (error) {
        console.error('Failed to spawn agent:', error);
      }
    },
    [projectId, prompt, agentType, taskId, spawnAgent, onSpawned]
  );

  const handleOpenChange = useCallback(
    (newOpen: boolean) => {
      setOpen(newOpen);
      if (!newOpen) {
        // Reset form on close
        setPrompt('');
        if (!defaultProjectId) setProjectId('');
        if (!defaultTaskId) setTaskId('');
      }
    },
    [defaultProjectId, defaultTaskId]
  );

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent size="lg">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Start New Agent</DialogTitle>
            <DialogDescription>
              Spawn an AI agent to work on a task. The agent will operate autonomously within your
              project.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 my-6">
            {/* Project Selector */}
            <div>
              <Label htmlFor="project" required>
                Project
              </Label>
              <Select
                value={projectId}
                onValueChange={setProjectId}
                disabled={projectsLoading || !!defaultProjectId}
              >
                <SelectTrigger id="project">
                  <SelectValue placeholder="Select a project..." />
                </SelectTrigger>
                <SelectContent>
                  {projects.map(project => (
                    <SelectItem key={project.id} value={project.id}>
                      {project.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Agent Type Selector */}
            <div>
              <Label htmlFor="agent-type">Agent Type</Label>
              <Select value={agentType} onValueChange={v => setAgentType(v as AgentType)}>
                <SelectTrigger id="agent-type">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(
                    Object.entries(AGENT_TYPE_CONFIG) as [
                      AgentTypeValue,
                      (typeof AGENT_TYPE_CONFIG)[AgentTypeValue],
                    ][]
                  ).map(([type, config]) => (
                    <SelectItem key={type} value={type}>
                      <span className="flex items-center gap-2">
                        <span style={{ color: config.color }}>{config.icon}</span>
                        {config.label}
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Task Selector (optional) */}
            {projectId && (
              <div>
                <Label htmlFor="task" description="Optionally attach the agent to an existing task">
                  Attach to Task
                </Label>
                <Select
                  value={taskId || '__none__'}
                  onValueChange={v => setTaskId(v === '__none__' ? '' : v)}
                  disabled={tasksLoading || !!defaultTaskId}
                >
                  <SelectTrigger id="task">
                    <SelectValue placeholder="No task (standalone agent)" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">No task (standalone agent)</SelectItem>
                    {tasks.map(task => (
                      <SelectItem key={task.id} value={task.id}>
                        {task.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* Prompt */}
            <div>
              <Label htmlFor="prompt" required>
                Instructions
              </Label>
              <Textarea
                id="prompt"
                value={prompt}
                onChange={e => setPrompt(e.target.value)}
                placeholder="Describe what you want the agent to do..."
                rows={4}
              />
            </div>

            {/* Options */}
            <div className="space-y-3 pt-2">
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={createWorktree}
                  onChange={e => setCreateWorktree(e.target.checked)}
                  className="w-4 h-4 rounded border-sc-fg-subtle/40 bg-sc-bg-highlight text-sc-purple focus:ring-sc-purple/20"
                />
                <div>
                  <span className="text-sm text-sc-fg-primary">Create isolated worktree</span>
                  <p className="text-xs text-sc-fg-muted">
                    Agent will work in a separate git worktree to avoid conflicts
                  </p>
                </div>
              </label>

              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={requestReview}
                  onChange={e => setRequestReview(e.target.checked)}
                  className="w-4 h-4 rounded border-sc-fg-subtle/40 bg-sc-bg-highlight text-sc-purple focus:ring-sc-purple/20"
                />
                <div>
                  <span className="text-sm text-sc-fg-primary">Request review before merging</span>
                  <p className="text-xs text-sc-fg-muted">
                    Agent will pause for human approval before finalizing changes
                  </p>
                </div>
              </label>
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
              disabled={!projectId || !prompt.trim() || spawnAgent.isPending}
              className="px-4 py-2 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 disabled:bg-sc-fg-subtle/20 disabled:text-sc-fg-muted text-white rounded-lg transition-colors"
            >
              {spawnAgent.isPending ? 'Starting...' : 'Start Agent'}
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
