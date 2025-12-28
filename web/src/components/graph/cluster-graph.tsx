'use client';

import * as d3Force from 'd3-force';
import dynamic from 'next/dynamic';
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef } from 'react';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import { GraphEmptyState } from '@/components/ui/empty-state';
import { ENTITY_COLORS, type EntityType } from '@/lib/constants';
import { useTheme } from '@/lib/theme';

// Dynamic import to avoid SSR issues with canvas
const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-dark">
      <div className="text-sc-fg-muted">Loading clusters...</div>
    </div>
  ),
});

export interface Cluster {
  id: string;
  count: number;
  dominant_type: string;
  type_distribution: Record<string, number>;
  level: number;
}

interface ClusterNode {
  id: string;
  count: number;
  type: string;
  color: string;
  size: number;
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
}

export interface ClusterGraphRef {
  zoomIn: () => void;
  zoomOut: () => void;
  fitView: () => void;
  resetView: () => void;
}

interface ClusterGraphProps {
  clusters: Cluster[];
  onClusterClick: (clusterId: string) => void;
  isLoading?: boolean;
}

const DEFAULT_COLOR = '#8b85a0';

// Canvas requires hex colors - OKLCH CSS vars don't work directly
const CANVAS_COLORS = {
  neon: { bg: '#0a0812', fgPrimary: '#fafaf5', fgMuted: '#9b93b8' },
  dawn: { bg: '#f1ecff', fgPrimary: '#2b2540', fgMuted: '#8e84a8' },
};

