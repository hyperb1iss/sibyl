'use client';

import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { EditableTags, EditableText } from '@/components/editable';
import { Breadcrumb, ROUTE_CONFIG } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { VelocityLineChart } from '@/components/metrics/charts';
import { ProjectsEmptyState } from '@/components/ui/empty-state';
import {
  AlertTriangle,
  Archive,
  ArrowDownAZ,
  BarChart3,
  CheckCircle2,
  Clock,
  FolderKanban,
  GitBranch,
  Pause,
  Plus,
  Sparkles,
  Trash,
  TrendingUp,
  Zap,
} from '@/components/ui/icons';
import { ErrorState, Tooltip } from '@/components/ui/tooltip';
import type { TaskListResponse, TaskStatus, TaskSummary } from '@/lib/api';
import { TASK_STATUS_CONFIG } from '@/lib/constants';
import {
  useDeleteEntity,
  useProjectMetrics,
  useProjects,
  useTasks,
  useUpdateEntity,
} from '@/lib/hooks';

interface ProjectsContentProps {
  initialProjects: TaskListResponse;
}

// Project sort options
type ProjectSortOption = 'active' | 'name' | 'tasks' | 'completion' | 'urgent';

const PROJECT_SORT_OPTIONS: Array<{
  value: ProjectSortOption;
  label: string;
  icon: React.ReactNode;
}> = [
  { value: 'active', label: 'Most Active', icon: <TrendingUp width={14} height={14} /> },
  { value: 'urgent', label: 'Urgent First', icon: <Zap width={14} height={14} /> },
  { value: 'tasks', label: 'Most Tasks', icon: <BarChart3 width={14} height={14} /> },
  { value: 'completion', label: 'Completion %', icon: <CheckCircle2 width={14} height={14} /> },
  { value: 'name', label: 'Name', icon: <ArrowDownAZ width={14} height={14} /> },
];

interface ProjectStats {
  total: number;
  done: number;
  doing: number;
  blocked: number;
  review: number;
  todo: number;
  backlog: number;
  critical: number;
  high: number;
  overdue: number;
}

// Calculate stats for a project from its tasks
function calculateProjectStats(tasks: TaskSummary[]): ProjectStats {
  const stats: ProjectStats = {
    total: tasks.length,
    done: 0,
    doing: 0,
    blocked: 0,
    review: 0,
    todo: 0,
    backlog: 0,
    critical: 0,
    high: 0,
    overdue: 0,
  };

  const now = new Date();

  for (const task of tasks) {
    const status = task.metadata.status as TaskStatus | undefined;
    const priority = task.metadata.priority as string | undefined;
    const dueDate = task.metadata.due_date as string | undefined;

    // Count by status
    if (status === 'done') stats.done++;
    else if (status === 'doing') stats.doing++;
    else if (status === 'blocked') stats.blocked++;
    else if (status === 'review') stats.review++;
    else if (status === 'todo') stats.todo++;
    else if (status === 'backlog') stats.backlog++;

    // Count urgent priorities (only for open tasks)
    const isOpen = status && !['done', 'archived'].includes(status);
    if (isOpen && priority === 'critical') stats.critical++;
    if (isOpen && priority === 'high') stats.high++;

    // Count overdue (not done, has due date in past)
    if (dueDate && status !== 'done' && new Date(dueDate) < now) {
      stats.overdue++;
    }
  }

  return stats;
}

