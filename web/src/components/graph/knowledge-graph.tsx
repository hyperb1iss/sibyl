'use client';

import dynamic from 'next/dynamic';
import { forwardRef, useCallback, useImperativeHandle, useMemo, useRef } from 'react';
import { EmptyState } from '@/components/ui/tooltip';
import { ENTITY_COLORS } from '@/lib/constants';

// Dynamic import to avoid SSR issues with canvas
const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-dark">
      <div className="text-sc-fg-muted">Loading graph...</div>
    </div>
  ),
});

const DEFAULT_NODE_COLOR = '#8b85a0';

// Edge colors by relationship type
const EDGE_COLORS: Record<string, string> = {
  APPLIES_TO: '#e135ff',
  REQUIRES: '#80ffea',
  CONFLICTS_WITH: '#ff6363',
  SUPERSEDES: '#f1fa8c',
  ENABLES: '#50fa7b',
  BREAKS: '#ff6363',
  BELONGS_TO: '#ff6ac1',
  DEPENDS_ON: '#80ffea',
  BLOCKS: '#ff6363',
  REFERENCES: '#6b6580',
  MENTIONS: '#6b6580',
  DEFAULT: '#4a4560',
};

interface GraphData {
  nodes: Array<{
    id: string;
    label?: string;
    type?: string;
    color?: string;
    size?: number;
  }>;
  edges: Array<{
    id: string;
    source: string;
    target: string;
    type?: string;
    weight?: number;
  }>;
}

// Force graph node/link types
interface ForceNode {
  id: string;
  label: string;
  color: string;
  size: number;
  type?: string;
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
}

interface ForceLink {
  source: string | ForceNode;
  target: string | ForceNode;
  label?: string;
  color: string;
  width: number;
}

export interface KnowledgeGraphRef {
  zoomIn: () => void;
  zoomOut: () => void;
  fitView: () => void;
  resetView: () => void;
  centerOnNode: (nodeId: string) => void;
}

interface KnowledgeGraphProps {
  data: GraphData | null;
  onNodeClick?: (nodeId: string) => void;
  selectedNodeId?: string | null;
  searchTerm?: string;
}

