'use client';

import { useState } from 'react';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { StatusBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input, Label } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { Toggle } from '@/components/ui/toggle';
import { TIMING } from '@/lib/constants';
import { useHealth, useIngest, useIngestStatus, useStats } from '@/lib/hooks';

export default function IngestPage() {
  const { data: health } = useHealth();
  const { data: stats, refetch: refetchStats } = useStats();
  const { data: status } = useIngestStatus();
  const ingest = useIngest();

  const [customPath, setCustomPath] = useState('');
  const [force, setForce] = useState(false);

  const handleIngest = async () => {
    await ingest.mutateAsync({
      path: customPath || undefined,
      force,
    });
    setTimeout(() => refetchStats(), TIMING.REFETCH_DELAY);
  };

  const isRunning = status?.running ?? false;
  const progress = status?.progress ?? 0;

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />

      <PageHeader description="Sync documents and extract knowledge entities" />

      {/* Status Card */}
      <Card>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-sc-fg-primary">Ingestion Status</h2>
          <StatusBadge status={isRunning ? 'running' : 'idle'} pulse={isRunning} />
        </div>

        {isRunning && status ? (
          <div className="space-y-4">
            <Progress value={progress} showLabel />

            {/* Stats */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-sc-bg-highlight rounded-lg p-3">
                <div className="text-2xl font-bold text-sc-fg-primary">
                  {status.files_processed}
                </div>
                <div className="text-xs text-sc-fg-muted">Files Processed</div>
              </div>
              <div className="bg-sc-bg-highlight rounded-lg p-3">
                <div className="text-2xl font-bold text-sc-green">{status.entities_created}</div>
                <div className="text-xs text-sc-fg-muted">Entities Created</div>
              </div>
              <div className="bg-sc-bg-highlight rounded-lg p-3">
                <div className="text-2xl font-bold text-sc-cyan">{status.entities_updated}</div>
                <div className="text-xs text-sc-fg-muted">Entities Updated</div>
              </div>
              <div className="bg-sc-bg-highlight rounded-lg p-3">
                <div className="text-2xl font-bold text-sc-red">{status.errors?.length ?? 0}</div>
                <div className="text-xs text-sc-fg-muted">Errors</div>
              </div>
            </div>

            {/* Errors */}
            {status.errors && status.errors.length > 0 && (
              <div className="bg-sc-red/10 border border-sc-red/30 rounded-lg p-4">
                <h3 className="text-sm font-medium text-sc-red mb-2">Errors</h3>
                <ul className="space-y-1 text-sm text-sc-fg-muted">
                  {status.errors.map((error: string) => (
                    <li key={error}>• {error}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sc-fg-muted">
            Ready to ingest documents. Configure options below and start ingestion.
          </p>
        )}
      </Card>

      {/* Configuration */}
      <Card>
        <h2 className="text-lg font-semibold text-sc-fg-primary mb-4">Configuration</h2>

        <div className="space-y-6">
          <div>
            <Label
              htmlFor="custom-path"
              description="Specify a directory path to ingest, or leave empty to use configured sources"
            >
              Custom Path (optional)
            </Label>
            <Input
              id="custom-path"
              type="text"
              value={customPath}
              onChange={e => setCustomPath(e.target.value)}
              placeholder="Leave empty to use default knowledge sources"
              className="font-mono"
              disabled={isRunning}
            />
          </div>

          <Toggle
            checked={force}
            onChange={setForce}
            disabled={isRunning}
            label="Force Re-ingest"
            description="Re-process all files even if they haven't changed"
          />

          <Button
            size="lg"
            onClick={handleIngest}
            disabled={isRunning || ingest.isPending}
            loading={isRunning || ingest.isPending}
            icon="↻"
            className="w-full"
          >
            {isRunning ? 'Ingesting...' : ingest.isPending ? 'Starting...' : 'Start Ingestion'}
          </Button>
        </div>
      </Card>

      {/* Current Stats */}
      <Card>
        <h2 className="text-lg font-semibold text-sc-fg-primary mb-4">Current Knowledge Base</h2>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="bg-sc-bg-highlight rounded-lg p-4">
            <div className="text-3xl font-bold text-sc-fg-primary">
              {stats?.total_entities ?? 0}
            </div>
            <div className="text-sm text-sc-fg-muted">Total Entities</div>
          </div>

          {stats?.entity_counts &&
            Object.entries(stats.entity_counts)
              .slice(0, 3)
              .map(([type, count]) => (
                <div key={type} className="bg-sc-bg-highlight rounded-lg p-4">
                  <div className="text-3xl font-bold text-sc-fg-primary">{count}</div>
                  <div className="text-sm text-sc-fg-muted capitalize">
                    {type.replace(/_/g, ' ')}
                  </div>
                </div>
              ))}
        </div>
      </Card>

      {/* Server Info */}
      <Card>
        <h2 className="text-lg font-semibold text-sc-fg-primary mb-4">Server Info</h2>

        <dl className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <dt className="text-sc-fg-subtle">Server</dt>
            <dd className="text-sc-fg-primary font-mono">{health?.server_name ?? 'sibyl'}</dd>
          </div>
          <div>
            <dt className="text-sc-fg-subtle">Status</dt>
            <dd className={health?.status === 'healthy' ? 'text-sc-green' : 'text-sc-red'}>
              {health?.status ?? 'Unknown'}
            </dd>
          </div>
          <div>
            <dt className="text-sc-fg-subtle">Graph</dt>
            <dd className={health?.graph_connected ? 'text-sc-green' : 'text-sc-red'}>
              {health?.graph_connected ? 'Connected' : 'Disconnected'}
            </dd>
          </div>
          <div>
            <dt className="text-sc-fg-subtle">Version</dt>
            <dd className="text-sc-fg-primary font-mono">0.1.0</dd>
          </div>
        </dl>
      </Card>
    </div>
  );
}
