'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useState } from 'react';

import { GRAPH_DEFAULTS } from '@/lib/constants';
import { Card } from '@/components/ui/card';
import { ColorButton } from '@/components/ui/button';
import { FilterChip } from '@/components/ui/toggle';
import { LoadingState } from '@/components/ui/spinner';
import { EntityLegend } from '@/components/entities/entity-legend';
import { PageHeader } from '@/components/layout/page-header';
import { KnowledgeGraph } from '@/components/graph/knowledge-graph';
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
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedNodeId = searchParams.get('selected');

  const [filters, setFilters] = useState<{ types?: string[] }>({});

  const handleNodeClick = useCallback(
    (nodeId: string) => {
      const params = new URLSearchParams(searchParams);
      if (nodeId === selectedNodeId) {
        params.delete('selected');
      } else {
        params.set('selected', nodeId);
      }
      router.push(`/graph?${params.toString()}`);
    },
    [router, searchParams, selectedNodeId]
  );

  const handleViewEntity = useCallback(() => {
    if (selectedNodeId) {
      router.push(`/entities/${selectedNodeId}`);
    }
  }, [router, selectedNodeId]);

  return (
    <div className="h-full flex flex-col gap-4">
      <PageHeader
        title="Knowledge Graph"
        description="Explore entity relationships and patterns"
        action={
          selectedNodeId ? (
            <ColorButton color="cyan" onClick={handleViewEntity}>
              View Entity
            </ColorButton>
          ) : null
        }
      />

      {/* Controls */}
      <GraphControls filters={filters} onFilterChange={setFilters} />

      {/* Graph */}
      <div className="flex-1 bg-sc-bg-dark rounded-xl border border-sc-fg-subtle/20 overflow-hidden min-h-[400px]">
        <KnowledgeGraph
          onNodeClick={handleNodeClick}
          selectedNodeId={selectedNodeId}
          maxNodes={GRAPH_DEFAULTS.MAX_NODES}
        />
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