export const ClusterGraph = forwardRef<ClusterGraphRef, ClusterGraphProps>(function ClusterGraph(
  { clusters, onClusterClick, isLoading },
  ref
) {
  const { theme } = useTheme();
  const colors = CANVAS_COLORS[theme];
  const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);

  // Configure d3 forces for bubble layout
  useEffect(() => {
    if (!graphRef.current) return;

    // Charge force - bubbles repel each other
    graphRef.current.d3Force('charge', d3Force.forceManyBody().strength(-200));

    // Center force - keep bubbles centered
    graphRef.current.d3Force('center', d3Force.forceCenter().strength(0.1));

    // Collision force - prevent bubble overlap based on size
    // Cast inside callback to access custom size property while keeping d3's expected signature
    graphRef.current.d3Force(
      'collision',
      d3Force.forceCollide().radius(node => ((node as ClusterNode).size || 20) + 8)
    );

    // Remove link force (no connections between clusters)
    graphRef.current.d3Force('link', null);
  }, []);

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
      graphRef.current?.zoomToFit(400, 80);
    },
    resetView: () => {
      graphRef.current?.zoomToFit(400, 80);
      graphRef.current?.centerAt(0, 0, 300);
    },
  }));

  // Transform clusters to graph nodes
  const graphData = useMemo(() => {
    if (!clusters || clusters.length === 0) return { nodes: [] as ClusterNode[], links: [] };

    // Calculate size based on member count (log scale for better visibility)
    const maxCount = Math.max(...clusters.map(c => c.count), 1);

    const nodes: ClusterNode[] = clusters.map(cluster => {
      const type = cluster.dominant_type;
      const color = ENTITY_COLORS[type as EntityType] || DEFAULT_COLOR;

      // Size: 20 (min) to 80 (max) based on log scale
      const logCount = Math.log2(cluster.count + 1);
      const logMax = Math.log2(maxCount + 1);
      const size = 20 + (logCount / logMax) * 60;

      return {
        id: cluster.id,
        count: cluster.count,
        type: type,
        color: color,
        size: size,
      };
    });

    return { nodes, links: [] };
  }, [clusters]);

  // Custom bubble rendering with radial gradient
  const paintNode = useCallback(
    (node: ClusterNode, ctx: CanvasRenderingContext2D) => {
      const size = node.size || 30;
      const x = node.x || 0;
      const y = node.y || 0;

      // Outer glow
      ctx.beginPath();
      ctx.arc(x, y, size + 6, 0, 2 * Math.PI);
      ctx.fillStyle = `${node.color}22`;
      ctx.fill();

      // Create radial gradient for bubble effect
      const gradient = ctx.createRadialGradient(x - size / 3, y - size / 3, 0, x, y, size);
      gradient.addColorStop(0, `${node.color}ee`);
      gradient.addColorStop(0.7, `${node.color}aa`);
      gradient.addColorStop(1, `${node.color}66`);

      // Main bubble
      ctx.beginPath();
      ctx.arc(x, y, size, 0, 2 * Math.PI);
      ctx.fillStyle = gradient;
      ctx.fill();

      // Subtle border
      ctx.strokeStyle = `${node.color}55`;
      ctx.lineWidth = 1;
      ctx.stroke();

      // Count label (center)
      const countFontSize = Math.max(12, size * 0.4);
      ctx.font = `bold ${countFontSize}px "JetBrains Mono", monospace`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = colors.fgPrimary;
      ctx.fillText(node.count.toString(), x, y);

      // Type label (below bubble)
      if (size > 25) {
        const typeFontSize = Math.max(8, size * 0.2);
        ctx.font = `${typeFontSize}px "Space Grotesk", sans-serif`;
        ctx.fillStyle = colors.fgMuted;
        const typeLabel = node.type?.replace(/_/g, ' ') || 'unknown';
        ctx.fillText(typeLabel, x, y + size + 10);
      }
    },
    [colors]
  );

  const handleNodeClick = useCallback(
    (node: ClusterNode) => {
      if (node.id) {
        onClusterClick(String(node.id));
      }
    },
    [onClusterClick]
  );

  const paintPointerArea = useCallback(
    (node: ClusterNode, color: string, ctx: CanvasRenderingContext2D) => {
      ctx.beginPath();
      ctx.arc(node.x || 0, node.y || 0, (node.size || 30) + 5, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    []
  );

  if (isLoading) {
    return (
      <div
        className="flex items-center justify-center h-full"
        style={{ backgroundColor: colors.bg }}
      >
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-sc-purple border-t-transparent rounded-full animate-spin" />
          <div className="text-sc-fg-muted text-sm">Detecting communities...</div>
        </div>
      </div>
    );
  }

  if (!clusters || clusters.length === 0) {
    return (
      <div
        className="flex items-center justify-center h-full"
        style={{ backgroundColor: colors.bg }}
      >
        <GraphEmptyState />
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative w-full h-full"
      style={{ minHeight: '400px', backgroundColor: colors.bg }}
    >
      {/* Key forces re-render when theme changes */}
      <ForceGraph2D
        key={theme}
        ref={graphRef as React.MutableRefObject<ForceGraphMethods | undefined>}
        graphData={graphData as { nodes: object[]; links: object[] }}
        nodeCanvasObject={
          paintNode as (node: object, ctx: CanvasRenderingContext2D, globalScale: number) => void
        }
        nodeCanvasObjectMode={() => 'replace'}
        nodePointerAreaPaint={
          paintPointerArea as (node: object, color: string, ctx: CanvasRenderingContext2D) => void
        }
        onNodeClick={handleNodeClick as (node: object, event: MouseEvent) => void}
        cooldownTicks={150}
        warmupTicks={50}
        backgroundColor={colors.bg}
        enableZoomInteraction={true}
        enablePanInteraction={true}
        enableNodeDrag={true}
        minZoom={0.2}
        maxZoom={5}
        d3AlphaDecay={0.02}
        d3VelocityDecay={0.3}
      />

      {/* Cluster count overlay */}
      <div className="absolute bottom-4 left-4 bg-sc-bg-elevated/90 backdrop-blur-sm rounded-lg px-3 py-2 border border-sc-purple/20">
        <div className="text-xs text-sc-fg-muted">
          {clusters.length} cluster{clusters.length !== 1 ? 's' : ''}
        </div>
      </div>
    </div>
  );
});