export function ProjectsContent({ initialProjects }: ProjectsContentProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [sortBy, setSortBy] = useState<ProjectSortOption>('active');
  const [showArchived, setShowArchived] = useState(false);

  const selectedProjectId = searchParams.get('id');

  // Fetch projects
  const {
    data: projectsData,
    isLoading: projectsLoading,
    error: projectsError,
  } = useProjects({ includeArchived: showArchived }, initialProjects);

  // Fetch ALL tasks (not filtered) to calculate counts per project
  const { data: allTasksData, isLoading: tasksLoading } = useTasks();

  const projects = projectsData?.entities ?? [];
  const allTasks = allTasksData?.entities ?? [];

  // Group tasks by project and calculate stats
  const projectStatsMap = useMemo(() => {
    const map = new Map<string, ProjectStats>();

    // Group tasks by project_id
    const tasksByProject = new Map<string, TaskSummary[]>();
    for (const task of allTasks) {
      const projectId = task.metadata.project_id as string | undefined;
      if (projectId) {
        const existing = tasksByProject.get(projectId) ?? [];
        existing.push(task);
        tasksByProject.set(projectId, existing);
      }
    }

    // Calculate stats for each project
    for (const project of projects) {
      const projectTasks = tasksByProject.get(project.id) ?? [];
      map.set(project.id, calculateProjectStats(projectTasks));
    }

    return map;
  }, [projects, allTasks]);

  // Sort projects based on selected sort option
  const sortedProjects = useMemo(() => {
    const sorted = [...projects];
    sorted.sort((a, b) => {
      const statsA = projectStatsMap.get(a.id);
      const statsB = projectStatsMap.get(b.id);

      // Helper for secondary sort: by total tasks, then name
      const secondarySort = () => {
        const totalDiff = (statsB?.total ?? 0) - (statsA?.total ?? 0);
        return totalDiff !== 0 ? totalDiff : a.name.localeCompare(b.name);
      };

      switch (sortBy) {
        case 'active': {
          // Most active = doing + blocked + review
          const activeA = (statsA?.doing ?? 0) + (statsA?.blocked ?? 0) + (statsA?.review ?? 0);
          const activeB = (statsB?.doing ?? 0) + (statsB?.blocked ?? 0) + (statsB?.review ?? 0);
          return activeB !== activeA ? activeB - activeA : secondarySort();
        }
        case 'urgent': {
          // Urgent first = critical + high + overdue
          const urgentA = (statsA?.critical ?? 0) + (statsA?.high ?? 0) + (statsA?.overdue ?? 0);
          const urgentB = (statsB?.critical ?? 0) + (statsB?.high ?? 0) + (statsB?.overdue ?? 0);
          return urgentB !== urgentA ? urgentB - urgentA : secondarySort();
        }
        case 'tasks':
          return (statsB?.total ?? 0) - (statsA?.total ?? 0) || a.name.localeCompare(b.name);
        case 'completion': {
          const completionA = statsA?.total ? (statsA.done / statsA.total) * 100 : 0;
          const completionB = statsB?.total ? (statsB.done / statsB.total) * 100 : 0;
          return completionB !== completionA ? completionB - completionA : secondarySort();
        }
        case 'name':
          return a.name.localeCompare(b.name);
        default:
          return 0;
      }
    });
    return sorted;
  }, [projects, projectStatsMap, sortBy]);

  // Get tasks for selected project
  const selectedProjectTasks = useMemo(() => {
    if (!selectedProjectId) return [];
    return allTasks.filter(t => t.metadata.project_id === selectedProjectId);
  }, [allTasks, selectedProjectId]);

  // Auto-select first project if none selected (use sorted order)
  const effectiveSelectedId = selectedProjectId ?? sortedProjects[0]?.id ?? null;
  const selectedProject = sortedProjects.find(p => p.id === effectiveSelectedId);
  const selectedStats = effectiveSelectedId ? projectStatsMap.get(effectiveSelectedId) : null;

  const handleSelectProject = useCallback(
    (projectId: string) => {
      const params = new URLSearchParams(searchParams);
      params.set('id', projectId);
      router.push(`/projects?${params.toString()}`);
    },
    [router, searchParams]
  );

  // Build breadcrumb items (only needed when viewing a specific project)
  const breadcrumbItems = selectedProject
    ? [
        { label: ROUTE_CONFIG[''].label, href: '/', icon: ROUTE_CONFIG[''].icon },
        { label: 'Projects', href: '/projects', icon: ROUTE_CONFIG.projects.icon },
        { label: selectedProject.name },
      ]
    : undefined;

  const isLoading = projectsLoading || tasksLoading;

  // Calculate total stats across all projects (must be before early returns)
  const totalStats = useMemo(() => {
    const stats = { tasks: 0, active: 0, urgent: 0 };
    for (const s of projectStatsMap.values()) {
      stats.tasks += s.total;
      stats.active += s.doing + s.review;
      stats.urgent += s.critical + s.high;
    }
    return stats;
  }, [projectStatsMap]);

  if (projectsError) {
    return (
      <div className="space-y-4">
        <Breadcrumb />
        <PageHeader description="Manage your development projects" />
        <ErrorState
          title="Failed to load projects"
          message={projectsError instanceof Error ? projectsError.message : 'Unknown error'}
        />
      </div>
    );
  }

  // Empty state when no projects exist
  if (!isLoading && projects.length === 0) {
    return (
      <div className="space-y-4">
        <Breadcrumb />
        <PageHeader description="Manage your development projects" meta="0 projects" />
        <ProjectsEmptyState />
      </div>
    );
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb items={breadcrumbItems} />

      <PageHeader
        description="Manage your development projects"
        meta={`${projects.length} projects | ${totalStats.tasks} tasks | ${totalStats.active} active`}
        action={
          <button
            type="button"
            className="shrink-0 px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg font-medium transition-colors flex items-center gap-1.5 text-sm"
          >
            <span>+</span>
            <span>New Project</span>
          </button>
        }
      />

      <div className="flex gap-6">
        {/* Sidebar - Project List */}
        <div className="w-80 shrink-0">
          <div className="sticky top-4 space-y-2">
            {/* Header with sort options */}
            <div className="flex items-center justify-between px-1 mb-3">
              <h2 className="text-sm font-semibold text-sc-fg-muted">All Projects</h2>
              {/* Sort + filter options */}
              <div className="flex items-center gap-2">
                {/* Archive toggle */}
                <Tooltip content={showArchived ? 'Hide archived' : 'Show archived'} side="bottom">
                  <button
                    type="button"
                    onClick={() => setShowArchived(!showArchived)}
                    className={`p-1.5 rounded transition-colors ${
                      showArchived
                        ? 'bg-sc-yellow/20 text-sc-yellow'
                        : 'text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50'
                    }`}
                  >
                    <Archive width={14} height={14} />
                  </button>
                </Tooltip>
                <span className="w-px h-4 bg-sc-fg-subtle/20" />
                {/* Sort options */}
                {PROJECT_SORT_OPTIONS.map(option => (
                  <Tooltip key={option.value} content={option.label} side="bottom">
                    <button
                      type="button"
                      onClick={() => setSortBy(option.value)}
                      className={`p-1.5 rounded transition-colors ${
                        sortBy === option.value
                          ? 'bg-sc-purple/20 text-sc-purple'
                          : 'text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50'
                      }`}
                    >
                      {option.icon}
                    </button>
                  </Tooltip>
                ))}
              </div>
            </div>

            {isLoading ? (
              <div className="space-y-2">
                <ProjectCardSkeleton />
                <ProjectCardSkeleton />
                <ProjectCardSkeleton />
              </div>
            ) : (
              <div className="space-y-2">
                <AnimatePresence mode="popLayout">
                  {sortedProjects.map((project, index) => {
                    const stats = projectStatsMap.get(project.id);
                    return (
                      <motion.div
                        key={project.id}
                        layout
                        initial={{ opacity: 0, x: -20 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: -20 }}
                        transition={{
                          layout: { type: 'spring', stiffness: 350, damping: 30 },
                          opacity: { duration: 0.2, delay: index * 0.03 },
                          x: { duration: 0.2, delay: index * 0.03 },
                        }}
                      >
                        <ProjectCard
                          project={project}
                          stats={stats}
                          isSelected={project.id === effectiveSelectedId}
                          onClick={() => handleSelectProject(project.id)}
                        />
                      </motion.div>
                    );
                  })}
                </AnimatePresence>
              </div>
            )}
          </div>
        </div>

        {/* Main Content - Project Detail */}
        <div className="flex-1 min-w-0">
          {isLoading ? (
            <ProjectDetailSkeleton />
          ) : !selectedProject ? (
            <div className="flex items-center justify-center h-64 text-sc-fg-subtle">
              <div className="text-center">
                <div className="text-4xl mb-4">◇</div>
                <p>Select a project from the sidebar</p>
              </div>
            </div>
          ) : (
            <ProjectDetail
              project={selectedProject}
              stats={selectedStats}
              tasks={selectedProjectTasks}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// ProjectCard Component
// =============================================================================

interface ProjectCardProps {
  project: TaskSummary;
  stats?: ProjectStats;
  isSelected?: boolean;
  onClick?: () => void;
}

function ProjectCard({ project, stats, isSelected, onClick }: ProjectCardProps) {
  const progress = stats && stats.total > 0 ? Math.round((stats.done / stats.total) * 100) : 0;
  const hasActive = stats && stats.doing > 0;
  const isArchived = project.metadata.status === 'archived';

  return (
    <button
      type="button"
      onClick={onClick}
      className={`
        w-full text-left p-4 rounded-xl transition-all duration-150
        border group relative overflow-hidden
        ${
          isArchived
            ? 'bg-sc-bg-base/50 border-sc-fg-subtle/10 opacity-75'
            : isSelected
              ? 'bg-gradient-to-br from-sc-purple/15 via-sc-bg-base to-sc-bg-base border-sc-purple/50 shadow-lg shadow-sc-purple/10'
              : 'bg-sc-bg-base border-sc-fg-subtle/20 hover:border-sc-fg-subtle/40 hover:bg-sc-bg-highlight/30'
        }
      `}
    >
      {/* Active indicator bar */}
      {hasActive && !isSelected && !isArchived && (
        <div className="absolute left-0 top-0 bottom-0 w-1 bg-sc-purple" />
      )}
      {/* Archived indicator bar */}
      {isArchived && <div className="absolute left-0 top-0 bottom-0 w-1 bg-sc-yellow/50" />}

      {/* Header with name and badges */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <h3
          className={`font-semibold truncate ${
            isArchived
              ? 'text-sc-fg-muted'
              : isSelected
                ? 'text-sc-purple'
                : 'text-sc-fg-primary group-hover:text-white'
          }`}
        >
          {project.name}
        </h3>

        {/* Status badges */}
        <div className="flex items-center gap-1 shrink-0">
          {isArchived && (
            <span className="flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded bg-sc-yellow/20 text-sc-yellow">
              <Archive width={10} height={10} />
            </span>
          )}
          {!isArchived && (stats?.blocked ?? 0) > 0 && (
            <span className="flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded bg-sc-yellow/20 text-sc-yellow">
              <Pause width={10} height={10} />
              {stats?.blocked}
            </span>
          )}
          {!isArchived && (stats?.critical ?? 0) > 0 && (
            <span className="flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded bg-sc-red/20 text-sc-red">
              <Zap width={10} height={10} />
              {stats?.critical}
            </span>
          )}
        </div>
      </div>

      {/* Description */}
      {project.description && (
        <p className="text-xs text-sc-fg-muted line-clamp-2 mb-3">{project.description}</p>
      )}

      {/* Progress section */}
      {stats && stats.total > 0 && (
        <div className="space-y-2">
          {/* Progress bar */}
          <div className="h-1.5 bg-sc-bg-elevated rounded-full overflow-hidden">
            <div
              className={`h-full transition-all duration-500 ${
                progress === 100 ? 'bg-sc-green' : 'bg-sc-purple'
              }`}
              style={{ width: `${progress}%` }}
            />
          </div>

          {/* Stats row */}
          <div className="flex items-center justify-between text-[10px]">
            <div className="flex items-center gap-2 text-sc-fg-subtle">
              <span className="flex items-center gap-1">
                <CheckCircle2 width={10} height={10} className="text-sc-green" />
                {stats.done}/{stats.total}
              </span>
              {stats.doing > 0 && (
                <span className="flex items-center gap-1 text-sc-purple">
                  <Clock width={10} height={10} />
                  {stats.doing} active
                </span>
              )}
            </div>
            <span
              className={`font-medium ${progress === 100 ? 'text-sc-green' : 'text-sc-fg-muted'}`}
            >
              {progress}%
            </span>
          </div>
        </div>
      )}

      {/* Empty project indicator */}
      {(!stats || stats.total === 0) && (
        <div className="text-xs text-sc-fg-subtle italic">No tasks yet</div>
      )}
    </button>
  );
}

function ProjectCardSkeleton() {
  return (
    <div className="p-4 rounded-xl bg-sc-bg-base border border-sc-fg-subtle/20 animate-pulse">
      <div className="h-5 w-3/4 bg-sc-bg-elevated rounded mb-2" />
      <div className="h-3 w-full bg-sc-bg-elevated rounded mb-3" />
      <div className="h-1.5 w-full bg-sc-bg-elevated rounded mb-2" />
      <div className="flex justify-between">
        <div className="h-3 w-16 bg-sc-bg-elevated rounded" />
        <div className="h-3 w-8 bg-sc-bg-elevated rounded" />
      </div>
    </div>
  );
}

// =============================================================================
// ProjectDetail Component
// =============================================================================

interface ProjectDetailProps {
  project: TaskSummary;
  stats?: ProjectStats | null;
  tasks: TaskSummary[];
  onDeleted?: () => void;
}

function ProjectDetail({ project, stats, tasks, onDeleted }: ProjectDetailProps) {
  const router = useRouter();
  const updateEntity = useUpdateEntity();
  const deleteEntity = useDeleteEntity();
  const { data: projectMetrics } = useProjectMetrics(project.id);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const progress = stats && stats.total > 0 ? Math.round((stats.done / stats.total) * 100) : 0;

  // Get tech stack from metadata
  const techStack =
    (project.metadata.technologies as string[]) ?? (project.metadata.tech_stack as string[]) ?? [];
  const repoUrl = project.metadata.repository_url as string | undefined;
  const features = (project.metadata.features as string[]) ?? [];

  // Update handlers
  const handleNameSave = useCallback(
    async (value: string) => {
      try {
        await updateEntity.mutateAsync({ id: project.id, updates: { name: value } });
        toast.success('Project name updated');
      } catch {
        toast.error('Failed to update project name');
        throw new Error('Update failed');
      }
    },
    [project.id, updateEntity]
  );

  const handleDescriptionSave = useCallback(
    async (value: string) => {
      try {
        await updateEntity.mutateAsync({ id: project.id, updates: { description: value } });
        toast.success('Description updated');
      } catch {
        toast.error('Failed to update description');
        throw new Error('Update failed');
      }
    },
    [project.id, updateEntity]
  );

  const handleTechStackSave = useCallback(
    async (values: string[]) => {
      try {
        await updateEntity.mutateAsync({
          id: project.id,
          updates: { metadata: { ...project.metadata, technologies: values } },
        });
        toast.success('Tech stack updated');
      } catch {
        toast.error('Failed to update tech stack');
        throw new Error('Update failed');
      }
    },
    [project.id, project.metadata, updateEntity]
  );

  const handleFeaturesSave = useCallback(
    async (values: string[]) => {
      try {
        await updateEntity.mutateAsync({
          id: project.id,
          updates: { metadata: { ...project.metadata, features: values } },
        });
        toast.success('Features updated');
      } catch {
        toast.error('Failed to update features');
        throw new Error('Update failed');
      }
    },
    [project.id, project.metadata, updateEntity]
  );

  const handleRepoSave = useCallback(
    async (value: string) => {
      try {
        await updateEntity.mutateAsync({
          id: project.id,
          updates: { metadata: { ...project.metadata, repository_url: value || undefined } },
        });
        toast.success('Repository URL updated');
      } catch {
        toast.error('Failed to update repository URL');
        throw new Error('Update failed');
      }
    },
    [project.id, project.metadata, updateEntity]
  );

  const handleDelete = useCallback(async () => {
    try {
      await deleteEntity.mutateAsync(project.id);
      toast.success('Project deleted');
      setShowDeleteConfirm(false);
      onDeleted?.();
      router.push('/projects');
    } catch {
      toast.error('Failed to delete project');
    }
  }, [project.id, deleteEntity, onDeleted, router]);

  const isArchived = project.metadata.status === 'archived';

  const handleArchiveToggle = useCallback(async () => {
    const newStatus = isArchived ? 'active' : 'archived';
    try {
      await updateEntity.mutateAsync({
        id: project.id,
        updates: { metadata: { ...project.metadata, status: newStatus } },
      });
      toast.success(isArchived ? 'Project restored' : 'Project archived');
    } catch {
      toast.error('Failed to update project status');
    }
  }, [project.id, project.metadata, updateEntity, isArchived]);

  // Sort tasks: blocked first, then doing, then by priority
  const priorityOrder: Record<string, number> = {
    critical: 0,
    high: 1,
    medium: 2,
    low: 3,
    someday: 4,
  };

  const activeTasks = tasks
    .filter(t => ['doing', 'blocked', 'review'].includes(t.metadata.status as string))
    .sort((a, b) => {
      // Blocked first
      if (a.metadata.status === 'blocked' && b.metadata.status !== 'blocked') return -1;
      if (b.metadata.status === 'blocked' && a.metadata.status !== 'blocked') return 1;
      // Then by priority
      const aPri = priorityOrder[a.metadata.priority as string] ?? 2;
      const bPri = priorityOrder[b.metadata.priority as string] ?? 2;
      return aPri - bPri;
    });

  const upcomingTasks = tasks
    .filter(t => ['todo', 'backlog'].includes(t.metadata.status as string))
    .sort((a, b) => {
      const aPri = priorityOrder[a.metadata.priority as string] ?? 2;
      const bPri = priorityOrder[b.metadata.priority as string] ?? 2;
      return aPri - bPri;
    })
    .slice(0, 5);

  return (
    <div className="space-y-6">
      {/* Header with Inline Editing */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="group/name">
            <EditableText
              value={project.name}
              onSave={handleNameSave}
              placeholder="Project name"
              required
              className="text-2xl font-bold text-sc-fg-primary"
            />
          </div>
          <div className="mt-2 group/desc">
            <EditableText
              value={project.description ?? ''}
              onSave={handleDescriptionSave}
              placeholder="Add a description..."
              multiline
              rows={2}
              className="text-sc-fg-muted"
            />
          </div>
        </div>

        {/* Quick Actions */}
        <div className="flex items-center gap-2 shrink-0">
          {repoUrl && (
            <a
              href={repoUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="p-2 bg-sc-bg-elevated hover:bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-muted hover:text-sc-cyan transition-colors"
              title="View Repository"
            >
              <GitBranch width={18} height={18} />
            </a>
          )}
          <Link
            href={`/tasks?project=${project.id}`}
            className="px-3 py-2 bg-sc-bg-elevated hover:bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-muted hover:text-sc-cyan transition-colors flex items-center gap-1.5 text-sm"
          >
            <FolderKanban width={14} height={14} />
            <span>Tasks</span>
          </Link>
          <Tooltip content={isArchived ? 'Restore project' : 'Archive project'} side="bottom">
            <button
              type="button"
              onClick={handleArchiveToggle}
              className={`p-2 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg transition-colors ${
                isArchived
                  ? 'hover:bg-sc-green/20 hover:border-sc-green/30 text-sc-yellow hover:text-sc-green'
                  : 'hover:bg-sc-yellow/20 hover:border-sc-yellow/30 text-sc-fg-muted hover:text-sc-yellow'
              }`}
            >
              <Archive width={18} height={18} />
            </button>
          </Tooltip>
          <button
            type="button"
            onClick={() => setShowDeleteConfirm(true)}
            className="p-2 bg-sc-bg-elevated hover:bg-sc-red/20 border border-sc-fg-subtle/20 hover:border-sc-red/30 rounded-lg text-sc-fg-muted hover:text-sc-red transition-colors"
            title="Delete Project"
          >
            <Trash width={18} height={18} />
          </button>
        </div>
      </div>

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <button
            type="button"
            className="absolute inset-0 bg-sc-bg-dark/80 backdrop-blur-sm cursor-default"
            onClick={() => setShowDeleteConfirm(false)}
            aria-label="Close"
          />
          <div className="relative bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl p-6 max-w-md w-full mx-4 shadow-2xl">
            <div className="flex items-start gap-4">
              <div className="p-3 bg-sc-red/20 rounded-full">
                <AlertTriangle width={24} height={24} className="text-sc-red" />
              </div>
              <div className="flex-1">
                <h3 className="text-lg font-semibold text-sc-fg-primary mb-2">Delete Project?</h3>
                <p className="text-sm text-sc-fg-muted mb-1">
                  Are you sure you want to delete <strong>{project.name}</strong>?
                </p>
                {stats && stats.total > 0 && (
                  <p className="text-sm text-sc-yellow">
                    This project has {stats.total} task{stats.total !== 1 ? 's' : ''} that will be
                    orphaned.
                  </p>
                )}
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button
                type="button"
                onClick={() => setShowDeleteConfirm(false)}
                className="px-4 py-2 text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleDelete}
                disabled={deleteEntity.isPending}
                className="px-4 py-2 bg-sc-red hover:bg-sc-red/80 disabled:opacity-50 text-white rounded-lg font-medium transition-colors"
              >
                {deleteEntity.isPending ? 'Deleting...' : 'Delete Project'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Stats Overview */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {/* Progress Card */}
        <div className="col-span-2 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-semibold text-sc-fg-primary">Progress</h2>
            <span
              className={`text-2xl font-bold ${progress === 100 ? 'text-sc-green' : 'text-sc-purple'}`}
            >
              {progress}%
            </span>
          </div>
          <div className="h-3 bg-sc-bg-elevated rounded-full overflow-hidden mb-2">
            <div
              className={`h-full transition-all duration-500 ${
                progress === 100 ? 'bg-sc-green' : 'bg-sc-purple'
              }`}
              style={{ width: `${progress}%` }}
            />
          </div>
          <div className="flex justify-between text-sm text-sc-fg-muted">
            <span>{stats?.done ?? 0} completed</span>
            <span>{stats?.total ?? 0} total</span>
          </div>
        </div>

        {/* Active Work */}
        <div className="bg-sc-bg-base border border-sc-purple/30 rounded-xl p-5">
          <div className="flex items-center gap-2 text-sc-purple mb-1">
            <Clock width={16} height={16} />
            <span className="text-sm font-medium">Active</span>
          </div>
          <div className="text-3xl font-bold text-sc-fg-primary">{stats?.doing ?? 0}</div>
          <div className="text-xs text-sc-fg-subtle mt-1">{stats?.review ?? 0} in review</div>
        </div>

        {/* Blocked/Urgent */}
        <div
          className={`bg-sc-bg-base border rounded-xl p-5 ${
            (stats?.blocked ?? 0) > 0 || (stats?.critical ?? 0) > 0
              ? 'border-sc-red/30'
              : 'border-sc-fg-subtle/20'
          }`}
        >
          <div
            className={`flex items-center gap-2 mb-1 ${
              (stats?.blocked ?? 0) > 0 ? 'text-sc-yellow' : 'text-sc-coral'
            }`}
          >
            {(stats?.blocked ?? 0) > 0 ? (
              <Pause width={16} height={16} />
            ) : (
              <AlertTriangle width={16} height={16} />
            )}
            <span className="text-sm font-medium">
              {(stats?.blocked ?? 0) > 0 ? 'Blocked' : 'Urgent'}
            </span>
          </div>
          <div className="text-3xl font-bold text-sc-fg-primary">
            {(stats?.blocked ?? 0) > 0
              ? stats?.blocked
              : (stats?.critical ?? 0) + (stats?.high ?? 0)}
          </div>
          <div className="text-xs text-sc-fg-subtle mt-1">
            {(stats?.blocked ?? 0) > 0 ? 'needs attention' : 'high priority'}
          </div>
        </div>
      </div>

      {/* Velocity Chart - Show if we have trend data */}
      {projectMetrics?.metrics?.velocity_trend &&
        projectMetrics.metrics.velocity_trend.length > 0 && (
          <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <TrendingUp width={16} height={16} className="text-sc-green" />
                <h2 className="font-semibold text-sc-fg-primary">Completion Velocity</h2>
              </div>
              <div className="flex items-center gap-4 text-sm">
                <span className="text-sc-fg-muted">
                  <span className="text-sc-fg-primary font-medium">
                    {projectMetrics.metrics.tasks_completed_last_7d}
                  </span>{' '}
                  completed this week
                </span>
              </div>
            </div>
            <VelocityLineChart data={projectMetrics.metrics.velocity_trend} />
          </div>
        )}

      {/* Active Work Section */}
      {activeTasks.length > 0 && (
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-sc-fg-primary">Active Work</h2>
            <span className="text-xs text-sc-fg-subtle">{activeTasks.length} tasks</span>
          </div>
          <div className="space-y-2">
            {activeTasks.slice(0, 5).map(task => (
              <TaskRow key={task.id} task={task} />
            ))}
            {activeTasks.length > 5 && (
              <Link
                href={`/tasks?project=${project.id}`}
                className="block text-center text-sm text-sc-purple hover:text-sc-purple/80 py-2"
              >
                View all {activeTasks.length} active tasks →
              </Link>
            )}
          </div>
        </div>
      )}

      {/* Up Next Section */}
      {upcomingTasks.length > 0 && (
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-sc-fg-primary">Up Next</h2>
            <span className="text-xs text-sc-fg-subtle">
              {stats?.todo ?? 0} todo, {stats?.backlog ?? 0} backlog
            </span>
          </div>
          <div className="space-y-2">
            {upcomingTasks.map(task => (
              <TaskRow key={task.id} task={task} />
            ))}
          </div>
        </div>
      )}

      {/* Tech & Features Grid - Always show for editing */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <h2 className="font-semibold text-sc-fg-primary mb-3">Tech Stack</h2>
          <EditableTags
            values={techStack}
            onSave={handleTechStackSave}
            placeholder="Add technology..."
            addPlaceholder="e.g., React, Python"
            tagClassName="bg-sc-cyan/10 text-sc-cyan border-sc-cyan/20"
          />
        </div>

        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <h2 className="font-semibold text-sc-fg-primary mb-3">Features</h2>
          <EditableTags
            values={features}
            onSave={handleFeaturesSave}
            placeholder="Add feature..."
            addPlaceholder="e.g., Auth, API"
            tagClassName="bg-sc-purple/10 text-sc-purple border-sc-purple/20"
          />
        </div>
      </div>

      {/* Repository URL */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
        <h2 className="font-semibold text-sc-fg-primary mb-3 flex items-center gap-2">
          <GitBranch width={16} height={16} className="text-sc-coral" />
          Repository
        </h2>
        <EditableText
          value={repoUrl ?? ''}
          onSave={handleRepoSave}
          placeholder="Add repository URL..."
          className="text-sm text-sc-cyan"
        />
      </div>

      {/* Empty state for no tasks */}
      {(!stats || stats.total === 0) && (
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-8 text-center">
          <div className="mb-4 flex justify-center">
            <Sparkles width={40} height={40} className="text-sc-yellow" />
          </div>
          <h3 className="text-lg font-semibold text-sc-fg-primary mb-2">No tasks yet</h3>
          <p className="text-sc-fg-muted mb-4">Create your first task to get started</p>
          <Link
            href={`/tasks?project=${project.id}`}
            className="inline-flex items-center gap-2 px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg font-medium transition-colors"
          >
            <Plus width={16} height={16} />
            <span>Add Task</span>
          </Link>
        </div>
      )}
    </div>
  );
}

// Task row component for lists
function TaskRow({ task }: { task: TaskSummary }) {
  const status = (task.metadata.status ?? 'todo') as TaskStatus;
  const priority = task.metadata.priority as string | undefined;
  const config = TASK_STATUS_CONFIG[status as keyof typeof TASK_STATUS_CONFIG];

  const priorityColors: Record<string, string> = {
    critical: 'text-sc-red',
    high: 'text-sc-coral',
    medium: 'text-sc-purple',
    low: 'text-sc-cyan',
    someday: 'text-sc-fg-subtle',
  };

  return (
    <Link
      href={`/tasks/${task.id}`}
      className="flex items-center gap-3 p-3 rounded-lg bg-sc-bg-highlight/30 hover:bg-sc-bg-highlight/50 transition-colors group"
    >
      <span className={config?.textClass}>{config?.icon}</span>
      <span className="flex-1 text-sm text-sc-fg-primary group-hover:text-white truncate">
        {task.name}
      </span>
      {priority && (
        <span className={`text-xs ${priorityColors[priority] ?? 'text-sc-fg-muted'}`}>
          {priority}
        </span>
      )}
      <span className={`text-xs px-2 py-0.5 rounded ${config?.bgClass} ${config?.textClass}`}>
        {config?.label}
      </span>
    </Link>
  );
}

function ProjectDetailSkeleton() {
  return (
    <div className="space-y-6 animate-pulse">
      <div>
        <div className="h-8 w-1/3 bg-sc-bg-elevated rounded" />
        <div className="h-4 w-2/3 bg-sc-bg-elevated rounded mt-3" />
      </div>
      <div className="grid grid-cols-4 gap-4">
        <div className="col-span-2 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <div className="h-4 w-20 bg-sc-bg-elevated rounded mb-3" />
          <div className="h-3 w-full bg-sc-bg-elevated rounded" />
        </div>
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <div className="h-4 w-16 bg-sc-bg-elevated rounded mb-2" />
          <div className="h-8 w-12 bg-sc-bg-elevated rounded" />
        </div>
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
          <div className="h-4 w-16 bg-sc-bg-elevated rounded mb-2" />
          <div className="h-8 w-12 bg-sc-bg-elevated rounded" />
        </div>
      </div>
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-5">
        <div className="h-4 w-24 bg-sc-bg-elevated rounded mb-4" />
        <div className="space-y-2">
          <div className="h-12 w-full bg-sc-bg-elevated rounded" />
          <div className="h-12 w-full bg-sc-bg-elevated rounded" />
        </div>
      </div>
    </div>
  );
}
