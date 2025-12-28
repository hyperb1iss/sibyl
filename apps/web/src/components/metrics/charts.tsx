'use client';

/**
 * SilkCircuit-styled chart components using Recharts.
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import type {
  AssigneeStats,
  TaskPriorityDistribution,
  TaskStatusDistribution,
  TimeSeriesPoint,
} from '@/lib/api';

// SilkCircuit color palette
const COLORS = {
  purple: '#e135ff',
  cyan: '#80ffea',
  coral: '#ff6ac1',
  yellow: '#f1fa8c',
  green: '#50fa7b',
  red: '#ff6363',
  muted: '#8b85a0',
  bg: '#12101a',
  bgElevated: '#1a162a',
};

// Status colors matching the app's status config
const STATUS_COLORS: Record<string, string> = {
  backlog: COLORS.muted,
  todo: COLORS.cyan,
  doing: COLORS.purple,
  blocked: COLORS.red,
  review: COLORS.yellow,
  done: COLORS.green,
};

// Priority colors
const PRIORITY_COLORS: Record<string, string> = {
  critical: COLORS.red,
  high: COLORS.coral,
  medium: COLORS.yellow,
  low: COLORS.cyan,
  someday: COLORS.muted,
};

// Custom tooltip component
function CustomTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ value: number; name: string; color: string }>;
  label?: string;
}) {
  if (!active || !payload?.length) return null;

  return (
    <div className="bg-sc-bg-elevated border border-sc-fg-subtle/30 rounded-lg px-3 py-2 shadow-xl">
      {label && <p className="text-xs text-sc-fg-muted mb-1">{label}</p>}
      {payload.map((entry, index) => (
        <p
          key={index}
          className="text-sm font-medium"
          style={{ color: entry.color || COLORS.cyan }}
        >
          {entry.name}: {entry.value}
        </p>
      ))}
    </div>
  );
}

// =============================================================================
// Status Distribution Donut Chart
// =============================================================================

interface StatusDonutProps {
  data: TaskStatusDistribution;
  className?: string;
}

export function StatusDonutChart({ data, className }: StatusDonutProps) {
  const chartData = Object.entries(data)
    .filter(([_, value]) => value > 0)
    .map(([name, value]) => ({
      name: name.charAt(0).toUpperCase() + name.slice(1),
      value,
      color: STATUS_COLORS[name] || COLORS.muted,
    }));

  const total = chartData.reduce((sum, item) => sum + item.value, 0);

  if (total === 0) {
    return (
      <div className={`flex items-center justify-center h-48 ${className}`}>
        <p className="text-sc-fg-subtle text-sm">No task data</p>
      </div>
    );
  }

  return (
    <div className={className}>
      <ResponsiveContainer width="100%" height={200}>
        <PieChart>
          <Pie
            data={chartData}
            cx="50%"
            cy="50%"
            innerRadius={50}
            outerRadius={80}
            paddingAngle={2}
            dataKey="value"
            strokeWidth={0}
          >
            {chartData.map((entry, index) => (
              <Cell
                key={`cell-${index}`}
                fill={entry.color}
                style={{ filter: `drop-shadow(0 0 6px ${entry.color}40)` }}
              />
            ))}
          </Pie>
          <Tooltip content={<CustomTooltip />} />
          <Legend
            verticalAlign="bottom"
            height={36}
            formatter={(value: string) => <span className="text-xs text-sc-fg-muted">{value}</span>}
          />
        </PieChart>
      </ResponsiveContainer>
      <div className="text-center -mt-24">
        <span className="text-2xl font-bold text-sc-fg-primary">{total}</span>
        <span className="block text-xs text-sc-fg-subtle">Tasks</span>
      </div>
    </div>
  );
}

// =============================================================================
// Priority Bar Chart
// =============================================================================

interface PriorityBarProps {
  data: TaskPriorityDistribution;
  className?: string;
}

export function PriorityBarChart({ data, className }: PriorityBarProps) {
  const chartData = Object.entries(data).map(([name, value]) => ({
    name: name.charAt(0).toUpperCase() + name.slice(1),
    value,
    fill: PRIORITY_COLORS[name] || COLORS.muted,
  }));

  return (
    <div className={className}>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={chartData} layout="vertical" margin={{ left: 10, right: 20 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={`${COLORS.muted}20`} horizontal={false} />
          <XAxis type="number" tick={{ fill: COLORS.muted, fontSize: 11 }} axisLine={false} />
          <YAxis
            type="category"
            dataKey="name"
            tick={{ fill: COLORS.muted, fontSize: 11 }}
            axisLine={false}
            width={70}
          />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: COLORS.bgElevated }} />
          <Bar dataKey="value" radius={[0, 4, 4, 0]}>
            {chartData.map((entry, index) => (
              <Cell key={`cell-${index}`} fill={entry.fill} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// =============================================================================
// Velocity Line Chart
// =============================================================================

interface VelocityChartProps {
  data: TimeSeriesPoint[];
  className?: string;
}

export function VelocityLineChart({ data, className }: VelocityChartProps) {
  // Format dates for display
  const chartData = data.map(point => ({
    ...point,
    displayDate: new Date(point.date).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
    }),
  }));

  // Check if there's any actual data (not all zeros)
  const hasActivity = data.some(point => point.value > 0);

  if (chartData.length === 0) {
    return (
      <div className={`flex items-center justify-center h-40 ${className}`}>
        <p className="text-sc-fg-subtle text-sm">No velocity data</p>
      </div>
    );
  }

  if (!hasActivity) {
    return (
      <div className={`flex flex-col items-center justify-center h-40 ${className}`}>
        <p className="text-sc-fg-subtle text-sm">No completions in the last 14 days</p>
        <p className="text-sc-fg-muted text-xs mt-1">Complete tasks to see velocity trends</p>
      </div>
    );
  }

  return (
    <div className={className}>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={chartData} margin={{ left: -10, right: 10, top: 10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={`${COLORS.muted}20`} />
          <XAxis
            dataKey="displayDate"
            tick={{ fill: COLORS.muted, fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fill: COLORS.muted, fontSize: 11 }}
            axisLine={false}
            tickLine={false}
            allowDecimals={false}
          />
          <Tooltip
            content={<CustomTooltip />}
            labelFormatter={(label: string) => `Date: ${label}`}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke={COLORS.green}
            strokeWidth={2}
            dot={{ fill: COLORS.green, strokeWidth: 0, r: 3 }}
            activeDot={{ r: 5, fill: COLORS.green, stroke: COLORS.bg, strokeWidth: 2 }}
            name="Completed"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// =============================================================================
// Assignee Bar Chart
// =============================================================================

interface AssigneeChartProps {
  data: AssigneeStats[];
  className?: string;
  maxItems?: number;
}

export function AssigneeBarChart({ data, className, maxItems = 8 }: AssigneeChartProps) {
  const chartData = data.slice(0, maxItems).map(assignee => ({
    name: assignee.name.length > 12 ? `${assignee.name.slice(0, 12)}...` : assignee.name,
    fullName: assignee.name,
    completed: assignee.completed,
    in_progress: assignee.in_progress,
    pending: assignee.total - assignee.completed - assignee.in_progress,
  }));

  if (chartData.length === 0) {
    return (
      <div className={`flex items-center justify-center h-48 ${className}`}>
        <p className="text-sc-fg-subtle text-sm">No assignee data</p>
      </div>
    );
  }

  return (
    <div className={className}>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={chartData} margin={{ left: -10, right: 10, top: 10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={`${COLORS.muted}20`} vertical={false} />
          <XAxis
            dataKey="name"
            tick={{ fill: COLORS.muted, fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            interval={0}
            angle={-45}
            textAnchor="end"
            height={60}
          />
          <YAxis
            tick={{ fill: COLORS.muted, fontSize: 11 }}
            axisLine={false}
            tickLine={false}
            allowDecimals={false}
          />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: COLORS.bgElevated }} />
          <Legend
            verticalAlign="top"
            height={36}
            formatter={(value: string) => (
              <span className="text-xs text-sc-fg-muted capitalize">{value.replace('_', ' ')}</span>
            )}
          />
          <Bar
            dataKey="completed"
            stackId="a"
            fill={COLORS.green}
            radius={[0, 0, 0, 0]}
            name="Completed"
          />
          <Bar
            dataKey="in_progress"
            stackId="a"
            fill={COLORS.purple}
            radius={[0, 0, 0, 0]}
            name="In Progress"
          />
          <Bar
            dataKey="pending"
            stackId="a"
            fill={COLORS.cyan}
            radius={[4, 4, 0, 0]}
            name="Pending"
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// =============================================================================
// Project Comparison Bar Chart
// =============================================================================

interface ProjectComparisonProps {
  data: Array<{
    id: string;
    name: string;
    total: number;
    completed: number;
    completion_rate: number;
  }>;
  className?: string;
  maxItems?: number;
}

export function ProjectComparisonChart({ data, className, maxItems = 10 }: ProjectComparisonProps) {
  const chartData = data.slice(0, maxItems).map(project => ({
    name: project.name.length > 15 ? `${project.name.slice(0, 15)}...` : project.name,
    fullName: project.name,
    completed: project.completed,
    remaining: project.total - project.completed,
    rate: project.completion_rate,
  }));

  if (chartData.length === 0) {
    return (
      <div className={`flex items-center justify-center h-48 ${className}`}>
        <p className="text-sc-fg-subtle text-sm">No project data</p>
      </div>
    );
  }

  return (
    <div className={className}>
      <ResponsiveContainer width="100%" height={250}>
        <BarChart
          data={chartData}
          layout="vertical"
          margin={{ left: 10, right: 20, top: 10, bottom: 10 }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke={`${COLORS.muted}20`} horizontal={false} />
          <XAxis type="number" tick={{ fill: COLORS.muted, fontSize: 11 }} axisLine={false} />
          <YAxis
            type="category"
            dataKey="name"
            tick={{ fill: COLORS.muted, fontSize: 11 }}
            axisLine={false}
            width={100}
          />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: COLORS.bgElevated }} />
          <Legend
            verticalAlign="top"
            height={36}
            formatter={(value: string) => <span className="text-xs text-sc-fg-muted">{value}</span>}
          />
          <Bar dataKey="completed" stackId="a" fill={COLORS.green} name="Completed" />
          <Bar
            dataKey="remaining"
            stackId="a"
            fill={COLORS.muted}
            radius={[0, 4, 4, 0]}
            name="Remaining"
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
