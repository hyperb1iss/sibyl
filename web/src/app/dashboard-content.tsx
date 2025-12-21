'use client';

import Link from 'next/link';

import { Card, StatCard } from '@/components/ui/card';
import { ColorButton } from '@/components/ui/button';
import { EntityBreakdown } from '@/components/entities/entity-legend';
import { PageHeader } from '@/components/layout/page-header';
import { useHealth, useStats } from '@/lib/hooks';
import { formatUptime, QUICK_ACTIONS } from '@/lib/constants';
import type { StatsResponse } from '@/lib/api';

interface DashboardContentProps {
  initialStats: StatsResponse;
}

export function DashboardContent({ initialStats }: DashboardContentProps) {
  // Health is always client-side (polling for real-time status)
  const { data: health, isLoading: healthLoading } = useHealth();

  // Stats hydrate from server, then update via WebSocket invalidation
  const { data: stats, isLoading: statsLoading } = useStats(initialStats);

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Dashboard"
        description="Knowledge graph overview and server status"
      />

      {/* Status Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Server Status"
          value={healthLoading ? '...' : health?.status === 'healthy' ? 'Healthy' : 'Unhealthy'}
          icon={
            <span
              className={`w-3 h-3 rounded-full inline-block ${
                health?.status === 'healthy'
                  ? 'bg-sc-green animate-pulse'
                  : health?.status === 'unhealthy'
                    ? 'bg-sc-red'
                    : 'bg-sc-yellow animate-pulse'
              }`}
            />
          }
          sublabel={health?.graph_connected ? 'Graph connected' : 'Graph disconnected'}
          loading={healthLoading}
        />

        <StatCard
          label="Total Entities"
          value={statsLoading ? '...' : (stats?.total_entities ?? 0)}
          icon="▣"
          sublabel="Across all types"
          loading={statsLoading}
        />

        <StatCard
          label="Uptime"
          value={healthLoading ? '...' : formatUptime(health?.uptime_seconds ?? 0)}
          icon="↑"
          sublabel={health?.server_name ?? 'sibyl'}
          loading={healthLoading}
        />

        <StatCard
          label="Patterns"
          value={statsLoading ? '...' : (stats?.entity_counts?.pattern ?? 0)}
          icon="◆"
          sublabel="Development patterns"
          loading={statsLoading}
        />
      </div>

      {/* Entity Breakdown */}
      <Card>
        <h3 className="text-lg font-semibold text-sc-fg-primary mb-4">Entity Types</h3>
        <EntityBreakdown counts={stats?.entity_counts ?? {}} loading={statsLoading} />
      </Card>

      {/* Quick Actions */}
      <Card>
        <h3 className="text-lg font-semibold text-sc-fg-primary mb-4">Quick Actions</h3>
        <div className="flex flex-wrap gap-3">
          {QUICK_ACTIONS.map((action) => (
            <Link key={action.href} href={action.href}>
              <ColorButton color={action.color} icon={action.icon}>
                {action.label}
              </ColorButton>
            </Link>
          ))}
        </div>
      </Card>

      {/* Errors */}
      {health?.errors && health.errors.length > 0 && (
        <Card className="!bg-sc-red/10 !border-sc-red/30">
          <h3 className="text-lg font-semibold text-sc-red mb-2">Errors</h3>
          <ul className="space-y-1">
            {health.errors.map((error: string) => (
              <li key={error} className="text-sc-fg-muted text-sm">
                • {error}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
