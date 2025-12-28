'use client';

import * as d3Force from 'd3-force';
import dynamic from 'next/dynamic';
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import { EntityDetailPanel } from '@/components/graph/entity-detail-panel';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { Card } from '@/components/ui/card';
import { GraphEmptyState } from '@/components/ui/empty-state';
import {
  ChevronDown,
  ChevronUp,
  Focus,
  Loader2,
  Maximize2,
  Minimize2,
  MinusCircle,
  PlusCircle,
  RotateCcw,
} from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import type { HierarchicalCluster, HierarchicalEdge, HierarchicalNode } from '@/lib/api';
import { GRAPH_DEFAULTS, getClusterColor, getEntityColor } from '@/lib/constants';
import { useHierarchicalGraph } from '@/lib/hooks';
import { useTheme } from '@/lib/theme';

// Canvas requires hex colors - OKLCH CSS vars don't work directly
const CANVAS_COLORS = {
  neon: { bg: '#0a0812', fgPrimary: '#fafaf5', fgMuted: '#9b93b8' },
  dawn: { bg: '#f1ecff', fgPrimary: '#2b2540', fgMuted: '#8e84a8' },
};

// Dynamic import to avoid SSR issues with canvas
const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-base">
      <div className="text-sc-fg-muted">Loading graph...</div>
    </div>
  ),
});

// Extended node type for force graph
interface GraphNode extends HierarchicalNode {
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
  clusterColor?: string;
  entityColor?: string; // Color based on entity type
  degree?: number; // Number of connections (for sizing)
  isProject?: boolean; // Projects are STARS in our galaxy!
  zIndex?: number; // Render order (higher = on top, gets label priority)
  __highlightTime?: number; // For pulse animation
}

// d3-force mutates source/target from string IDs to node objects at runtime
interface GraphLink extends Omit<HierarchicalEdge, 'source' | 'target'> {
  source: string | GraphNode;
  target: string | GraphNode;
  sourceNode?: GraphNode;
  targetNode?: GraphNode;
}

export interface KnowledgeGraphRef {
  zoomIn: () => void;
  zoomOut: () => void;
  fitView: () => void;
  resetView: () => void;
}

// Mobile bottom sheet for entity details
function MobileEntitySheet({ entityId, onClose }: { entityId: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 md:hidden">
      <button
        type="button"
        className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default"
        onClick={onClose}
        onKeyDown={e => e.key === 'Escape' && onClose()}
        aria-label="Close panel"
      />
      <div className="absolute bottom-0 left-0 right-0 max-h-[70vh] bg-sc-bg-base rounded-t-2xl overflow-hidden animate-slide-up">
        <div className="flex justify-center py-2">
          <div className="w-10 h-1 bg-sc-fg-subtle/30 rounded-full" />
        </div>
        <EntityDetailPanel entityId={entityId} onClose={onClose} variant="sheet" />
      </div>
    </div>
  );
}

// Generate a descriptive label for a cluster from its top nodes
function getClusterLabel(cluster: HierarchicalCluster, nodes: GraphNode[]): string {
  // Find nodes belonging to this cluster, sorted by degree (most connected first)
  const clusterNodes = nodes
    .filter(n => n.cluster_id === cluster.id)
    .sort((a, b) => (b.degree || 0) - (a.degree || 0));

  if (clusterNodes.length === 0) {
    return cluster.dominant_type?.replace(/_/g, ' ') || 'Mixed';
  }

  // Get top 2 node names as the cluster label
  const topNames = clusterNodes
    .slice(0, 2)
    .map(n => {
      const name = n.label || n.name || '';
      // Truncate long names
      return name.length > 15 ? `${name.slice(0, 12)}...` : name;
    })
    .filter(Boolean);

  if (topNames.length === 0) {
    return cluster.dominant_type?.replace(/_/g, ' ') || 'Mixed';
  }

  return topNames.join(', ');
}

