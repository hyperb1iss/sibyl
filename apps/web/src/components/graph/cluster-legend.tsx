'use client';

import { useState } from 'react';
import { Card } from '@/components/ui/card';
import { ChevronDown, ChevronUp } from '@/components/ui/icons';
import type { HierarchicalCluster } from '@/lib/api';
import type { GraphNode } from './graph-types';

export function getClusterLabel(cluster: HierarchicalCluster, nodes: GraphNode[]): string {
  const clusterNodes = nodes
    .filter(node => node.cluster_id === cluster.id)
    .sort((a, b) => (b.degree || 0) - (a.degree || 0));

  if (clusterNodes.length === 0) {
    return cluster.dominant_type?.replace(/_/g, ' ') || 'Mixed';
  }

  const topNames = clusterNodes
    .slice(0, 2)
    .map(node => {
      const name = node.label || node.name || '';
      return name.length > 15 ? `${name.slice(0, 12)}...` : name;
    })
    .filter(Boolean);

  return topNames.length > 0
    ? topNames.join(', ')
    : cluster.dominant_type?.replace(/_/g, ' ') || 'Mixed';
}

function getClusterDisplayCount(cluster: HierarchicalCluster): number {
  return cluster.displayed_member_count ?? cluster.member_count;
}

function formatClusterCount(cluster: HierarchicalCluster): string {
  const displayed = cluster.displayed_member_count;
  if (displayed == null || displayed === cluster.member_count) {
    return cluster.member_count.toLocaleString();
  }
  return `${displayed.toLocaleString()}/${cluster.member_count.toLocaleString()}`;
}

interface ClusterLegendProps {
  clusters: HierarchicalCluster[];
  clusterColorMap: Map<string, string>;
  selectedCluster: string | null;
  onClusterClick: (clusterId: string | null) => void;
  nodes: GraphNode[];
}

export function ClusterLegend({
  clusters,
  clusterColorMap,
  selectedCluster,
  onClusterClick,
  nodes,
}: ClusterLegendProps) {
  const [expanded, setExpanded] = useState(true);

  if (clusters.length === 0) return null;

  return (
    <Card className="!p-0 max-w-xs">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
      >
        <span>Clusters ({clusters.length})</span>
        {expanded ? <ChevronUp width={14} height={14} /> : <ChevronDown width={14} height={14} />}
      </button>
      {expanded && (
        <div className="px-3 pb-3 space-y-1 max-h-48 overflow-y-auto">
          <button
            type="button"
            onClick={() => onClusterClick(null)}
            className={`w-full flex items-center gap-2 px-2 py-1 rounded text-xs transition-colors ${
              selectedCluster === null
                ? 'bg-sc-purple/20 text-sc-purple'
                : 'text-sc-fg-muted hover:text-sc-fg-primary'
            }`}
          >
            <div className="w-2 h-2 rounded-full bg-gradient-to-r from-sc-purple to-sc-cyan" />
            <span>All clusters</span>
          </button>
          {[...clusters]
            .sort((a, b) => getClusterDisplayCount(b) - getClusterDisplayCount(a))
            .map(cluster => {
              const color = clusterColorMap.get(cluster.id) || '#8b85a0';
              const isSelected = selectedCluster === cluster.id;
              const label = cluster.label || getClusterLabel(cluster, nodes);
              return (
                <button
                  key={cluster.id}
                  type="button"
                  onClick={() => onClusterClick(cluster.id)}
                  className={`w-full flex items-center gap-2 px-2 py-1 rounded text-xs transition-colors ${
                    isSelected
                      ? 'bg-sc-purple/20 text-sc-fg-primary'
                      : 'text-sc-fg-muted hover:text-sc-fg-primary'
                  }`}
                  title={label}
                >
                  <div
                    className="w-2 h-2 rounded-full flex-shrink-0"
                    style={{ backgroundColor: color }}
                  />
                  <span className="truncate">{label}</span>
                  <span className="ml-auto text-sc-fg-subtle flex-shrink-0">
                    {formatClusterCount(cluster)}
                  </span>
                </button>
              );
            })}
        </div>
      )}
    </Card>
  );
}