export const KnowledgeGraph = forwardRef<KnowledgeGraphRef, KnowledgeGraphProps>(
  function KnowledgeGraph({ data, onNodeClick, selectedNodeId, searchTerm }, ref) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const graphRef = useRef<any>(null);
    const containerRef = useRef<HTMLDivElement>(null);

    // Expose control methods to parent
    useImperativeHandle(ref, () => ({
      zoomIn: () => {
        if (graphRef.current) {
          const currentZoom = graphRef.current.zoom();
          graphRef.current.zoom(currentZoom * 1.5, 300);
        }
      },
      zoomOut: () => {
        if (graphRef.current) {
          const currentZoom = graphRef.current.zoom();
          graphRef.current.zoom(currentZoom / 1.5, 300);
        }
      },
      fitView: () => {
        graphRef.current?.zoomToFit(400, 50);
      },
      resetView: () => {
        graphRef.current?.zoomToFit(400, 50);
        graphRef.current?.centerAt(0, 0, 300);
      },
      centerOnNode: (nodeId: string) => {
        const gd = graphRef.current?.graphData();
        const node = gd?.nodes.find((n: ForceNode) => n.id === nodeId);
        if (node && node.x !== undefined && node.y !== undefined) {
          graphRef.current?.centerAt(node.x, node.y, 500);
          graphRef.current?.zoom(2, 500);
        }
      },
    }));

    // Transform data for force-graph format
    const graphData = useMemo(() => {
      if (!data) return { nodes: [] as ForceNode[], links: [] as ForceLink[] };

      const searchLower = searchTerm?.toLowerCase() || '';
      const seenNodeIds = new Set<string>();

      const nodes: ForceNode[] = data.nodes
        .filter(node => {
          if (!node.id) return false;
          if (seenNodeIds.has(node.id)) return false;
          seenNodeIds.add(node.id);
          return true;
        })
        .map(node => {
          const isHighlighted = searchLower && node.label?.toLowerCase().includes(searchLower);
          const isSelected = node.id === selectedNodeId;
          const baseColor =
            node.color ||
            ENTITY_COLORS[node.type as keyof typeof ENTITY_COLORS] ||
            DEFAULT_NODE_COLOR;

          const nodeSize = typeof node.size === 'number' && node.size > 0 ? node.size : 5;
          // Much larger nodes for visibility
          let displaySize = Math.max(8, Math.min(24, nodeSize * 4));
          if (isHighlighted) displaySize = 28;
          if (isSelected) displaySize = 32;

          return {
            id: node.id,
            label: node.label || node.id.slice(0, 8),
            color: isSelected ? '#ffffff' : baseColor,
            size: displaySize,
            type: node.type,
          };
        });

      const seenEdgeIds = new Set<string>();
      const links: ForceLink[] = data.edges
        .filter(edge => {
          if (!edge.id || !edge.source || !edge.target) return false;
          if (!seenNodeIds.has(edge.source) || !seenNodeIds.has(edge.target)) return false;
          if (seenEdgeIds.has(edge.id)) return false;
          seenEdgeIds.add(edge.id);
          return true;
        })
        .map(edge => ({
          source: edge.source,
          target: edge.target,
          label: edge.type?.replace(/_/g, ' ').toLowerCase(),
          color: EDGE_COLORS[edge.type || ''] || EDGE_COLORS.DEFAULT,
          width: Math.max(1, (edge.weight || 1) * 0.8),
        }));

      return { nodes, links };
    }, [data, searchTerm, selectedNodeId]);

    // Custom node rendering with glow effect
    const paintNode = useCallback((node: ForceNode, ctx: CanvasRenderingContext2D) => {
      const size = node.size || 6;
      const x = node.x || 0;
      const y = node.y || 0;
      const isSelected = node.id === selectedNodeId;
      const isHighlighted = searchTerm && node.label?.toLowerCase().includes(searchTerm.toLowerCase());

      // Glow effect for selected/highlighted
      if (isSelected || isHighlighted) {
        ctx.beginPath();
        ctx.arc(x, y, size + 4, 0, 2 * Math.PI);
        ctx.fillStyle = isSelected ? 'rgba(225, 53, 255, 0.4)' : 'rgba(128, 255, 234, 0.3)';
        ctx.fill();
      }

      // Main node circle
      ctx.beginPath();
      ctx.arc(x, y, size, 0, 2 * Math.PI);
      ctx.fillStyle = node.color;
      ctx.fill();

      // Border
      ctx.strokeStyle = isSelected ? '#e135ff' : 'rgba(255, 255, 255, 0.2)';
      ctx.lineWidth = isSelected ? 2 : 0.5;
      ctx.stroke();

      // Label
      const fontSize = Math.max(3, size * 0.5);
      ctx.font = `${fontSize}px "Space Grotesk", sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = '#a8a3b8';
      ctx.fillText(node.label, x, y + size + 2);
    }, [selectedNodeId, searchTerm]);

    // Custom link rendering with arrows
    const paintLink = useCallback((link: ForceLink, ctx: CanvasRenderingContext2D) => {
      const source = link.source as ForceNode;
      const target = link.target as ForceNode;
      if (!source.x || !source.y || !target.x || !target.y) return;

      ctx.beginPath();
      ctx.moveTo(source.x, source.y);
      ctx.lineTo(target.x, target.y);
      ctx.strokeStyle = link.color;
      ctx.lineWidth = link.width;
      ctx.globalAlpha = 0.6;
      ctx.stroke();
      ctx.globalAlpha = 1;

      // Arrow head
      const angle = Math.atan2(target.y - source.y, target.x - source.x);
      const targetSize = target.size || 6;
      const arrowX = target.x - Math.cos(angle) * (targetSize + 3);
      const arrowY = target.y - Math.sin(angle) * (targetSize + 3);
      const arrowSize = 4;

      ctx.beginPath();
      ctx.moveTo(arrowX, arrowY);
      ctx.lineTo(
        arrowX - arrowSize * Math.cos(angle - Math.PI / 6),
        arrowY - arrowSize * Math.sin(angle - Math.PI / 6)
      );
      ctx.lineTo(
        arrowX - arrowSize * Math.cos(angle + Math.PI / 6),
        arrowY - arrowSize * Math.sin(angle + Math.PI / 6)
      );
      ctx.closePath();
      ctx.fillStyle = link.color;
      ctx.globalAlpha = 0.8;
      ctx.fill();
      ctx.globalAlpha = 1;
    }, []);

    const handleNodeClick = useCallback((node: ForceNode) => {
      onNodeClick?.(node.id);
    }, [onNodeClick]);

    const handleNodeDragEnd = useCallback((node: ForceNode) => {
      // Fix node position after drag
      node.fx = node.x;
      node.fy = node.y;
    }, []);

    if (!data || data.nodes.length === 0) {
      return (
        <div className="flex items-center justify-center h-full bg-sc-bg-dark">
          <EmptyState
            variant="data"
            title="No entities to display"
            description="Adjust filters or add more data to the knowledge graph"
          />
        </div>
      );
    }

    return (
      <div ref={containerRef} className="relative w-full h-full bg-[#0a0812]" style={{ minHeight: '400px' }}>
        <ForceGraph2D
          ref={graphRef as any}
          graphData={graphData as any}
          nodeCanvasObject={paintNode as any}
          nodeCanvasObjectMode={() => 'replace'}
          linkCanvasObject={paintLink as any}
          linkCanvasObjectMode={() => 'replace'}
          nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
            ctx.beginPath();
            ctx.arc(node.x || 0, node.y || 0, (node.size || 6) + 4, 0, 2 * Math.PI);
            ctx.fillStyle = color;
            ctx.fill();
          }}
          onNodeClick={handleNodeClick as any}
          onNodeDragEnd={handleNodeDragEnd as any}
          cooldownTicks={100}
          warmupTicks={50}
          backgroundColor="#0a0812"
          enableZoomInteraction={true}
          enablePanInteraction={true}
          enableNodeDrag={true}
          minZoom={0.1}
          maxZoom={10}
          linkDirectionalArrowLength={0}
          d3AlphaDecay={0.02}
          d3VelocityDecay={0.3}
        />
      </div>
    );
  }
);
