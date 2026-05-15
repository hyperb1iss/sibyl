'use client';

import { toast } from 'sonner';
import { SettingsPageHeader } from '@/components/settings/primitives';
import { StatusBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Activity,
  Archive,
  Clock,
  Database,
  Layers,
  Network,
  RefreshDouble,
} from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type { BackgroundJobSummary } from '@/lib/api';
import { formatDateTime, formatDistanceToNow } from '@/lib/constants/formatting';
import { useHealth, useJobs, useRunMaintenanceJob, useStats } from '@/lib/hooks';

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

function maintenanceStatus(
  status: BackgroundJobSummary['status']
): 'healthy' | 'running' | 'warning' | 'unknown' {
  if (status === 'complete') return 'healthy';
  if (status === 'queued' || status === 'in_progress') return 'running';
  if (status === 'deferred') return 'warning';
  return 'unknown';
}

function maintenanceLabel(fn: BackgroundJobSummary['function']): string {
  if (fn === 'priority_decay') return 'Forgetting Sweep';
  return 'Consolidation';
}

function maintenanceTimestamp(job: BackgroundJobSummary): string | null {
  return job.finish_time ?? job.start_time ?? job.enqueue_time;
}

export default function SystemStatusPage() {
  const {
    data: health,
    isLoading: healthLoading,
    error: healthError,
    refetch: refetchHealth,
  } = useHealth();
  const { data: stats, isLoading: statsLoading } = useStats();
  const { data: jobsData, isLoading: jobsLoading } = useJobs({ limit: 25 });
  const runMaintenance = useRunMaintenanceJob();

  const maintenanceJobs = (jobsData?.jobs ?? []).filter(
    job => job.function === 'consolidate_org' || job.function === 'priority_decay'
  );
  const latestConsolidation = maintenanceJobs.find(job => job.function === 'consolidate_org');
  const latestForgetting = maintenanceJobs.find(job => job.function === 'priority_decay');

  const isLoading = healthLoading || statsLoading;

  async function handleRun(action: 'consolidate' | 'forget') {
    try {
      const response = await runMaintenance.mutateAsync({ action });
      toast.success(response.message);
    } catch {
      toast.error(
        action === 'consolidate'
          ? 'Failed to queue consolidation'
          : 'Failed to queue forgetting sweep'
      );
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <SettingsPageHeader
          icon={Activity}
          iconColor="text-sc-cyan"
          title="System"
          description="Real-time health and diagnostics."
        />
        <div className="flex items-center justify-center py-12">
          <Spinner size="md" color="purple" />
        </div>
      </div>
    );
  }

  if (healthError) {
    return (
      <div className="space-y-6">
        <SettingsPageHeader
          icon={Activity}
          iconColor="text-sc-red"
          title="System"
          description="Real-time health and diagnostics."
          actions={
            <Button
              variant="secondary"
              size="sm"
              onClick={() => refetchHealth()}
              icon={<RefreshDouble width={14} height={14} />}
            >
              Retry
            </Button>
          }
        />
        <div className="rounded-lg border border-sc-red/20 bg-sc-red/5 p-4 text-sm text-sc-red">
          Failed to load system status. The server may be unavailable.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SettingsPageHeader
        icon={Activity}
        iconColor="text-sc-cyan"
        title="System"
        description="Real-time health and diagnostics for the Sibyl server and its services."
        actions={<StatusBadge status={health?.status ?? 'unknown'} variant="chip" />}
      />

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

      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <h3 className="font-semibold text-sc-fg-primary mb-4">Connections</h3>
        <div className="space-y-3">
          <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
            <div className="flex items-center gap-3">
              <Network width={18} height={18} className="text-sc-coral" />
              <div>
                <p className="text-sm font-medium text-sc-fg-primary">Knowledge Graph</p>
                <p className="text-xs text-sc-fg-muted">Graph runtime and retrieval</p>
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
                <p className="text-sm font-medium text-sc-fg-primary">Storage Runtime</p>
                <p className="text-xs text-sc-fg-muted">Auth, content, and services</p>
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

      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="flex items-center gap-3 mb-2">
              <Layers width={18} height={18} className="text-sc-cyan" />
              <h3 className="font-semibold text-sc-fg-primary">Memory Maintenance</h3>
            </div>
            <p className="text-sm text-sc-fg-muted max-w-2xl">
              Trigger consolidation and forgetting sweeps for the current organization, then review
              the most recent maintenance activity below.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Button
              variant="secondary"
              loading={runMaintenance.isPending}
              icon={<Layers width={16} height={16} />}
              onClick={() => handleRun('consolidate')}
            >
              Run Consolidation
            </Button>
            <Button
              variant="secondary"
              loading={runMaintenance.isPending}
              icon={<Archive width={16} height={16} />}
              onClick={() => handleRun('forget')}
            >
              Run Forgetting Sweep
            </Button>
          </div>
        </div>

        <div className="grid gap-4 mt-6 md:grid-cols-2">
          {[
            {
              title: 'Latest Consolidation',
              icon: <Layers width={16} height={16} className="text-sc-cyan" />,
              job: latestConsolidation,
              empty: 'No consolidation run recorded yet.',
            },
            {
              title: 'Latest Forgetting Sweep',
              icon: <Archive width={16} height={16} className="text-sc-coral" />,
              job: latestForgetting,
              empty: 'No forgetting sweep recorded yet.',
            },
          ].map(section => (
            <div
              key={section.title}
              className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/40 p-4"
            >
              <div className="flex items-center justify-between gap-3 mb-3">
                <div className="flex items-center gap-2">
                  {section.icon}
                  <h4 className="text-sm font-medium text-sc-fg-primary">{section.title}</h4>
                </div>
                <StatusBadge
                  status={section.job ? maintenanceStatus(section.job.status) : 'unknown'}
                  label={section.job ? maintenanceLabel(section.job.function) : 'Idle'}
                  variant="chip"
                />
              </div>
              {section.job ? (
                <div className="space-y-1 text-sm">
                  <p className="text-sc-fg-secondary">
                    {section.job.error
                      ? section.job.error
                      : `Status: ${section.job.status.replace('_', ' ')}`}
                  </p>
                  {maintenanceTimestamp(section.job) && (
                    <p className="text-sc-fg-muted">
                      {formatDistanceToNow(maintenanceTimestamp(section.job) as string)}
                      {' · '}
                      {formatDateTime(maintenanceTimestamp(section.job) as string)}
                    </p>
                  )}
                </div>
              ) : (
                <p className="text-sm text-sc-fg-muted">{section.empty}</p>
              )}
            </div>
          ))}
        </div>

        <div className="mt-6">
          <h4 className="text-sm font-medium text-sc-fg-primary mb-3">Recent Activity</h4>
          {jobsLoading && maintenanceJobs.length === 0 ? (
            <div className="flex items-center justify-center rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/30 py-6">
              <Spinner size="sm" color="purple" />
            </div>
          ) : maintenanceJobs.length > 0 ? (
            <div className="space-y-3">
              {maintenanceJobs.slice(0, 6).map(job => {
                const timestamp = maintenanceTimestamp(job);

                return (
                  <div
                    key={job.job_id}
                    className="flex flex-col gap-3 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/30 p-4 md:flex-row md:items-center md:justify-between"
                  >
                    <div className="space-y-1">
                      <div className="flex items-center gap-3">
                        <p className="text-sm font-medium text-sc-fg-primary">
                          {maintenanceLabel(job.function)}
                        </p>
                        <StatusBadge
                          status={maintenanceStatus(job.status)}
                          label={job.status.replace('_', ' ')}
                          variant="chip"
                        />
                      </div>
                      <p className="text-xs text-sc-fg-muted">{job.job_id}</p>
                    </div>
                    <div className="text-sm text-sc-fg-muted md:text-right">
                      {timestamp ? (
                        <>
                          <p>{formatDistanceToNow(timestamp)}</p>
                          <p>{formatDateTime(timestamp)}</p>
                        </>
                      ) : (
                        <p>Waiting for timestamps</p>
                      )}
                      {job.error && <p className="text-sc-red mt-1">{job.error}</p>}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/30 p-4 text-sm text-sc-fg-muted">
              No maintenance jobs have run for this organization yet.
            </div>
          )}
        </div>
      </div>

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

      <p className="text-xs text-sc-fg-subtle text-center">
        Status auto-refreshes every 30 seconds
      </p>
    </div>
  );
}
