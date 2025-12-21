'use client';

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
} from 'lucide-react';
import Link from 'next/link';
import { useMemo } from 'react';
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
      <div className="w-32 h-32 rounded-full border-4 border-sc-fg-subtle/20 flex items-center justify-center">
        <span className="text-sc-fg-subtle text-sm">No data</span>
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
    return {
      x: cx + r * Math.cos(rad),
      y: cy + r * Math.sin(rad),
    };
  };

  return (
    <div className="relative w-32 h-32">
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
        <span className="text-2xl font-bold text-sc-fg-primary">{total}</span>
        <span className="text-[10px] text-sc-fg-subtle uppercase tracking-wide">Entities</span>
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
  const { data: health, isLoading: healthLoading } = useHealth();
  const { data: stats } = useStats(initialStats);
  const { data: tasksData } = useTasks({});
  const { data: projectsData } = useProjects();

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
  const serverStatus = healthLoading
    ? 'unknown'
    : health?.status === 'healthy'
      ? 'healthy'
      : 'unhealthy';

  // Top entity types for quick stats
  const topEntities = useMemo(() => {
    if (!stats?.entity_counts) return [];
    return Object.entries(stats.entity_counts)
      .filter(([_, count]) => count > 0)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4);
  }, [stats]);

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Dashboard breadcrumb */}
      <nav
        aria-label="Breadcrumb"
        className="flex items-center gap-1.5 text-sm text-sc-fg-muted min-h-[24px]"
        style={{ viewTransitionName: 'breadcrumb' }}
      >
        <span className="flex items-center gap-1.5 text-sc-fg-primary font-medium">
          <LayoutDashboard size={14} strokeWidth={2} />
          <span>Dashboard</span>
        </span>
      </nav>

      {/* Hero Section - System Overview */}
      <div className="bg-gradient-to-br from-sc-bg-base via-sc-bg-elevated to-sc-purple/5 border border-sc-fg-subtle/20 rounded-2xl p-6 shadow-xl shadow-black/10">
        <div className="flex flex-col lg:flex-row gap-8 items-start lg:items-center justify-between">
          {/* Left: Status & Welcome */}
          <div className="flex-1 space-y-4">
            <div className="flex items-center gap-3">
              <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center shadow-lg shadow-sc-purple/30">
                <Sparkles size={24} className="text-white" />
              </div>
              <div>
                <h1 className="text-2xl font-bold text-sc-fg-primary">Knowledge Oracle</h1>
                <div className="flex items-center gap-4 mt-1">
                  <StatusIndicator status={serverStatus} />
                  {health?.graph_connected && (
                    <div className="flex items-center gap-1.5 text-sm text-sc-fg-muted">
                      <Database size={12} className="text-sc-cyan" />
                      <span>Graph Connected</span>
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Quick Stats Row */}
            <div className="flex flex-wrap gap-6">
              <div className="flex items-center gap-2">
                <Clock size={16} className="text-sc-cyan" />
                <span className="text-sm text-sc-fg-muted">
                  Uptime:{' '}
                  <span className="text-sc-fg-primary font-medium">
                    {formatUptime(health?.uptime_seconds ?? 0)}
                  </span>
                </span>
              </div>
              <div className="flex items-center gap-2">
                <FolderKanban size={16} className="text-sc-purple" />
                <span className="text-sm text-sc-fg-muted">
                  <span className="text-sc-fg-primary font-medium">{projectCount}</span> Projects
                </span>
              </div>
              <div className="flex items-center gap-2">
                <ListTodo size={16} className="text-sc-coral" />
                <span className="text-sm text-sc-fg-muted">
                  <span className="text-sc-fg-primary font-medium">{taskStats.total}</span> Tasks
                </span>
              </div>
            </div>
          </div>

          {/* Right: Entity Ring Chart */}
          <div className="flex items-center gap-6">
            <EntityRingChart counts={stats?.entity_counts ?? {}} />
            <div className="space-y-2">
              {topEntities.map(([type, count]) => (
                <div key={type} className="flex items-center gap-2">
                  <div
                    className="w-2 h-2 rounded-full"
                    style={{ backgroundColor: ENTITY_COLORS[type as keyof typeof ENTITY_COLORS] }}
                  />
                  <span className="text-xs text-sc-fg-muted capitalize">
                    {type.replace(/_/g, ' ')}
                  </span>
                  <span className="text-xs font-medium text-sc-fg-primary">{count}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Task Overview - Takes 2 cols */}
        <div className="lg:col-span-2 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-sc-coral/10 border border-sc-coral/20 flex items-center justify-center">
                <ListTodo size={20} className="text-sc-coral" />
              </div>
              <div>
                <h2 className="text-lg font-semibold text-sc-fg-primary">Task Overview</h2>
                <p className="text-sm text-sc-fg-muted">{taskStats.doing} in progress</p>
              </div>
            </div>
            <Link
              href="/tasks"
              className="flex items-center gap-1.5 text-sm text-sc-purple hover:text-sc-purple/80 transition-colors"
            >
              View all <ArrowRight size={14} />
            </Link>
          </div>

          {/* Task Status Grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Link
              href="/tasks"
              className="bg-sc-bg-elevated rounded-xl p-4 border border-sc-fg-subtle/10 hover:border-sc-cyan/30 transition-all group"
            >
              <div className="flex items-center gap-2 mb-2">
                <Target size={16} className="text-sc-cyan" />
                <span className="text-sm text-sc-fg-muted">To Do</span>
              </div>
              <p className="text-2xl font-bold text-sc-fg-primary group-hover:text-sc-cyan transition-colors">
                {taskStats.todo}
              </p>
            </Link>

            <Link
              href="/tasks"
              className="bg-sc-bg-elevated rounded-xl p-4 border border-sc-fg-subtle/10 hover:border-sc-purple/30 transition-all group"
            >
              <div className="flex items-center gap-2 mb-2">
                <Play size={16} className="text-sc-purple" />
                <span className="text-sm text-sc-fg-muted">In Progress</span>
              </div>
              <p className="text-2xl font-bold text-sc-fg-primary group-hover:text-sc-purple transition-colors">
                {taskStats.doing}
              </p>
            </Link>

            <Link
              href="/tasks"
              className="bg-sc-bg-elevated rounded-xl p-4 border border-sc-fg-subtle/10 hover:border-sc-yellow/30 transition-all group"
            >
              <div className="flex items-center gap-2 mb-2">
                <RefreshCw size={16} className="text-sc-yellow" />
                <span className="text-sm text-sc-fg-muted">In Review</span>
              </div>
              <p className="text-2xl font-bold text-sc-fg-primary group-hover:text-sc-yellow transition-colors">
                {taskStats.review}
              </p>
            </Link>

            <Link
              href="/tasks"
              className="bg-sc-bg-elevated rounded-xl p-4 border border-sc-fg-subtle/10 hover:border-sc-green/30 transition-all group"
            >
              <div className="flex items-center gap-2 mb-2">
                <CheckCircle2 size={16} className="text-sc-green" />
                <span className="text-sm text-sc-fg-muted">Completed</span>
              </div>
              <p className="text-2xl font-bold text-sc-fg-primary group-hover:text-sc-green transition-colors">
                {taskStats.done}
              </p>
            </Link>
          </div>

          {/* Task Progress Bar */}
          {taskStats.total > 0 && (
            <div className="mt-6">
              <div className="flex items-center justify-between text-xs text-sc-fg-muted mb-2">
                <span>Progress</span>
                <span>{Math.round((taskStats.done / taskStats.total) * 100)}% complete</span>
              </div>
              <div className="h-2 bg-sc-bg-dark rounded-full overflow-hidden flex">
                <div
                  className="h-full bg-sc-green transition-all duration-500"
                  style={{ width: `${(taskStats.done / taskStats.total) * 100}%` }}
                />
                <div
                  className="h-full bg-sc-yellow transition-all duration-500"
                  style={{ width: `${(taskStats.review / taskStats.total) * 100}%` }}
                />
                <div
                  className="h-full bg-sc-purple transition-all duration-500"
                  style={{ width: `${(taskStats.doing / taskStats.total) * 100}%` }}
                />
                <div
                  className="h-full bg-sc-cyan transition-all duration-500"
                  style={{ width: `${(taskStats.todo / taskStats.total) * 100}%` }}
                />
              </div>
            </div>
          )}
        </div>

        {/* Quick Actions */}
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
          <div className="flex items-center gap-3 mb-6">
            <div className="w-10 h-10 rounded-xl bg-sc-purple/10 border border-sc-purple/20 flex items-center justify-center">
              <Zap size={20} className="text-sc-purple" />
            </div>
            <h2 className="text-lg font-semibold text-sc-fg-primary">Quick Actions</h2>
          </div>

          <div className="space-y-3">
            <Link
              href="/search"
              className="flex items-center gap-3 p-3 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10 hover:border-sc-cyan/30 hover:bg-sc-bg-highlight transition-all group"
            >
              <div className="w-9 h-9 rounded-lg bg-sc-cyan/10 flex items-center justify-center">
                <Search size={18} className="text-sc-cyan" />
              </div>
              <div className="flex-1">
                <div className="text-sm font-medium text-sc-fg-primary group-hover:text-sc-cyan transition-colors">
                  Search Knowledge
                </div>
                <div className="text-xs text-sc-fg-subtle">Find patterns & insights</div>
              </div>
              <ArrowRight
                size={16}
                className="text-sc-fg-subtle group-hover:text-sc-cyan transition-colors"
              />
            </Link>

            <Link
              href="/graph"
              className="flex items-center gap-3 p-3 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10 hover:border-sc-purple/30 hover:bg-sc-bg-highlight transition-all group"
            >
              <div className="w-9 h-9 rounded-lg bg-sc-purple/10 flex items-center justify-center">
                <Network size={18} className="text-sc-purple" />
              </div>
              <div className="flex-1">
                <div className="text-sm font-medium text-sc-fg-primary group-hover:text-sc-purple transition-colors">
                  Explore Graph
                </div>
                <div className="text-xs text-sc-fg-subtle">Visualize connections</div>
              </div>
              <ArrowRight
                size={16}
                className="text-sc-fg-subtle group-hover:text-sc-purple transition-colors"
              />
            </Link>

            <Link
              href="/entities"
              className="flex items-center gap-3 p-3 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10 hover:border-sc-coral/30 hover:bg-sc-bg-highlight transition-all group"
            >
              <div className="w-9 h-9 rounded-lg bg-sc-coral/10 flex items-center justify-center">
                <Boxes size={18} className="text-sc-coral" />
              </div>
              <div className="flex-1">
                <div className="text-sm font-medium text-sc-fg-primary group-hover:text-sc-coral transition-colors">
                  Browse Entities
                </div>
                <div className="text-xs text-sc-fg-subtle">View all knowledge</div>
              </div>
              <ArrowRight
                size={16}
                className="text-sc-fg-subtle group-hover:text-sc-coral transition-colors"
              />
            </Link>

            <Link
              href="/ingest"
              className="flex items-center gap-3 p-3 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10 hover:border-sc-green/30 hover:bg-sc-bg-highlight transition-all group"
            >
              <div className="w-9 h-9 rounded-lg bg-sc-green/10 flex items-center justify-center">
                <RefreshCw size={18} className="text-sc-green" />
              </div>
              <div className="flex-1">
                <div className="text-sm font-medium text-sc-fg-primary group-hover:text-sc-green transition-colors">
                  Ingest Documents
                </div>
                <div className="text-xs text-sc-fg-subtle">Sync knowledge sources</div>
              </div>
              <ArrowRight
                size={16}
                className="text-sc-fg-subtle group-hover:text-sc-green transition-colors"
              />
            </Link>
          </div>
        </div>
      </div>

      {/* Entity Breakdown - Full Width Bar Chart Style */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 rounded-xl bg-sc-cyan/10 border border-sc-cyan/20 flex items-center justify-center">
            <Layers size={20} className="text-sc-cyan" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-sc-fg-primary">Knowledge Distribution</h2>
            <p className="text-sm text-sc-fg-muted">{stats?.total_entities ?? 0} total entities</p>
          </div>
        </div>

        <div className="space-y-3">
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
                        className="w-2.5 h-2.5 rounded-full"
                        style={{ backgroundColor: color }}
                      />
                      <span className="text-sm font-medium text-sc-fg-primary capitalize">
                        {type.replace(/_/g, ' ')}
                      </span>
                    </div>
                    <span className="text-sm text-sc-fg-muted">
                      {count} <span className="text-sc-fg-subtle">({percentage.toFixed(1)}%)</span>
                    </span>
                  </div>
                  <div className="h-2 bg-sc-bg-dark rounded-full overflow-hidden">
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
      {health?.errors && health.errors.length > 0 && (
        <div className="bg-sc-red/10 border border-sc-red/30 rounded-2xl p-6">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-sc-red/20 flex items-center justify-center">
              <Activity size={20} className="text-sc-red" />
            </div>
            <h2 className="text-lg font-semibold text-sc-red">System Errors</h2>
          </div>
          <ul className="space-y-2">
            {health.errors.map((error: string) => (
              <li key={error} className="flex items-start gap-2 text-sm text-sc-fg-muted">
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