// Cluster legend component
function ClusterLegend({
  clusters,
  clusterColorMap,
  selectedCluster,
  onClusterClick,
  nodes,
}: {
  clusters: HierarchicalCluster[];
  clusterColorMap: Map<string, string>;
  selectedCluster: string | null;
  onClusterClick: (clusterId: string | null) => void;
  nodes: GraphNode[];
}) {
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
            .sort((a, b) => b.member_count - a.member_count)
            .map(cluster => {
              const color = clusterColorMap.get(cluster.id) || '#8b85a0';
              const isSelected = selectedCluster === cluster.id;
              const label = getClusterLabel(cluster, nodes);
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
                    {cluster.member_count}
                  </span>
                </button>
              );
            })}
        </div>
      )}
    </Card>
  );
}

// Stats overlay - shows real totals and displayed counts
function StatsOverlay({
  totalNodes,
  totalEdges,
  displayedNodes,
  displayedEdges,
  clusterCount,
}: {
  totalNodes: number;
  totalEdges: number;
  displayedNodes: number;
  displayedEdges: number;
  clusterCount: number;
}) {
  const showingAll = displayedNodes >= totalNodes;

  return (
    <div className="absolute top-4 right-4 z-10 bg-sc-bg-elevated/90 backdrop-blur-sm rounded-xl p-4 border border-sc-purple/20 min-w-[140px]">
      <div className="text-sm text-sc-fg-muted">Total Nodes</div>
      <div className="text-2xl font-bold text-sc-purple">{totalNodes.toLocaleString()}</div>
      {!showingAll && (
        <div className="text-xs text-sc-fg-subtle">showing {displayedNodes.toLocaleString()}</div>
      )}
      <div className="text-sm text-sc-fg-muted mt-2">Total Edges</div>
      <div className="text-2xl font-bold text-sc-cyan">{totalEdges.toLocaleString()}</div>
      {!showingAll && displayedEdges < totalEdges && (
        <div className="text-xs text-sc-fg-subtle">showing {displayedEdges.toLocaleString()}</div>
      )}
      <div className="text-sm text-sc-fg-muted mt-2">Clusters</div>
      <div className="text-2xl font-bold text-sc-coral">{clusterCount}</div>
    </div>
  );
}

function GraphToolbar({
  onZoomIn,
  onZoomOut,
  onFitView,
  onReset,
  isFullscreen,
  onToggleFullscreen,
  nodeCount,
  edgeCount,
}: {
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitView: () => void;
  onReset: () => void;
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
  nodeCount: number;
  edgeCount: number;
}) {
  return (
    <>
      {/* Mobile compact toolbar */}
      <div className="absolute top-2 left-2 right-2 z-10 flex items-center gap-2 md:hidden">
        {/* Stats pill */}
        <div className="flex-1 flex items-center justify-center gap-3 text-xs bg-sc-bg-base/90 rounded-lg px-3 py-2 border border-sc-fg-subtle/20">
          <span>
            <span className="text-sc-purple font-medium">{nodeCount}</span>
            <span className="text-sc-fg-subtle ml-1">nodes</span>
          </span>
          <span>
            <span className="text-sc-cyan font-medium">{edgeCount}</span>
            <span className="text-sc-fg-subtle ml-1">edges</span>
          </span>
        </div>

        {/* Fullscreen */}
        <button
          type="button"
          onClick={onToggleFullscreen}
          className="p-2.5 rounded-lg bg-sc-bg-base/90 text-sc-fg-subtle hover:text-sc-fg-primary border border-sc-fg-subtle/20 transition-colors"
        >
          {isFullscreen ? (
            <Minimize2 width={18} height={18} />
          ) : (
            <Maximize2 width={18} height={18} />
          )}
        </button>
      </div>

      {/* Desktop toolbar */}
      <div className="absolute top-4 left-4 z-10 hidden md:flex items-start gap-3">
        {/* Zoom Controls */}
        <Card className="!p-1 flex items-center gap-1">
          <button
            type="button"
            onClick={onZoomIn}
            className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
            title="Zoom in"
          >
            <PlusCircle width={16} height={16} />
          </button>
          <button
            type="button"
            onClick={onZoomOut}
            className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
            title="Zoom out"
          >
            <MinusCircle width={16} height={16} />
          </button>
          <div className="w-px h-4 bg-sc-fg-subtle/20" />
          <button
            type="button"
            onClick={onFitView}
            className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
            title="Fit to view"
          >
            <Focus width={16} height={16} />
          </button>
          <button
            type="button"
            onClick={onReset}
            className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
            title="Reset view"
          >
            <RotateCcw width={16} height={16} />
          </button>
          <div className="w-px h-4 bg-sc-fg-subtle/20" />
          <button
            type="button"
            onClick={onToggleFullscreen}
            className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
            title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          >
            {isFullscreen ? (
              <Minimize2 width={16} height={16} />
            ) : (
              <Maximize2 width={16} height={16} />
            )}
          </button>
        </Card>
      </div>

      {/* Mobile zoom controls (bottom) */}
      <div className="absolute bottom-4 right-4 z-10 flex md:hidden">
        <Card className="!p-1 flex items-center gap-1">
          <button
            type="button"
            onClick={onZoomOut}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <MinusCircle width={20} height={20} />
          </button>
          <button
            type="button"
            onClick={onFitView}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <Focus width={20} height={20} />
          </button>
          <button
            type="button"
            onClick={onZoomIn}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <PlusCircle width={20} height={20} />
          </button>
        </Card>
      </div>
    </>
  );
}

