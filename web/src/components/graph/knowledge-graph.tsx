'use client';

import * as d3Force from 'd3-force';
import dynamic from 'next/dynamic';
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import { GraphEmptyState } from '@/components/ui/empty-state';
import { ENTITY_COLORS, GRAPH_DEFAULTS } from '@/lib/constants';
import { useTheme } from '@/lib/theme';

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

// Graph node with our custom properties
interface GraphNode {
  id: string;
  label: string;
  color: string;
  size: number;
  type?: string;
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
}

// Graph link with our custom properties
interface GraphLink {
  source: string | GraphNode;
  target: string | GraphNode;
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

// Canvas requires hex colors - OKLCH CSS vars don't work directly
const CANVAS_COLORS = {
  neon: { bg: '#0a0812', fgPrimary: '#fafaf5', fgMuted: '#9b93b8', fgSubtle: '#5a5478' },
  dawn: { bg: '#f1ecff', fgPrimary: '#2b2540', fgMuted: '#8e84a8', fgSubtle: '#b8b0cc' },
};

export const KnowledgeGraph = forwardRef<KnowledgeGraphRef, KnowledgeGraphProps>(
  function KnowledgeGraph({ data, onNodeClick, selectedNodeId, searchTerm }, ref) {
    const { theme } = useTheme();
    const colors = CANVAS_COLORS[theme];

    // Using ForceGraphMethods with default generics since library types don't properly support custom types
    const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
    const containerRef = useRef<HTMLDivElement>(null);
    const [hasInitialFit, setHasInitialFit] = useState(false);
    const prevDataRef = useRef<GraphData | null>(null);

    // Calculate node degrees (connection count) for sizing
    const nodeDegrees = useMemo(() => {
      if (!data) return new Map<string, number>();
      const degrees = new Map<string, number>();
      for (const edge of data.edges) {
        degrees.set(edge.source, (degrees.get(edge.source) || 0) + 1);
        degrees.set(edge.target, (degrees.get(edge.target) || 0) + 1);
      }
      return degrees;
    }, [data]);

    // Configure d3 forces for better layout
    useEffect(() => {
      if (!graphRef.current) return;

      // Charge force - nodes repel each other
      graphRef.current.d3Force(
        'charge',
        d3Force.forceManyBody().strength(GRAPH_DEFAULTS.CHARGE_STRENGTH)
      );

      // Link force - connected nodes attract
      graphRef.current.d3Force(
        'link',
        d3Force.forceLink().distance(GRAPH_DEFAULTS.LINK_DISTANCE).strength(0.5)
      );

      // Center force - keep graph centered
      graphRef.current.d3Force(
        'center',
        d3Force.forceCenter().strength(GRAPH_DEFAULTS.CENTER_STRENGTH)
      );

      // Collision force - prevent node overlap
      graphRef.current.d3Force(
        'collision',
        d3Force.forceCollide().radius(GRAPH_DEFAULTS.COLLISION_RADIUS)
      );
    }, []);

    // Reset fit state when data changes significantly
    useEffect(() => {
      if (data && prevDataRef.current !== data) {
        const prevNodeCount = prevDataRef.current?.nodes.length || 0;
        const newNodeCount = data.nodes.length;
        // Reset if node count changed significantly
        if (Math.abs(newNodeCount - prevNodeCount) > 5 || prevNodeCount === 0) {
          setHasInitialFit(false);
        }
        prevDataRef.current = data;
      }
    }, [data]);

    // Auto-fit on simulation stop (only once per data load)
    const handleEngineStop = useCallback(() => {
      if (!hasInitialFit && graphRef.current) {
        graphRef.current.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
        setHasInitialFit(true);
      }
    }, [hasInitialFit]);

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
        // Access graph data through the d3 force simulation
        const linkForce = graphRef.current?.d3Force('link');
        if (!linkForce) return;

        // The links contain references to nodes after simulation
        const links = (linkForce as { links?: () => GraphLink[] }).links?.();
        if (!links || links.length === 0) return;

        // Find node in the source/target of any link
        for (const link of links) {
          const source = link.source as GraphNode;
          const target = link.target as GraphNode;

          const node = source.id === nodeId ? source : target.id === nodeId ? target : null;
          if (node && node.x !== undefined && node.y !== undefined) {
            graphRef.current?.centerAt(node.x, node.y, 500);
            graphRef.current?.zoom(2, 500);
            return;
          }
        }
      },
    }));

    // Transform data for force-graph format
    const graphData = useMemo(() => {
      if (!data) return { nodes: [] as GraphNode[], links: [] as GraphLink[] };

      const searchLower = searchTerm?.toLowerCase() || '';
      const seenNodeIds = new Set<string>();

      // Find max degree for normalization
      const maxDegree = Math.max(1, ...Array.from(nodeDegrees.values()));

      const nodes: GraphNode[] = data.nodes
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

          // Size based on connection count (degree)
          const degree = nodeDegrees.get(node.id) || 0;
          const normalizedDegree = degree / maxDegree;
          // Scale from MIN to MAX based on degree, with sqrt for less extreme differences
          let displaySize =
            GRAPH_DEFAULTS.NODE_SIZE_MIN +
            Math.sqrt(normalizedDegree) *
              (GRAPH_DEFAULTS.NODE_SIZE_MAX - GRAPH_DEFAULTS.NODE_SIZE_MIN);

          // Override for highlighted/selected states
          if (isHighlighted) displaySize = GRAPH_DEFAULTS.NODE_SIZE_HIGHLIGHTED;
          if (isSelected) displaySize = GRAPH_DEFAULTS.NODE_SIZE_SELECTED;

          return {
            id: node.id,
            label: node.label || node.id.slice(0, 8),
            color: isSelected ? '#ffffff' : baseColor,
            size: displaySize,
            type: node.type,
          };
        });

      const seenEdgeIds = new Set<string>();
      const links: GraphLink[] = data.edges
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
    }, [data, searchTerm, selectedNodeId, nodeDegrees]);

    // Custom node rendering with glow effect
    const paintNode = useCallback(
      (node: GraphNode, ctx: CanvasRenderingContext2D) => {
        const size = node.size || 4;
        const x = node.x || 0;
        const y = node.y || 0;
        const isSelected = node.id === selectedNodeId;
        const isHighlighted =
          searchTerm && node.label?.toLowerCase().includes(searchTerm.toLowerCase());

        // Glow effect for selected/highlighted
        if (isSelected || isHighlighted) {
          ctx.beginPath();
          ctx.arc(x, y, size + 2, 0, 2 * Math.PI);
          ctx.fillStyle = isSelected ? 'rgba(225, 53, 255, 0.4)' : 'rgba(128, 255, 234, 0.3)';
          ctx.fill();
        }

        // Main node circle
        ctx.beginPath();
        ctx.arc(x, y, size, 0, 2 * Math.PI);
        ctx.fillStyle = node.color;
        ctx.fill();

        // Border
        ctx.strokeStyle = isSelected ? '#e135ff' : colors.fgSubtle;
        ctx.lineWidth = isSelected ? 1.5 : 0.3;
        ctx.stroke();

        // Label - smaller and truncated
        const fontSize = Math.max(
          GRAPH_DEFAULTS.LABEL_SIZE_MIN,
          Math.min(GRAPH_DEFAULTS.LABEL_SIZE_MAX, size * 0.4)
        );
        ctx.font = `${fontSize}px "Space Grotesk", sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillStyle = isSelected ? colors.fgPrimary : colors.fgMuted;
        // Truncate long labels
        const maxLabelLen = isSelected ? 30 : 20;
        const label =
          node.label.length > maxLabelLen ? `${node.label.slice(0, maxLabelLen)}…` : node.label;
        ctx.fillText(label, x, y + size + 1);
      },
      [selectedNodeId, searchTerm, colors]
    );

    // Custom link rendering with arrows
    const paintLink = useCallback((link: GraphLink, ctx: CanvasRenderingContext2D) => {
      const source = link.source as GraphNode;
      const target = link.target as GraphNode;
      if (!source.x || !source.y || !target.x || !target.y) return;

      ctx.beginPath();
      ctx.moveTo(source.x, source.y);
      ctx.lineTo(target.x, target.y);
      ctx.strokeStyle = link.color;
      ctx.lineWidth = link.width * 0.6;
      ctx.globalAlpha = 0.5;
      ctx.stroke();
      ctx.globalAlpha = 1;

      // Arrow head
      const angle = Math.atan2(target.y - source.y, target.x - source.x);
      const targetSize = target.size || 4;
      const arrowX = target.x - Math.cos(angle) * (targetSize + 2);
      const arrowY = target.y - Math.sin(angle) * (targetSize + 2);
      const arrowSize = 2.5;

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

    const handleNodeClick = useCallback(
      (node: GraphNode) => {
        if (node.id) {
          onNodeClick?.(String(node.id));
        }
      },
      [onNodeClick]
    );

    const handleNodeDragEnd = useCallback((node: GraphNode) => {
      // Fix node position after drag
      node.fx = node.x;
      node.fy = node.y;
    }, []);

    const paintPointerArea = useCallback(
      (node: GraphNode, color: string, ctx: CanvasRenderingContext2D) => {
        ctx.beginPath();
        // Slightly larger hit area for easier clicking
        ctx.arc(node.x || 0, node.y || 0, (node.size || 4) + 3, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.fill();
      },
      []
    );

    if (!data || data.nodes.length === 0) {
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
        {/* Cast callbacks to any since react-force-graph-2d types don't properly support custom node/link types */}
        {/* Key forces re-render when theme changes since ForceGraph2D doesn't react to backgroundColor prop changes */}
        <ForceGraph2D
          key={theme}
          ref={graphRef as React.MutableRefObject<ForceGraphMethods | undefined>}
          graphData={graphData as { nodes: object[]; links: object[] }}
          nodeCanvasObject={
            paintNode as (node: object, ctx: CanvasRenderingContext2D, globalScale: number) => void
          }
          nodeCanvasObjectMode={() => 'replace'}
          linkCanvasObject={
            paintLink as (link: object, ctx: CanvasRenderingContext2D, globalScale: number) => void
          }
          linkCanvasObjectMode={() => 'replace'}
          nodePointerAreaPaint={
            paintPointerArea as (node: object, color: string, ctx: CanvasRenderingContext2D) => void
          }
          onNodeClick={handleNodeClick as (node: object, event: MouseEvent) => void}
          onNodeDragEnd={
            handleNodeDragEnd as (node: object, translate: { x: number; y: number }) => void
          }
          onEngineStop={handleEngineStop}
          cooldownTicks={GRAPH_DEFAULTS.COOLDOWN_TICKS}
          warmupTicks={GRAPH_DEFAULTS.WARMUP_TICKS}
          backgroundColor={colors.bg}
          enableZoomInteraction={true}
          enablePanInteraction={true}
          enableNodeDrag={true}
          minZoom={0.1}
          maxZoom={10}
          linkDirectionalArrowLength={0}
          d3AlphaDecay={GRAPH_DEFAULTS.ALPHA_DECAY}
          d3VelocityDecay={GRAPH_DEFAULTS.VELOCITY_DECAY}
        />
      </div>
    );
  }
);
