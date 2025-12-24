'use client';

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  ArrowRight,
  Boxes,
  CheckCircle2,
  Clock,
  Database,
  FolderKanban,
  Layers,
  LayoutDashboard,
  ListTodo,
  Network,
  Play,
  RefreshCw,
  Search,
  Sparkles,
  Target,
  Zap,
} from '@/components/ui/icons';
import type { StatsResponse } from '@/lib/api';
import { ENTITY_COLORS, formatUptime } from '@/lib/constants';
import { useHealth, useProjects, useStats, useTasks } from '@/lib/hooks';

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

// Status indicator component
function StatusIndicator({ status }: { status: 'healthy' | 'unhealthy' | 'unknown' }) {
  const config = {
    healthy: {
      color: 'bg-sc-green',
      glow: 'shadow-[0_0_12px_rgba(80,250,123,0.6)]',
      text: 'Online',
    },
    unhealthy: { color: 'bg-sc-red', glow: 'shadow-[0_0_12px_rgba(255,99,99,0.6)]', text: 'Error' },
    unknown: { color: 'bg-sc-yellow', glow: '', text: 'Loading' },
  };
  const { color, glow, text } = config[status];

  return (
    <div className="flex items-center gap-2">
      <div className={`w-2.5 h-2.5 rounded-full ${color} ${glow} animate-pulse`} />
      <span className="text-sm font-medium text-sc-fg-primary">{text}</span>
    </div>
  );
}

export function DashboardContent({ initialStats }: DashboardContentProps) {
  const [mounted, setMounted] = useState(false);
  const { data: health, isLoading: healthLoading } = useHealth();
  const { data: stats } = useStats(initialStats);
  const { data: tasksData } = useTasks();
  const { data: projectsData } = useProjects();

  // Avoid hydration mismatch - only show real status after mount
  useEffect(() => {
    setMounted(true);
  }, []);

  // Calculate task stats
  const taskStats = useMemo(() => {
    const tasks = tasksData?.entities ?? [];
    return {
      total: tasks.length,
      doing: tasks.filter(t => t.metadata.status === 'doing').length,
      todo: tasks.filter(t => t.metadata.status === 'todo').length,
      review: tasks.filter(t => t.metadata.status === 'review').length,
      done: tasks.filter(t => t.metadata.status === 'done').length,
      blocked: tasks.filter(t => t.metadata.status === 'blocked').length,
    };
  }, [tasksData]);

  const projectCount = projectsData?.entities?.length ?? 0;
  const serverStatus =
    !mounted || healthLoading ? 'unknown' : health?.status === 'healthy' ? 'healthy' : 'unhealthy';

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

      {/* Hero Section - System Overview */}
      <div className="bg-gradient-to-br from-sc-bg-base via-sc-bg-elevated to-sc-purple/5 border border-sc-fg-subtle/20 rounded-xl sm:rounded-2xl p-4 sm:p-6 shadow-xl shadow-black/10">
        <div className="flex flex-col lg:flex-row gap-4 sm:gap-8 items-start lg:items-center justify-between">
          {/* Left: Status & Welcome */}
          <div className="flex-1 space-y-3 sm:space-y-4 min-w-0">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 sm:w-12 sm:h-12 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center shadow-lg shadow-sc-purple/30 shrink-0">
                <Sparkles width={20} height={20} className="text-white sm:w-6 sm:h-6" />
              </div>
              <div className="min-w-0">
                <h1 className="text-xl sm:text-2xl font-bold text-sc-fg-primary truncate">
                  Knowledge Oracle
                </h1>
                <div className="flex items-center gap-3 sm:gap-4 mt-1 flex-wrap">
                  <StatusIndicator status={serverStatus} />
                  {mounted && health?.graph_connected && (
                    <div className="flex items-center gap-1.5 text-xs sm:text-sm text-sc-fg-muted">
                      <Database width={12} height={12} className="text-sc-cyan shrink-0" />
                      <span>Graph Connected</span>
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
                  <span className="text-sc-fg-primary font-medium">{projectCount}</span> Projects
                </span>
              </div>
              <div className="flex items-center gap-2">
                <ListTodo width={14} height={14} className="text-sc-coral shrink-0 sm:w-4 sm:h-4" />
                <span className="text-xs sm:text-sm text-sc-fg-muted">
                  <span className="text-sc-fg-primary font-medium">{taskStats.total}</span> Tasks
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

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 sm:gap-6">
        {/* Task Overview - Takes 2 cols */}
        <div className="lg:col-span-2 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl sm:rounded-2xl p-4 sm:p-6">
          <div className="flex items-center justify-between mb-4 sm:mb-6 gap-2">
            <div className="flex items-center gap-2 sm:gap-3 min-w-0">
              <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-coral/10 border border-sc-coral/20 flex items-center justify-center shrink-0">
                <ListTodo width={16} height={16} className="text-sc-coral sm:w-5 sm:h-5" />
              </div>
              <div className="min-w-0">
                <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary truncate">
                  Task Overview
                </h2>
                <p className="text-xs sm:text-sm text-sc-fg-muted">{taskStats.doing} in progress</p>
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
                <RefreshCw width={14} height={14} className="text-sc-yellow sm:w-4 sm:h-4" />
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

        {/* Quick Actions */}
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl sm:rounded-2xl p-4 sm:p-6">
          <div className="flex items-center gap-2 sm:gap-3 mb-4 sm:mb-6">
            <div className="w-8 h-8 sm:w-10 sm:h-10 rounded-lg sm:rounded-xl bg-sc-purple/10 border border-sc-purple/20 flex items-center justify-center">
              <Zap width={16} height={16} className="text-sc-purple sm:w-5 sm:h-5" />
            </div>
            <h2 className="text-base sm:text-lg font-semibold text-sc-fg-primary">Quick Actions</h2>
          </div>

          <div className="space-y-2 sm:space-y-3">
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
              href="/ingest"
              className="flex items-center gap-2 sm:gap-3 p-2.5 sm:p-3 bg-sc-bg-elevated rounded-lg sm:rounded-xl border border-sc-fg-subtle/10 hover:border-sc-green/30 hover:bg-sc-bg-highlight transition-all group"
            >
              <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-lg bg-sc-green/10 flex items-center justify-center shrink-0">
                <RefreshCw
                  width={16}
                  height={16}
                  className="text-sc-green sm:w-[18px] sm:h-[18px]"
                />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-xs sm:text-sm font-medium text-sc-fg-primary group-hover:text-sc-green transition-colors truncate">
                  Ingest Documents
                </div>
                <div className="text-[10px] sm:text-xs text-sc-fg-subtle truncate">
                  Sync knowledge sources
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
      </div>

      {/* Entity Breakdown - Full Width Bar Chart Style */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl sm:rounded-2xl p-4 sm:p-6">
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