function GraphPageContent() {
  const { theme } = useTheme();
  const colors = CANVAS_COLORS[theme];
  const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  // Fetch hierarchical graph data with up to 1000 nodes
  const {
    data,
    isLoading,
    error: _error,
  } = useHierarchicalGraph({
    max_nodes: GRAPH_DEFAULTS.MAX_NODES,
    max_edges: GRAPH_DEFAULTS.MAX_EDGES,
  });

  // Build cluster color map
  const clusterColorMap = useMemo(() => {
    const map = new Map<string, string>();
    if (data?.clusters) {
      data.clusters.forEach((cluster, index) => {
        map.set(cluster.id, getClusterColor(cluster.id, index));
      });
    }
    return map;
  }, [data?.clusters]);

  // Calculate degree for ALL nodes (used by legend, unaffected by cluster filter)
  const allNodesWithDegree = useMemo(() => {
    if (!data) return [];

    const degreeMap = new Map<string, number>();
    for (const edge of data.edges) {
      degreeMap.set(edge.source, (degreeMap.get(edge.source) || 0) + 1);
      degreeMap.set(edge.target, (degreeMap.get(edge.target) || 0) + 1);
    }

    return data.nodes.map(node => ({
      ...node,
      degree: degreeMap.get(node.id) || 0,
    })) as GraphNode[];
  }, [data]);

  // Transform data for force graph with entity coloring and degree-based sizing
  const graphData = useMemo(() => {
    if (!data) return { nodes: [], links: [], maxDegree: 1 };

    // Filter by selected cluster if any
    let nodes = data.nodes;
    if (selectedCluster) {
      nodes = nodes.filter(n => n.cluster_id === selectedCluster);
    }

    const nodeIds = new Set(nodes.map(n => n.id));

    // Filter edges to only include those between visible nodes
    const filteredEdges = data.edges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));

    // Calculate degree (connection count) for each node
    const degreeMap = new Map<string, number>();
    for (const edge of filteredEdges) {
      degreeMap.set(edge.source, (degreeMap.get(edge.source) || 0) + 1);
      degreeMap.set(edge.target, (degreeMap.get(edge.target) || 0) + 1);
    }

    const maxDegree = Math.max(1, ...Array.from(degreeMap.values()));

    // Transform nodes with entity colors and degree
    const graphNodes: GraphNode[] = nodes.map(node => {
      const degree = degreeMap.get(node.id) || 0;
      const isProject = node.type === 'project';
      const entityType = node.type || 'unknown';

      // Calculate z-index for rendering order (higher = rendered later = on top)
      // Projects and high-degree nodes should be on top and get label priority
      let zIndex = degree; // Base: more connections = higher priority
      if (isProject) zIndex += 1000; // Projects always on top
      if (entityType === 'task') zIndex += 50; // Tasks are important
      if (entityType === 'pattern') zIndex += 30; // Patterns too

      return {
        ...node,
        clusterColor: clusterColorMap.get(node.cluster_id) || '#8b85a0',
        entityColor: getEntityColor(entityType),
        degree,
        isProject,
        zIndex,
      };
    });

    // Sort by zIndex so important nodes render last (on top) and get label priority
    graphNodes.sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));

    const graphLinks: GraphLink[] = filteredEdges.map(e => ({ ...e }));

    return { nodes: graphNodes, links: graphLinks, maxDegree };
  }, [data, selectedCluster, clusterColorMap]);

  // Configure d3 forces
  useEffect(() => {
    if (!graphRef.current) return;

    graphRef.current.d3Force(
      'charge',
      d3Force.forceManyBody().strength(GRAPH_DEFAULTS.CHARGE_STRENGTH)
    );
    graphRef.current.d3Force(
      'center',
      d3Force.forceCenter().strength(GRAPH_DEFAULTS.CENTER_STRENGTH)
    );
    graphRef.current.d3Force(
      'collision',
      d3Force.forceCollide().radius(GRAPH_DEFAULTS.COLLISION_RADIUS)
    );

    // Link force with distance - ForceFn has [key: string]: any so we can access distance directly
    const linkForce = graphRef.current.d3Force('link');
    if (linkForce && typeof linkForce.distance === 'function') {
      linkForce.distance(GRAPH_DEFAULTS.LINK_DISTANCE);
    }
  }, []);

  // Track drawn labels to avoid overlap - capped for performance
  const drawnLabelsRef = useRef<{ x: number; y: number; width: number }[]>([]);
  const maxLabelsToTrack = 50; // Limit overlap checks for performance

  // Clean node rendering - entity colors + degree-based sizing
  // Smart labels: always show for selected/hovered, projects, and high-connectivity hubs
  const paintNode = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = node.x || 0;
      const y = node.y || 0;
      const isSelected = node.id === selectedNodeId;
      const isHovered = node.id === hoveredNode;
      const isProject = node.isProject;
      const degree = node.degree || 0;
      const maxDegree = graphData.maxDegree || 1;

      // Size based on degree (connections) - more connections = bigger
      const degreeScale = Math.sqrt(degree / maxDegree);
      const logDegree = degree > 0 ? Math.log2(degree + 1) / Math.log2(maxDegree + 1) : 0;
      const combinedScale = (degreeScale + logDegree) / 2;

      let size: number;
      if (isProject) {
        // Projects are larger - 12px to 20px based on connections
        size = 12 + combinedScale * 8;
      } else if (isSelected) {
        size = Math.max(10, 5 + combinedScale * 8);
      } else if (isHovered) {
        size = Math.max(8, 4 + combinedScale * 7);
      } else {
        // Regular nodes: 3px (isolated) to 14px (hub nodes)
        size = 3 + combinedScale * 11;
      }

      // Use ENTITY COLOR - makes different types visually distinct
      const color = node.entityColor || '#8b85a0';

      // Simple glow for selected/hovered
      if (isSelected || isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, size + 4, 0, 2 * Math.PI);
        ctx.fillStyle = `${color}40`;
        ctx.fill();
      }

      // Main node - solid color, clean look
      ctx.beginPath();
      ctx.arc(x, y, size, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();

      // Border for selected
      if (isSelected) {
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();
      } else if (isHovered) {
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.5)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      // Label visibility based on node importance
      // Always show: selected, hovered, projects
      // Show at default zoom: hub nodes (top 5% by degree)
      // Show as zoom increases: progressively more nodes
      const isHubNode = degree > Math.max(3, maxDegree * 0.05); // Top 5% or 4+ connections

      let showLabel = isSelected || isHovered || isProject;

      // Hub nodes always show labels
      if (!showLabel && isHubNode) {
        showLabel = true;
      }
      // Nodes with 2+ connections show at slight zoom
      if (!showLabel && degree >= 2 && globalScale >= 1.0) {
        showLabel = true;
      }
      // Any connected node shows at moderate zoom
      if (!showLabel && degree >= 1 && globalScale >= 1.5) {
        showLabel = true;
      }

      if (showLabel) {
        const label = node.label || node.name || node.id.slice(0, 8);
        const displayLabel = label.length > 20 ? `${label.slice(0, 17)}...` : label;
        const fontSize = Math.max(8, Math.min(12, 10 / globalScale));

        ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
        const textWidth = ctx.measureText(displayLabel).width;

        const labelX = x;
        const labelY = y + size + 3;

        // Priority labels always draw (bypass overlap check)
        const isPriority = isSelected || isHovered || isProject || isHubNode;
        let shouldDraw = isPriority;

        if (!isPriority) {
          // Performance: skip overlap check if we've hit max labels
          // This prevents O(n²) checks during pan/zoom
          if (drawnLabelsRef.current.length >= maxLabelsToTrack) {
            shouldDraw = false;
          } else {
            // Fast overlap check - only check recent labels (last 20)
            const recentLabels = drawnLabelsRef.current.slice(-20);
            const minSpacing = 25 / globalScale;
            const overlaps = recentLabels.some(existing => {
              const dx = Math.abs(labelX - existing.x);
              const dy = Math.abs(labelY - existing.y);
              return dx < (textWidth + existing.width) / 2 + minSpacing && dy < fontSize + 4;
            });
            shouldDraw = !overlaps;
          }
        }

        if (shouldDraw) {
          ctx.textAlign = 'center';
          ctx.textBaseline = 'top';

          // Text shadow - inverted based on theme
          const shadowColor = theme === 'neon' ? 'rgba(0, 0, 0, 0.8)' : 'rgba(255, 255, 255, 0.8)';
          ctx.fillStyle = shadowColor;
          ctx.fillText(displayLabel, x + 0.5, labelY + 0.5);

          // Text color - use theme foreground
          const textColor = colors.fgPrimary;
          ctx.fillStyle = isSelected ? textColor : isProject ? `${textColor}ee` : `${textColor}cc`;
          ctx.fillText(displayLabel, x, labelY);

          // Track this label (only if under cap)
          if (drawnLabelsRef.current.length < maxLabelsToTrack) {
            drawnLabelsRef.current.push({ x: labelX, y: labelY, width: textWidth });
          }
        }
      }
    },
    [selectedNodeId, hoveredNode, graphData.maxDegree, theme, colors]
  );

  // Clear label tracking before each frame - runs once on mount
  useEffect(() => {
    const fg = graphRef.current;
    if (!fg) return;

    // Hook into the render cycle to clear labels before drawing
    // Access internal _renderFrame property (not in public types)
    type FGInternal = { _renderFrame?: (...args: unknown[]) => void };
    const fgInternal = fg as unknown as FGInternal;
    const originalRender = fgInternal._renderFrame;
    if (originalRender) {
      fgInternal._renderFrame = function (...args: unknown[]) {
        drawnLabelsRef.current = [];
        return originalRender.apply(this, args);
      };
    }
  }, []); // Only run once on mount

  // Clean link rendering
  const paintLink = useCallback(
    (link: GraphLink, ctx: CanvasRenderingContext2D) => {
      // After d3-force processes, source/target become node objects (not strings)
      const source = link.sourceNode || (typeof link.source === 'object' ? link.source : null);
      const target = link.targetNode || (typeof link.target === 'object' ? link.target : null);
      if (!source || !target) return;
      const sx = source.x;
      const sy = source.y;
      const tx = target.x;
      const ty = target.y;
      if (sx === undefined || sy === undefined || tx === undefined || ty === undefined) return;

      // Highlight links connected to selected/hovered node
      const isHighlighted =
        source.id === selectedNodeId ||
        target.id === selectedNodeId ||
        source.id === hoveredNode ||
        target.id === hoveredNode;

      ctx.beginPath();
      ctx.moveTo(sx, sy);
      ctx.lineTo(tx, ty);

      // Theme-aware link colors - stronger in light mode for visibility
      const linkColor = theme === 'neon' ? '#ffffff' : '#2b2540';
      if (isHighlighted) {
        ctx.strokeStyle = theme === 'neon' ? `${linkColor}50` : `${linkColor}70`;
        ctx.lineWidth = 1.5;
      } else {
        ctx.strokeStyle = theme === 'neon' ? `${linkColor}18` : `${linkColor}35`;
        ctx.lineWidth = theme === 'neon' ? 0.5 : 0.8;
      }
      ctx.stroke();
    },
    [selectedNodeId, hoveredNode, theme]
  );

  // Smooth zoom to node on click
  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      const isDeselecting = selectedNodeId === node.id;
      setSelectedNodeId(isDeselecting ? null : node.id);

      if (!isDeselecting && graphRef.current && node.x !== undefined && node.y !== undefined) {
        // Smooth zoom and center on the clicked node
        graphRef.current.centerAt(node.x, node.y, 800);
        // Zoom in for detail view (but not too close)
        const currentZoom = graphRef.current.zoom();
        if (currentZoom < 2.5) {
          graphRef.current.zoom(2.5, 800);
        }
      }
    },
    [selectedNodeId]
  );

  const handleClosePanel = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  const handleZoomIn = useCallback(() => {
    if (graphRef.current) {
      const currentZoom = graphRef.current.zoom();
      graphRef.current.zoom(currentZoom * 1.5, 300);
    }
  }, []);

  const handleZoomOut = useCallback(() => {
    if (graphRef.current) {
      const currentZoom = graphRef.current.zoom();
      graphRef.current.zoom(currentZoom / 1.5, 300);
    }
  }, []);

  const handleFitView = useCallback(() => {
    graphRef.current?.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
  }, []);

  const handleReset = useCallback(() => {
    graphRef.current?.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
    graphRef.current?.centerAt(0, 0, 300);
    setSelectedNodeId(null);
    setSelectedCluster(null);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) return;
    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  }, []);

  const nodeCount = graphData.nodes.length;
  const edgeCount = graphData.links.length;

  return (
    <div
      ref={containerRef}
      className={`flex flex-col ${isFullscreen ? 'fixed inset-0 z-50' : 'h-full'}`}
      style={isFullscreen ? { backgroundColor: colors.bg } : undefined}
      suppressHydrationWarning
    >
      {!isFullscreen && <Breadcrumb className="hidden md:flex" />}

      <div className="flex-1 flex gap-4 min-h-0 mt-0 md:mt-4">
        <div
          className="flex-1 relative md:rounded-xl md:border border-sc-fg-subtle/20 overflow-hidden"
          style={{ backgroundColor: colors.bg }}
          suppressHydrationWarning
        >
          <GraphToolbar
            onZoomIn={handleZoomIn}
            onZoomOut={handleZoomOut}
            onFitView={handleFitView}
            onReset={handleReset}
            isFullscreen={isFullscreen}
            onToggleFullscreen={toggleFullscreen}
            nodeCount={nodeCount}
            edgeCount={edgeCount}
          />

          {/* Loading overlay */}
          {isLoading && (
            <div
              className="absolute inset-0 flex items-center justify-center z-20"
              style={{ backgroundColor: `${colors.bg}cc` }}
              suppressHydrationWarning
            >
              <div className="flex items-center gap-3 text-sc-fg-muted">
                <Loader2 width={20} height={20} className="animate-spin text-sc-purple" />
                <span>Detecting communities & building graph...</span>
              </div>
            </div>
          )}

          {/* Empty state */}
          {!isLoading && graphData.nodes.length === 0 && (
            <div
              className="flex items-center justify-center h-full"
              style={{ backgroundColor: colors.bg }}
              suppressHydrationWarning
            >
              <GraphEmptyState />
            </div>
          )}

          {/* Graph - key forces re-render when theme changes */}
          {!isLoading && graphData.nodes.length > 0 && (
            <ForceGraph2D
              key={theme}
              ref={graphRef as React.MutableRefObject<ForceGraphMethods | undefined>}
              graphData={graphData as { nodes: object[]; links: object[] }}
              nodeCanvasObject={
                paintNode as (
                  node: object,
                  ctx: CanvasRenderingContext2D,
                  globalScale: number
                ) => void
              }
              nodeCanvasObjectMode={() => 'replace'}
              linkCanvasObject={
                paintLink as (
                  link: object,
                  ctx: CanvasRenderingContext2D,
                  globalScale: number
                ) => void
              }
              linkCanvasObjectMode={() => 'replace'}
              onNodeClick={handleNodeClick as (node: object, event: MouseEvent) => void}
              onNodeHover={node => setHoveredNode((node as GraphNode)?.id || null)}
              cooldownTicks={GRAPH_DEFAULTS.COOLDOWN_TICKS}
              warmupTicks={GRAPH_DEFAULTS.WARMUP_TICKS}
              backgroundColor={colors.bg}
              enableZoomInteraction={true}
              enablePanInteraction={true}
              enableNodeDrag={true}
              minZoom={0.1}
              maxZoom={10}
              d3AlphaDecay={GRAPH_DEFAULTS.ALPHA_DECAY}
              d3VelocityDecay={GRAPH_DEFAULTS.VELOCITY_DECAY}
            />
          )}

          {/* Stats overlay */}
          {data && (
            <StatsOverlay
              totalNodes={data.total_nodes}
              totalEdges={data.total_edges}
              displayedNodes={data.displayed_nodes ?? graphData.nodes.length}
              displayedEdges={data.displayed_edges ?? graphData.links.length}
              clusterCount={data.clusters.length}
            />
          )}

          {/* Cluster legend - bottom left */}
          {data && data.clusters.length > 0 && (
            <div className="absolute bottom-4 left-4 z-10 hidden md:block">
              <ClusterLegend
                clusters={data.clusters}
                clusterColorMap={clusterColorMap}
                selectedCluster={selectedCluster}
                onClusterClick={setSelectedCluster}
                nodes={allNodesWithDegree}
              />
            </div>
          )}

          {/* Keyboard hints - desktop only */}
          <div className="absolute bottom-4 right-4 z-10 text-xs text-sc-fg-subtle/50 hidden md:block">
            <kbd className="px-1.5 py-0.5 rounded bg-sc-bg-highlight/50 border border-sc-fg-subtle/20">
              scroll
            </kbd>{' '}
            zoom ·{' '}
            <kbd className="px-1.5 py-0.5 rounded bg-sc-bg-highlight/50 border border-sc-fg-subtle/20">
              drag
            </kbd>{' '}
            pan ·{' '}
            <kbd className="px-1.5 py-0.5 rounded bg-sc-bg-highlight/50 border border-sc-fg-subtle/20">
              click
            </kbd>{' '}
            select
          </div>
        </div>

        {/* Entity detail panel - desktop sidebar */}
        {selectedNodeId && (
          <div className="hidden md:block">
            <EntityDetailPanel entityId={selectedNodeId} onClose={handleClosePanel} />
          </div>
        )}
      </div>

      {/* Entity detail panel - mobile bottom sheet */}
      {selectedNodeId && <MobileEntitySheet entityId={selectedNodeId} onClose={handleClosePanel} />}
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
