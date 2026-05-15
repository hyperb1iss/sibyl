'use client';

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import { WelcomeBanner } from '@/components/dashboard';
import { useCaptureMemory } from '@/components/layout/capture-memory-context';
import { VelocityLineChart } from '@/components/metrics/charts';
import {
  Activity,
  Archive,
  ArrowRight,
  BarChart3,
  BookOpen,
  Boxes,
  CheckCircle2,
  Clock,
  Database,
  EditPencil,
  FolderKanban,
  Layers,
  LayoutDashboard,
  ListTodo,
  Network,
  Play,
  Search,
  Target,
  TrendingUp,
  Zap,
} from '@/components/ui/icons';
import type { StatsResponse } from '@/lib/api';
import { ENTITY_COLORS, formatUptime } from '@/lib/constants';
import { useHealth, useOrgMetrics, useProjects, useSessionBundle, useStats } from '@/lib/hooks';
import { useProjectFilters } from '@/lib/project-context';

interface DashboardContentProps {
  initialStats: StatsResponse;
}

// Mini ring chart component for entity distribution
function EntityRingChart({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).filter(([_, count]) => count > 0);
  const total = entries.reduce((sum, [_, count]) => sum + count, 0);

  if (total === 0) {
    return (
      <div className="w-24 h-24 sm:w-32 sm:h-32 rounded-full border-4 border-sc-fg-subtle/20 flex items-center justify-center">
        <span className="text-sc-fg-subtle text-xs sm:text-sm">No data</span>
      </div>
    );
  }

  // Calculate segments for the ring
  let currentAngle = 0;
  const segments = entries.map(([type, count]) => {
    const percentage = count / total;
    const angle = percentage * 360;
    const segment = {
      type,
      count,
      percentage,
      startAngle: currentAngle,
      endAngle: currentAngle + angle,
      color: ENTITY_COLORS[type as keyof typeof ENTITY_COLORS] ?? '#8b85a0',
    };
    currentAngle += angle;
    return segment;
  });

  // Create SVG arc paths
  const createArc = (startAngle: number, endAngle: number, radius: number) => {
    const start = polarToCartesian(50, 50, radius, endAngle);
    const end = polarToCartesian(50, 50, radius, startAngle);
    const largeArcFlag = endAngle - startAngle <= 180 ? 0 : 1;
    return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${largeArcFlag} 0 ${end.x} ${end.y}`;
  };

  const polarToCartesian = (cx: number, cy: number, r: number, angle: number) => {
    const rad = ((angle - 90) * Math.PI) / 180;
    // Round to 2 decimal places to prevent SSR/client hydration mismatch
    return {
      x: Math.round((cx + r * Math.cos(rad)) * 100) / 100,
      y: Math.round((cy + r * Math.sin(rad)) * 100) / 100,
    };
  };

  return (
    <div className="relative w-24 h-24 sm:w-32 sm:h-32">
      <svg viewBox="0 0 100 100" className="w-full h-full -rotate-90" role="img">
        <title>Entity distribution chart</title>
        {segments.map((seg, _i) => (
          <path
            key={seg.type}
            d={createArc(seg.startAngle, seg.endAngle - 0.5, 40)}
            fill="none"
            stroke={seg.color}
            strokeWidth="12"
            strokeLinecap="round"
            className="transition-all duration-500"
            style={{ filter: `drop-shadow(0 0 6px ${seg.color}40)` }}
          />
        ))}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl sm:text-2xl font-bold text-sc-fg-primary">{total}</span>
        <span className="text-[8px] sm:text-[10px] text-sc-fg-subtle uppercase tracking-wide">
          Entities
        </span>
      </div>
    </div>
  );
}

function sessionStatusTone(status: string): string {
  switch (status) {
    case 'blocked':
      return 'text-sc-yellow';
    case 'doing':
      return 'text-sc-purple';
    default:
      return 'text-sc-fg-muted';
  }
}

export function DashboardContent({ initialStats }: DashboardContentProps) {
  const [mounted, setMounted] = useState(false);
  const { openCaptureMemory } = useCaptureMemory();
  const projectFilters = useProjectFilters();
  const { data: health, isLoading: healthLoading } = useHealth();
  const { data: stats } = useStats(initialStats);
  const { data: projectsData } = useProjects();
  const { data: orgMetrics } = useOrgMetrics();
  const { data: sessionBundle, isLoading: sessionBundleLoading } = useSessionBundle({
    project_ids: projectFilters,
    task_limit: 4,
    memory_limit: 2,
  });

  // Avoid hydration mismatch - only show real status after mount
  useEffect(() => {
    setMounted(true);
  }, []);

  // Calculate task stats in single pass
  const taskStats = useMemo(() => {
    const status = orgMetrics?.status_distribution;
    return {
      total: orgMetrics?.total_tasks ?? 0,
      doing: status?.doing ?? 0,
      todo: status?.todo ?? 0,
      review: status?.review ?? 0,
      done: status?.done ?? 0,
      blocked: status?.blocked ?? 0,
    };
  }, [orgMetrics]);

  const projectCount = projectsData?.entities?.length ?? 0;
  const sessionScopeLabel = projectFilters?.length
    ? projectFilters.length === 1
      ? 'Current project'
      : `${projectFilters.length} projects`
    : 'All projects';

  // Top entity types for quick stats
  const topEntities = useMemo(() => {
    if (!stats?.entity_counts) return [];
    return Object.entries(stats.entity_counts)
      .filter(([_, count]) => count > 0)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4);
  }, [stats]);

  return (
    <div className="space-y-4 sm:space-y-6 animate-fade-in">
      {/* Dashboard breadcrumb */}
      <nav
        aria-label="Breadcrumb"
        className="flex items-center gap-1.5 text-sm text-sc-fg-muted min-h-[24px]"
        style={{ viewTransitionName: 'breadcrumb' }}
      >
        <span className="flex items-center gap-1.5 text-sc-fg-primary font-medium">
          <LayoutDashboard width={14} height={14} />
          <span>Dashboard</span>
        </span>
      </nav>

      {/* Welcome Banner - Shows for new users with few entities */}
      <WelcomeBanner totalEntities={stats?.total_entities ?? 0} />

      {/* Hero Section - System Overview */}
      <div className="bg-gradient-to-br from-sc-bg-base via-sc-bg-elevated to-sc-purple/5 border border-sc-fg-subtle/20 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-xl shadow-black/10">
        <div className="flex flex-col lg:flex-row gap-4 sm:gap-8 items-start lg:items-center justify-between">
          {/* Left: Status & Welcome */}
          <div className="flex-1 space-y-3 sm:space-y-4 min-w-0">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 sm:w-12 sm:h-12 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center shadow-lg shadow-sc-purple/30 shrink-0">
                <Database width={20} height={20} className="text-white sm:w-6 sm:h-6" />
              </div>
              <div className="min-w-0">
                <h1 className="text-xl sm:text-2xl font-bold text-sc-fg-primary truncate">
                  Knowledge Oracle
                </h1>
                <div className="flex items-center gap-3 sm:gap-4 mt-1 flex-wrap">
                  {mounted && health?.graph_connected && (
                    <div className="flex items-center gap-1.5 text-xs sm:text-sm text-sc-fg-muted">
                      <div className="w-2 h-2 rounded-full bg-sc-green shadow-[0_0_8px_rgba(80,250,123,0.6)] animate-pulse" />
                      <Database width={12} height={12} className="text-sc-cyan shrink-0" />
                      <span>Graph Connected</span>
                    </div>
                  )}
                  {mounted && !healthLoading && !health?.graph_connected && (
                    <div className="flex items-center gap-1.5 text-xs sm:text-sm text-sc-fg-muted">
                      <div className="w-2 h-2 rounded-full bg-sc-red shadow-[0_0_8px_rgba(255,99,99,0.6)]" />
                      <Database width={12} height={12} className="text-sc-red shrink-0" />
                      <span>Graph Disconnected</span>
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Quick Stats Row */}
            <div className="flex flex-wrap gap-3 sm:gap-6">
              <div className="flex items-center gap-2">
                <Clock width={14} height={14} className="text-sc-cyan shrink-0 sm:w-4 sm:h-4" />
                <span className="text-xs sm:text-sm text-sc-fg-muted">
                  Uptime:{' '}
                  <span className="text-sc-fg-primary font-medium" suppressHydrationWarning>
                    {formatUptime(mounted ? (health?.uptime_seconds ?? 0) : 0)}
                  </span>
                </span>
              </div>
              <div className="flex items-center gap-2">
                <FolderKanban
                  width={14}
                  height={14}
                  className="text-sc-purple shrink-0 sm:w-4 sm:h-4"
                />
                <span className="text-xs sm:text-sm text-sc-fg-muted">
                  <span className="text-sc-fg-primary font-medium" suppressHydrationWarning>
                    {projectCount}
                  </span>{' '}
                  Projects
                </span>
              </div>
              <div className="flex items-center gap-2">
                <ListTodo width={14} height={14} className="text-sc-coral shrink-0 sm:w-4 sm:h-4" />
                <span className="text-xs sm:text-sm text-sc-fg-muted">
                  <span className="text-sc-fg-primary font-medium" suppressHydrationWarning>
                    {taskStats.total}
                  </span>{' '}
                  Tasks
                </span>
              </div>
            </div>
          </div>

          {/* Right: Entity Ring Chart */}
          <div className="flex items-center gap-4 sm:gap-6 w-full sm:w-auto justify-center sm:justify-end">
            <EntityRingChart counts={stats?.entity_counts ?? {}} />
            <div className="space-y-1.5 sm:space-y-2 hidden xs:block">
              {topEntities.map(([type, count]) => (
                <div key={type} className="flex items-center gap-2">
                  <div
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ backgroundColor: ENTITY_COLORS[type as keyof typeof ENTITY_COLORS] }}
                  />
                  <span className="text-[10px] sm:text-xs text-sc-fg-muted capitalize">
                    {type.replace(/_/g, ' ')}
                  </span>
                  <span className="text-[10px] sm:text-xs font-medium text-sc-fg-primary">
                    {count}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Main Layout - Two independent columns */}
      <div className="flex flex-col lg:flex-row gap-4 sm:gap-6">
        {/* Left Column - Main content */}
        <div className="flex-1 space-y-4 sm:space-y-6">
          {/* Task Overview */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-card">
            <div className="flex items-center justify-between mb-4 sm:mb-6 gap-2">
              <div className="flex items-center gap-2 sm:gap-3 min-w-0">
                <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-coral/10 border border-sc-coral/20 flex items-center justify-center shrink-0">
                  <ListTodo width={16} height={16} className="text-sc-coral sm:w-5 sm:h-5" />
                </div>
                <div className="min-w-0">
                  <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary truncate">
                    Task Overview
                  </h2>
                  <p className="text-xs sm:text-sm text-sc-fg-muted">
                    {taskStats.doing} in progress
                  </p>
                </div>
              </div>
              <Link
                href="/tasks"
                className="flex items-center gap-1 sm:gap-1.5 text-xs sm:text-sm text-sc-purple hover:text-sc-purple/80 transition-colors shrink-0"
              >
                <span className="hidden xs:inline">View all</span>
                <ArrowRight width={14} height={14} />
              </Link>
            </div>

            {/* Task Status Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 sm:gap-4">
              <Link
                href="/tasks"
                className="bg-sc-bg-elevated rounded-lg sm:rounded-xl p-3 sm:p-4 border border-sc-fg-subtle/10 hover:border-sc-cyan/30 transition-all group"
              >
                <div className="flex items-center gap-1.5 sm:gap-2 mb-1.5 sm:mb-2">
                  <Target width={14} height={14} className="text-sc-cyan sm:w-4 sm:h-4" />
                  <span className="text-xs sm:text-sm text-sc-fg-muted">To Do</span>
                </div>
                <p className="text-xl sm:text-2xl font-bold text-sc-fg-primary group-hover:text-sc-cyan transition-colors">
                  {taskStats.todo}
                </p>
              </Link>

              <Link
                href="/tasks"
                className="bg-sc-bg-elevated rounded-lg sm:rounded-xl p-3 sm:p-4 border border-sc-fg-subtle/10 hover:border-sc-purple/30 transition-all group"
              >
                <div className="flex items-center gap-1.5 sm:gap-2 mb-1.5 sm:mb-2">
                  <Play width={14} height={14} className="text-sc-purple sm:w-4 sm:h-4" />
                  <span className="text-xs sm:text-sm text-sc-fg-muted">In Progress</span>
                </div>
                <p className="text-xl sm:text-2xl font-bold text-sc-fg-primary group-hover:text-sc-purple transition-colors">
                  {taskStats.doing}
                </p>
              </Link>

              <Link
                href="/tasks"
                className="bg-sc-bg-elevated rounded-lg sm:rounded-xl p-3 sm:p-4 border border-sc-fg-subtle/10 hover:border-sc-yellow/30 transition-all group"
              >
                <div className="flex items-center gap-1.5 sm:gap-2 mb-1.5 sm:mb-2">
                  <Clock width={14} height={14} className="text-sc-yellow sm:w-4 sm:h-4" />
                  <span className="text-xs sm:text-sm text-sc-fg-muted">In Review</span>
                </div>
                <p className="text-xl sm:text-2xl font-bold text-sc-fg-primary group-hover:text-sc-yellow transition-colors">
                  {taskStats.review}
                </p>
              </Link>

              <Link
                href="/tasks"
                className="bg-sc-bg-elevated rounded-lg sm:rounded-xl p-3 sm:p-4 border border-sc-fg-subtle/10 hover:border-sc-green/30 transition-all group"
              >
                <div className="flex items-center gap-1.5 sm:gap-2 mb-1.5 sm:mb-2">
                  <CheckCircle2 width={14} height={14} className="text-sc-green sm:w-4 sm:h-4" />
                  <span className="text-xs sm:text-sm text-sc-fg-muted">Completed</span>
                </div>
                <p className="text-xl sm:text-2xl font-bold text-sc-fg-primary group-hover:text-sc-green transition-colors">
                  {taskStats.done}
                </p>
              </Link>
            </div>

            {/* Task Progress Bar */}
            {taskStats.total > 0 && (
              <div className="mt-4 sm:mt-6">
                <div className="flex items-center justify-between text-[10px] sm:text-xs text-sc-fg-muted mb-1.5 sm:mb-2">
                  <span>Progress</span>
                  <span>{Math.round((taskStats.done / taskStats.total) * 100)}% complete</span>
                </div>
                <div className="h-1.5 sm:h-2 bg-sc-bg-dark rounded-full overflow-hidden">
                  <div
                    className="h-full bg-sc-green rounded-full transition-all duration-500"
                    style={{ width: `${(taskStats.done / taskStats.total) * 100}%` }}
                  />
                </div>
              </div>
            )}
          </div>

          {/* Velocity Chart */}
          {orgMetrics && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-card">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2 sm:gap-3">
                  <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-green/10 border border-sc-green/20 flex items-center justify-center shrink-0">
                    <TrendingUp width={16} height={16} className="text-sc-green sm:w-5 sm:h-5" />
                  </div>
                  <div>
                    <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary">
                      Completion Velocity
                    </h2>
                    <p className="text-xs text-sc-fg-muted">
                      Tasks completed per day (14-day trend)
                    </p>
                  </div>
                </div>
              </div>
              <VelocityLineChart data={orgMetrics.velocity_trend} />
            </div>
          )}

          {/* Knowledge Distribution */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-card">
            <div className="flex items-center gap-2 sm:gap-3 mb-4 sm:mb-6">
              <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-cyan/10 border border-sc-cyan/20 flex items-center justify-center shrink-0">
                <Layers width={16} height={16} className="text-sc-cyan sm:w-5 sm:h-5" />
              </div>
              <div className="min-w-0">
                <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary truncate">
                  Knowledge Distribution
                </h2>
                <p className="text-xs sm:text-sm text-sc-fg-muted">
                  {stats?.total_entities ?? 0} total entities
                </p>
              </div>
            </div>

            <div className="space-y-2.5 sm:space-y-3">
              {Object.entries(stats?.entity_counts ?? {})
                .filter(([_, count]) => count > 0)
                .sort((a, b) => b[1] - a[1])
                .map(([type, count]) => {
                  const total = stats?.total_entities ?? 1;
                  const percentage = (count / total) * 100;
                  const color = ENTITY_COLORS[type as keyof typeof ENTITY_COLORS] ?? '#8b85a0';

                  return (
                    <div key={type} className="group">
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-2">
                          <div
                            className="w-2 h-2 sm:w-2.5 sm:h-2.5 rounded-full shrink-0"
                            style={{ backgroundColor: color }}
                          />
                          <span className="text-xs sm:text-sm font-medium text-sc-fg-primary capitalize">
                            {type.replace(/_/g, ' ')}
                          </span>
                        </div>
                        <span className="text-xs sm:text-sm text-sc-fg-muted">
                          {count}{' '}
                          <span className="text-sc-fg-subtle hidden xs:inline">
                            ({percentage.toFixed(1)}%)
                          </span>
                        </span>
                      </div>
                      <div className="h-1.5 sm:h-2 bg-sc-bg-dark rounded-full overflow-hidden">
                        <div
                          className="h-full rounded-full transition-all duration-500 group-hover:opacity-80"
                          style={{
                            width: `${percentage}%`,
                            backgroundColor: color,
                            boxShadow: `0 0 8px ${color}40`,
                          }}
                        />
                      </div>
                    </div>
                  );
                })}
            </div>
          </div>
        </div>

        {/* Right Column - Sidebar */}
        <div className="lg:w-80 shrink-0 space-y-4 sm:space-y-6">
          <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-card">
            <div className="flex items-center gap-2 sm:gap-3 mb-4">
              <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-cyan/10 border border-sc-cyan/20 flex items-center justify-center">
                <Activity width={16} height={16} className="text-sc-cyan sm:w-5 sm:h-5" />
              </div>
              <div className="min-w-0">
                <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary">
                  Session Snapshot
                </h2>
                <p className="text-xs sm:text-sm text-sc-fg-muted">{sessionScopeLabel}</p>
              </div>
            </div>

            {sessionBundleLoading && !sessionBundle ? (
              <p className="text-sm text-sc-fg-muted">Packaging wake-up context...</p>
            ) : (
              <div className="space-y-4">
                <p className="text-sm leading-6 text-sc-fg-primary">
                  {sessionBundle?.remember_next ??
                    'Start a task or capture a useful learning to seed the next wake-up bundle.'}
                </p>

                {sessionBundle?.query && (
                  <div className="rounded-lg border border-sc-purple/20 bg-sc-purple/10 px-3 py-2">
                    <div className="text-[10px] uppercase tracking-[0.18em] text-sc-fg-subtle">
                      Focus
                    </div>
                    <div className="mt-1 text-sm text-sc-purple">{sessionBundle.query}</div>
                  </div>
                )}

                <div className="space-y-2">
                  <div className="text-[10px] uppercase tracking-[0.18em] text-sc-fg-subtle">
                    Active Now
                  </div>
                  {sessionBundle?.tasks.length ? (
                    sessionBundle.tasks.slice(0, 3).map(task => (
                      <Link
                        key={task.id}
                        href={`/tasks/${task.id}`}
                        className="flex items-start justify-between gap-3 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-elevated px-3 py-2 transition-colors hover:border-sc-purple/30"
                      >
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium text-sc-fg-primary">
                            {task.name}
                          </div>
                          <div className={`text-xs capitalize ${sessionStatusTone(task.status)}`}>
                            {task.status || 'todo'}
                          </div>
                        </div>
                        {task.priority && (
                          <div className="shrink-0 text-[10px] uppercase tracking-wide text-sc-fg-subtle">
                            {task.priority}
                          </div>
                        )}
                      </Link>
                    ))
                  ) : (
                    <div className="rounded-lg border border-dashed border-sc-fg-subtle/20 px-3 py-2 text-sm text-sc-fg-muted">
                      No doing or blocked tasks right now.
                    </div>
                  )}
                </div>

                {sessionBundle?.relevant_entities[0] && (
                  <div className="space-y-2">
                    <div className="text-[10px] uppercase tracking-[0.18em] text-sc-fg-subtle">
                      Relevant Memory
                    </div>
                    <Link
                      href="/search"
                      className="block rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-elevated px-3 py-3 transition-colors hover:border-sc-cyan/30"
                    >
                      <div className="text-sm font-medium text-sc-fg-primary">
                        {sessionBundle.relevant_entities[0].name}
                      </div>
                      <div className="mt-1 text-xs text-sc-fg-subtle line-clamp-3">
                        {sessionBundle.relevant_entities[0].preview}
                      </div>
                    </Link>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Quick Actions */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-card">
            <div className="flex items-center gap-2 sm:gap-3 mb-4 sm:mb-6">
              <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-purple/10 border border-sc-purple/20 flex items-center justify-center">
                <Zap width={16} height={16} className="text-sc-purple sm:w-5 sm:h-5" />
              </div>
              <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary">
                Quick Actions
              </h2>
            </div>

            <div className="space-y-2 sm:space-y-3">
              <button
                type="button"
                onClick={() => openCaptureMemory('dashboard')}
                className="group flex w-full items-center gap-2 rounded-lg border border-sc-fg-subtle/10 bg-gradient-to-r from-sc-purple/10 via-sc-purple/5 to-sc-cyan/10 p-2.5 text-left transition-all hover:border-sc-purple/30 hover:bg-sc-bg-highlight sm:gap-3 sm:rounded-xl sm:p-3"
              >
                <div className="h-8 w-8 shrink-0 rounded-lg bg-sc-purple/15 flex items-center justify-center sm:h-9 sm:w-9">
                  <EditPencil
                    width={16}
                    height={16}
                    className="text-sc-purple sm:h-[18px] sm:w-[18px]"
                  />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium text-sc-fg-primary transition-colors group-hover:text-sc-purple sm:text-sm">
                    Capture Memory
                  </div>
                  <div className="truncate text-[10px] text-sc-fg-subtle sm:text-xs">
                    Save a fresh learning right now
                  </div>
                </div>
                <ArrowRight
                  width={14}
                  height={14}
                  className="shrink-0 text-sc-fg-subtle transition-colors group-hover:text-sc-purple sm:h-4 sm:w-4"
                />
              </button>

              <Link
                href="/memory/captures?link=unlinked"
                className="flex items-center gap-2 sm:gap-3 p-2.5 sm:p-3 bg-sc-bg-elevated rounded-lg sm:rounded-xl border border-sc-fg-subtle/10 hover:border-sc-yellow/30 hover:bg-sc-bg-highlight transition-all group"
              >
                <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-lg bg-sc-yellow/10 flex items-center justify-center shrink-0">
                  <Archive
                    width={16}
                    height={16}
                    className="text-sc-yellow sm:w-[18px] sm:h-[18px]"
                  />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-xs sm:text-sm font-medium text-sc-fg-primary group-hover:text-sc-yellow transition-colors truncate">
                    Review Memory
                  </div>
                  <div className="text-[10px] sm:text-xs text-sc-fg-subtle truncate">
                    Triage captures waiting on graph linkage
                  </div>
                </div>
                <ArrowRight
                  width={14}
                  height={14}
                  className="text-sc-fg-subtle group-hover:text-sc-yellow transition-colors shrink-0 sm:w-4 sm:h-4"
                />
              </Link>

              <Link
                href="/search"
                className="flex items-center gap-2 sm:gap-3 p-2.5 sm:p-3 bg-sc-bg-elevated rounded-lg sm:rounded-xl border border-sc-fg-subtle/10 hover:border-sc-cyan/30 hover:bg-sc-bg-highlight transition-all group"
              >
                <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-lg bg-sc-cyan/10 flex items-center justify-center shrink-0">
                  <Search width={16} height={16} className="text-sc-cyan sm:w-[18px] sm:h-[18px]" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-xs sm:text-sm font-medium text-sc-fg-primary group-hover:text-sc-cyan transition-colors truncate">
                    Search Knowledge
                  </div>
                  <div className="text-[10px] sm:text-xs text-sc-fg-subtle truncate">
                    Find patterns & insights
                  </div>
                </div>
                <ArrowRight
                  width={14}
                  height={14}
                  className="text-sc-fg-subtle group-hover:text-sc-cyan transition-colors shrink-0 sm:w-4 sm:h-4"
                />
              </Link>

              <Link
                href="/graph"
                className="flex items-center gap-2 sm:gap-3 p-2.5 sm:p-3 bg-sc-bg-elevated rounded-lg sm:rounded-xl border border-sc-fg-subtle/10 hover:border-sc-purple/30 hover:bg-sc-bg-highlight transition-all group"
              >
                <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-lg bg-sc-purple/10 flex items-center justify-center shrink-0">
                  <Network
                    width={16}
                    height={16}
                    className="text-sc-purple sm:w-[18px] sm:h-[18px]"
                  />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-xs sm:text-sm font-medium text-sc-fg-primary group-hover:text-sc-purple transition-colors truncate">
                    Explore Graph
                  </div>
                  <div className="text-[10px] sm:text-xs text-sc-fg-subtle truncate">
                    Visualize connections
                  </div>
                </div>
                <ArrowRight
                  width={14}
                  height={14}
                  className="text-sc-fg-subtle group-hover:text-sc-purple transition-colors shrink-0 sm:w-4 sm:h-4"
                />
              </Link>

              <Link
                href="/entities"
                className="flex items-center gap-2 sm:gap-3 p-2.5 sm:p-3 bg-sc-bg-elevated rounded-lg sm:rounded-xl border border-sc-fg-subtle/10 hover:border-sc-coral/30 hover:bg-sc-bg-highlight transition-all group"
              >
                <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-lg bg-sc-coral/10 flex items-center justify-center shrink-0">
                  <Boxes width={16} height={16} className="text-sc-coral sm:w-[18px] sm:h-[18px]" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-xs sm:text-sm font-medium text-sc-fg-primary group-hover:text-sc-coral transition-colors truncate">
                    Browse Entities
                  </div>
                  <div className="text-[10px] sm:text-xs text-sc-fg-subtle truncate">
                    View all knowledge
                  </div>
                </div>
                <ArrowRight
                  width={14}
                  height={14}
                  className="text-sc-fg-subtle group-hover:text-sc-coral transition-colors shrink-0 sm:w-4 sm:h-4"
                />
              </Link>

              <Link
                href="/sources"
                className="flex items-center gap-2 sm:gap-3 p-2.5 sm:p-3 bg-sc-bg-elevated rounded-lg sm:rounded-xl border border-sc-fg-subtle/10 hover:border-sc-green/30 hover:bg-sc-bg-highlight transition-all group"
              >
                <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-lg bg-sc-green/10 flex items-center justify-center shrink-0">
                  <BookOpen
                    width={16}
                    height={16}
                    className="text-sc-green sm:w-[18px] sm:h-[18px]"
                  />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-xs sm:text-sm font-medium text-sc-fg-primary group-hover:text-sc-green transition-colors truncate">
                    Add Source
                  </div>
                  <div className="text-[10px] sm:text-xs text-sc-fg-subtle truncate">
                    Documentation &amp; knowledge
                  </div>
                </div>
                <ArrowRight
                  width={14}
                  height={14}
                  className="text-sc-fg-subtle group-hover:text-sc-green transition-colors shrink-0 sm:w-4 sm:h-4"
                />
              </Link>
            </div>
          </div>

          {/* This Week Stats */}
          {orgMetrics && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-card">
              <div className="flex items-center gap-2 sm:gap-3 mb-3">
                <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-purple/10 border border-sc-purple/20 flex items-center justify-center">
                  <BarChart3 width={16} height={16} className="text-sc-purple sm:w-5 sm:h-5" />
                </div>
                <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary">This Week</h2>
              </div>

              <div className="space-y-3">
                <div className="flex items-center justify-between p-3 bg-sc-bg-elevated rounded-lg">
                  <span className="text-sm text-sc-fg-muted">Completion Rate</span>
                  <span className="text-lg font-bold text-sc-green">
                    {orgMetrics.completion_rate}%
                  </span>
                </div>
                <div className="flex items-center justify-between p-3 bg-sc-bg-elevated rounded-lg">
                  <span className="text-sm text-sc-fg-muted">Tasks Created</span>
                  <span className="text-lg font-bold text-sc-fg-primary">
                    {orgMetrics.tasks_created_last_7d}
                  </span>
                </div>
                <div className="flex items-center justify-between p-3 bg-sc-bg-elevated rounded-lg">
                  <span className="text-sm text-sc-fg-muted">Tasks Completed</span>
                  <span className="text-lg font-bold text-sc-green">
                    {orgMetrics.tasks_completed_last_7d}
                  </span>
                </div>
                {orgMetrics.top_assignees.length > 0 && (
                  <div className="pt-2 border-t border-sc-fg-subtle/10">
                    <p className="text-xs text-sc-fg-subtle mb-2">Top Contributors</p>
                    <div className="space-y-1">
                      {orgMetrics.top_assignees.slice(0, 3).map(a => (
                        <div key={a.name} className="flex items-center justify-between text-sm">
                          <span className="text-sc-fg-muted truncate">{a.name}</span>
                          <span className="text-sc-green font-medium">{a.completed}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Error Display */}
      {mounted && health?.errors && health.errors.length > 0 && (
        <div className="bg-sc-red/10 border border-sc-red/30 rounded-xl sm:rounded-2xl p-4 sm:p-6">
          <div className="flex items-center gap-2 sm:gap-3 mb-3 sm:mb-4">
            <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-red/20 flex items-center justify-center">
              <Activity width={16} height={16} className="text-sc-red sm:w-5 sm:h-5" />
            </div>
            <h2 className="text-base sm:text-lg font-semibold text-sc-red">System Errors</h2>
          </div>
          <ul className="space-y-1.5 sm:space-y-2">
            {health.errors.map((error: string) => (
              <li
                key={error}
                className="flex items-start gap-2 text-xs sm:text-sm text-sc-fg-muted"
              >
                <span className="text-sc-red mt-0.5">•</span>
                {error}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
