'use client';

import { StatusBadge } from '@/components/ui/badge';
import { Activity, Clock, Database, Network, RefreshDouble } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import { useHealth, useStats } from '@/lib/hooks';

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  if (parts.length === 0) parts.push(`${seconds}s`);

  return parts.join(' ');
}

function StatCard({
  label,
  value,
  icon,
}: {
  label: string;
  value: string | number;
  icon: React.ReactNode;
}) {
  return (
    <div className="bg-sc-bg-highlight/50 rounded-lg p-4 border border-sc-fg-subtle/10">
      <div className="flex items-center gap-2 text-sc-fg-muted mb-2">
        {icon}
        <span className="text-xs font-medium uppercase tracking-wide">{label}</span>
      </div>
      <p className="text-2xl font-semibold text-sc-fg-primary">{value}</p>
    </div>
  );
}

export default function SystemStatusPage() {
  const {
    data: health,
    isLoading: healthLoading,
    error: healthError,
    refetch: refetchHealth,
  } = useHealth();
  const { data: stats, isLoading: statsLoading } = useStats();

  const isLoading = healthLoading || statsLoading;

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
          <div className="flex items-center gap-3 mb-4">
            <Activity width={20} height={20} className="text-sc-cyan" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">System Status</h2>
          </div>
          <div className="flex items-center justify-center py-8">
            <Spinner size="md" color="purple" />
          </div>
        </div>
      </div>
    );
  }

  if (healthError) {
    return (
      <div className="space-y-6">
        <div className="bg-sc-bg-base rounded-lg border border-sc-red/20 p-6">
          <div className="flex items-center gap-3 mb-4">
            <Activity width={20} height={20} className="text-sc-red" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">System Status</h2>
          </div>
          <p className="text-sc-red mb-4">
            Failed to load system status. The server may be unavailable.
          </p>
          <button
            type="button"
            onClick={() => refetchHealth()}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/20 text-sm font-medium text-sc-fg-secondary hover:bg-sc-bg-base transition-colors"
          >
            <RefreshDouble width={14} height={14} />
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <Activity width={20} height={20} className="text-sc-cyan" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">System Status</h2>
          </div>
          <StatusBadge status={health?.status ?? 'unknown'} variant="chip" />
        </div>
        <p className="text-sc-fg-muted">
          Real-time health and diagnostics for the Sibyl server and its connected services.
        </p>
      </div>

      {/* Key Metrics */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Uptime"
          value={formatUptime(health?.uptime_seconds || 0)}
          icon={<Clock width={14} height={14} />}
        />
        <StatCard
          label="Total Entities"
          value={stats?.total_entities?.toLocaleString() || 0}
          icon={<Database width={14} height={14} />}
        />
        <StatCard
          label="Server"
          value={health?.server_name || 'sibyl'}
          icon={<Activity width={14} height={14} />}
        />
        <StatCard
          label="Graph Status"
          value={health?.graph_connected ? 'Connected' : 'Disconnected'}
          icon={<Network width={14} height={14} />}
        />
      </div>

      {/* Connections */}
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <h3 className="font-semibold text-sc-fg-primary mb-4">Connections</h3>
        <div className="space-y-3">
          <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
            <div className="flex items-center gap-3">
              <Network width={18} height={18} className="text-sc-coral" />
              <div>
                <p className="text-sm font-medium text-sc-fg-primary">FalkorDB Graph</p>
                <p className="text-xs text-sc-fg-muted">Knowledge graph storage</p>
              </div>
            </div>
            <StatusBadge
              status={health?.graph_connected ?? false}
              label={health?.graph_connected ? 'Connected' : 'Disconnected'}
              variant="chip"
            />
          </div>

          <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
            <div className="flex items-center gap-3">
              <Database width={18} height={18} className="text-sc-purple" />
              <div>
                <p className="text-sm font-medium text-sc-fg-primary">PostgreSQL</p>
                <p className="text-xs text-sc-fg-muted">User data and authentication</p>
              </div>
            </div>
            <StatusBadge
              status={health?.status === 'healthy'}
              label={health?.status === 'healthy' ? 'Connected' : 'Error'}
              variant="chip"
            />
          </div>
        </div>
      </div>

      {/* Entity Breakdown */}
      {stats?.entity_counts && Object.keys(stats.entity_counts).length > 0 && (
        <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
          <h3 className="font-semibold text-sc-fg-primary mb-4">Entity Breakdown</h3>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
            {Object.entries(stats.entity_counts)
              .sort(([, a], [, b]) => b - a)
              .map(([type, count]) => (
                <div
                  key={type}
                  className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10"
                >
                  <span className="text-sm text-sc-fg-secondary capitalize">{type}</span>
                  <span className="text-sm font-semibold text-sc-fg-primary">
                    {count.toLocaleString()}
                  </span>
                </div>
              ))}
          </div>
        </div>
      )}

      {/* Errors */}
      {health?.errors && health.errors.length > 0 && (
        <div className="bg-sc-bg-base rounded-lg border border-sc-red/20 p-6">
          <h3 className="font-semibold text-sc-red mb-4">Active Errors</h3>
          <div className="space-y-2">
            {health.errors.map((error, idx) => (
              <div
                key={idx}
                className="p-3 rounded-lg bg-sc-red/5 border border-sc-red/10 text-sm text-sc-red"
              >
                {error}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Last Updated */}
      <p className="text-xs text-sc-fg-subtle text-center">
        Status auto-refreshes every 30 seconds
      </p>
    </div>
  );
}
