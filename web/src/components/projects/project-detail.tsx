'use client';

import Link from 'next/link';
import type { TaskSummary } from '@/lib/api';
import { TASK_STATUS_CONFIG, TASK_STATUSES } from '@/lib/constants';
import { Button, ColorButton } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';

interface ProjectDetailProps {
  project: TaskSummary;
  tasks: TaskSummary[];
}

export function ProjectDetail({ project, tasks }: ProjectDetailProps) {
  // Calculate task counts by status
  const tasksByStatus = TASK_STATUSES.reduce(
    (acc, status) => {
      acc[status] = tasks.filter((t) => t.metadata.status === status).length;
      return acc;
    },
    {} as Record<string, number>
  );

  const totalTasks = tasks.length;
  const doneTasks = tasksByStatus.done || 0;
  const doingTasks = tasksByStatus.doing || 0;
  const blockedTasks = tasksByStatus.blocked || 0;
  const reviewTasks = tasksByStatus.review || 0;

  const progress = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  // Extract metadata
  const techStack = (project.metadata.technologies as string[]) ??
    (project.metadata.tech_stack as string[]) ?? [];
  const repoUrl = project.metadata.repository_url as string | undefined;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <h1 className="text-2xl font-bold text-sc-fg-primary">{project.name}</h1>
          {project.description && (
            <p className="text-sc-fg-muted mt-2">{project.description}</p>
          )}
        </div>

        {/* Quick Actions */}
        <div className="flex items-center gap-2">
          <Link href={`/tasks?project=${project.id}`}>
            <Button variant="secondary">View Board</Button>
          </Link>
        </div>
      </div>

      {/* Progress Overview */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold text-sc-fg-primary">Progress</h2>
          <span className="text-2xl font-bold text-sc-green">{progress}%</span>
        </div>
        <Progress value={progress} color="green" className="h-3" />
        <div className="flex justify-between mt-2 text-sm text-sc-fg-muted">
          <span>{doneTasks} completed</span>
          <span>{totalTasks} total tasks</span>
        </div>
      </div>

      {/* Task Status Grid */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {TASK_STATUSES.map((status) => {
          const config = TASK_STATUS_CONFIG[status];
          const count = tasksByStatus[status] || 0;

          return (
            <div
              key={status}
              className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg p-3 text-center"
            >
              <span className={`text-2xl ${config.textClass}`}>{config.icon}</span>
              <div className="text-2xl font-bold text-sc-fg-primary mt-1">{count}</div>
              <div className="text-xs text-sc-fg-muted">{config.label}</div>
            </div>
          );
        })}
      </div>

      {/* Active Work Section */}
      {(doingTasks > 0 || blockedTasks > 0 || reviewTasks > 0) && (
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <h2 className="font-semibold text-sc-fg-primary mb-4">Active Work</h2>
          <div className="space-y-3">
            {tasks
              .filter((t) => ['doing', 'blocked', 'review'].includes(t.metadata.status ?? ''))
              .slice(0, 5)
              .map((task) => {
                const status = task.metadata.status ?? 'todo';
                const config = TASK_STATUS_CONFIG[status as keyof typeof TASK_STATUS_CONFIG];

                return (
                  <div
                    key={task.id}
                    className="flex items-center gap-3 p-2 rounded-lg bg-sc-bg-highlight/30"
                  >
                    <span className={config?.textClass}>{config?.icon}</span>
                    <span className="flex-1 text-sm text-sc-fg-primary truncate">
                      {task.name}
                    </span>
                    <span className={`text-xs px-2 py-0.5 rounded ${config?.bgClass} ${config?.textClass}`}>
                      {config?.label}
                    </span>
                  </div>
                );
              })}
          </div>
        </div>
      )}

      {/* Tech Stack & Links */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {techStack.length > 0 && (
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
            <h2 className="font-semibold text-sc-fg-primary mb-3">Tech Stack</h2>
            <div className="flex flex-wrap gap-2">
              {techStack.map((tech, i) => (
                <span
                  key={i}
                  className="text-xs px-2 py-1 rounded-md bg-sc-cyan/10 text-sc-cyan border border-sc-cyan/20"
                >
                  {tech}
                </span>
              ))}
            </div>
          </div>
        )}

        {repoUrl && (
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
            <h2 className="font-semibold text-sc-fg-primary mb-3">Repository</h2>
            <a
              href={repoUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sc-cyan hover:underline text-sm break-all"
            >
              {repoUrl}
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

export function ProjectDetailSkeleton() {
  return (
    <div className="space-y-6 animate-pulse">
      <div>
        <div className="h-8 w-1/3 bg-sc-bg-elevated rounded" />
        <div className="h-4 w-2/3 bg-sc-bg-elevated rounded mt-3" />
      </div>
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
        <div className="h-4 w-20 bg-sc-bg-elevated rounded mb-3" />
        <div className="h-3 w-full bg-sc-bg-elevated rounded" />
      </div>
      <div className="grid grid-cols-6 gap-3">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg p-3">
            <div className="h-6 w-6 bg-sc-bg-elevated rounded mx-auto" />
            <div className="h-6 w-8 bg-sc-bg-elevated rounded mx-auto mt-2" />
          </div>
        ))}
      </div>
    </div>
  );
}
