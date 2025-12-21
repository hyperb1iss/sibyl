'use client';

import { Suspense, useCallback, useState } from 'react';

import { GRAPH_DEFAULTS } from '@/lib/constants';
import { Card } from '@/components/ui/card';
import { FilterChip } from '@/components/ui/toggle';
import { LoadingState } from '@/components/ui/spinner';
import { EntityLegend } from '@/components/entities/entity-legend';
import { PageHeader } from '@/components/layout/page-header';
import { KnowledgeGraph } from '@/components/graph/knowledge-graph';
import { EntityDetailPanel } from '@/components/graph/entity-detail-panel';
import { useStats } from '@/lib/hooks';

function GraphControls({
  onFilterChange,
  filters,
}: {
  onFilterChange: (filters: { types?: string[] }) => void;
  filters: { types?: string[] };
}) {
  const { data: stats } = useStats();
  const entityTypes = stats ? Object.keys(stats.entity_counts) : [];

  const toggleType = (type: string) => {
    const current = filters.types || [];
    const newTypes = current.includes(type)
      ? current.filter((t) => t !== type)
      : [...current, type];
    onFilterChange({ types: newTypes.length > 0 ? newTypes : undefined });
  };

  return (
    <Card className="!p-4">
      <h3 className="text-sm font-medium text-sc-fg-muted mb-3">Entity Types</h3>
      <div className="flex flex-wrap gap-2">
        {entityTypes.map((type) => {
          const isActive = !filters.types || filters.types.includes(type);
          return (
            <FilterChip key={type} active={isActive} onClick={() => toggleType(type)}>
              {type.replace(/_/g, ' ')}
            </FilterChip>
          );
        })}
      </div>
    </Card>
  );
}

function GraphPageContent() {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [filters, setFilters] = useState<{ types?: string[] }>({});

  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedNodeId(prev => prev === nodeId ? null : nodeId);
  }, []);

  const handleClosePanel = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  return (
    <div className="h-full flex flex-col gap-4">
      <PageHeader
        title="Knowledge Graph"
        description="Explore entity relationships and patterns"
      />

      {/* Controls */}
      <GraphControls filters={filters} onFilterChange={setFilters} />

      {/* Main content area */}
      <div className="flex-1 flex gap-4 min-h-0">
        {/* Graph container */}
        <div className="flex-1 relative bg-sc-bg-dark rounded-xl border border-sc-fg-subtle/20 overflow-hidden">
          <KnowledgeGraph
            onNodeClick={handleNodeClick}
            selectedNodeId={selectedNodeId}
            maxNodes={GRAPH_DEFAULTS.MAX_NODES}
          />
        </div>

        {/* Entity detail panel */}
        {selectedNodeId && (
          <EntityDetailPanel
            entityId={selectedNodeId}
            onClose={handleClosePanel}
          />
        )}
      </div>

      {/* Legend */}
      <EntityLegend />
    </div>
  );
}

export default function GraphPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <GraphPageContent />
    </Suspense>
  );
}
