'use client';

import { useMemo } from 'react';
import { darkTheme, GraphCanvas, type GraphEdge, type GraphNode } from 'reagraph';

import { useGraphData } from '@/lib/hooks';
import { ENTITY_COLORS } from '@/lib/constants';
import { LoadingState } from '@/components/ui/spinner';
import { EmptyState, ErrorState } from '@/components/ui/tooltip';

const DEFAULT_NODE_COLOR = '#8b85a0';

interface KnowledgeGraphProps {
  onNodeClick?: (nodeId: string) => void;
  selectedNodeId?: string | null;
  maxNodes?: number;
}

export function KnowledgeGraph({
  onNodeClick,
  selectedNodeId,
  maxNodes = 500,
}: KnowledgeGraphProps) {
  const { data, isLoading, error } = useGraphData({ max_nodes: maxNodes, max_edges: 1000 });

  // Memoize node/edge transformation to avoid recalculating on every render
  const { nodes, edges } = useMemo(() => {
    if (!data) return { nodes: [], edges: [] };

    const transformedNodes: GraphNode[] = data.nodes.map(node => ({
      id: node.id,
      label: node.label,
      fill: node.color || ENTITY_COLORS[node.type as keyof typeof ENTITY_COLORS] || DEFAULT_NODE_COLOR,
      size: Math.max(10, Math.min(40, node.size * 5)),
      data: { type: node.type },
    }));

    const transformedEdges: GraphEdge[] = data.edges.map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.type,
      size: Math.max(1, edge.weight * 2),
    }));

    return { nodes: transformedNodes, edges: transformedEdges };
  }, [data]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full bg-sc-bg-dark">
        <LoadingState
          message="Loading knowledge graph..."
          variant="orbital"
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full bg-sc-bg-dark">
        <ErrorState
          variant="error"
          title="Failed to load graph"
          message={error.message}
        />
      </div>
    );
  }

  if (!data || data.nodes.length === 0) {
    return (
      <div className="flex items-center justify-center h-full bg-sc-bg-dark">
        <EmptyState
          variant="data"
          title="No entities in the knowledge graph"
          description="Try ingesting some documents to populate the graph"
        />
      </div>
    );
  }

  return (
    <div className="relative w-full h-full" style={{ minHeight: '400px' }}>
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        cameraMode="pan"
        layoutType="forceDirected2d"
        draggable
        animated
        theme={darkTheme}
        labelType="all"
        edgeArrowPosition="end"
        sizingType="attribute"
        sizingAttribute="size"
        selections={selectedNodeId ? [selectedNodeId] : []}
        onNodeClick={node => {
          onNodeClick?.(node.id);
        }}
      />
    </div>
  );
}
