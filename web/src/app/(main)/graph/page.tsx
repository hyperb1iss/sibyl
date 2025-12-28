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
import {
  CLUSTER_COLORS,
  DEFAULT_ENTITY_COLOR,
  ENTITY_COLORS,
  type ENTITY_TYPES,
  GRAPH_DEFAULTS,
  getClusterColor,
  getEntityColor,
} from '@/lib/constants';
import { useHierarchicalGraph, useStats } from '@/lib/hooks';

type EntityType = (typeof ENTITY_TYPES)[number];

// Dynamic import to avoid SSR issues with canvas
const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-dark">
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
  __highlightTime?: number; // For pulse animation
}

interface GraphLink extends HierarchicalEdge {
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

// Cluster legend component
function ClusterLegend({
  clusters,
  clusterColorMap,
  selectedCluster,
  onClusterClick,
}: {
  clusters: HierarchicalCluster[];
  clusterColorMap: Map<string, string>;
  selectedCluster: string | null;
  onClusterClick: (clusterId: string | null) => void;
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
          {clusters.map(cluster => {
            const color = clusterColorMap.get(cluster.id) || '#8b85a0';
            const isSelected = selectedCluster === cluster.id;
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
              >
                <div
                  className="w-2 h-2 rounded-full flex-shrink-0"
                  style={{ backgroundColor: color }}
                />
                <span className="truncate capitalize">
                  {cluster.dominant_type?.replace(/_/g, ' ') || 'Mixed'}
                </span>
                <span className="ml-auto text-sc-fg-subtle">{cluster.member_count}</span>
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
  const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  // Fetch hierarchical graph data with up to 1000 nodes
  const { data, isLoading, error } = useHierarchicalGraph({
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

      return {
        ...node,
        clusterColor: clusterColorMap.get(node.cluster_id) || '#8b85a0',
        entityColor: getEntityColor(entityType),
        degree,
        isProject,
      };
    });

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
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      d3Force.forceCollide<GraphNode>().radius(GRAPH_DEFAULTS.COLLISION_RADIUS) as any
    );

    // Link force with distance
    const linkForce = graphRef.current.d3Force('link');
    if (linkForce) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (linkForce as any).distance(GRAPH_DEFAULTS.LINK_DISTANCE);
    }
  }, []);

  // Track drawn labels to avoid overlap
  const drawnLabelsRef = useRef<{ x: number; y: number; width: number }[]>([]);

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

      // Smart label logic:
      // - Always show for selected/hovered (priority 1)
      // - Always show for projects (priority 2)
      // - Show for hub nodes (top 15% by connectivity) when zoomed in (priority 3)
      // - Show more labels as zoom increases
      const isHubNode = degree > maxDegree * 0.15; // Top 15% connectivity
      const zoomThreshold = isProject ? 1.0 : isHubNode ? 1.5 : 3.0;

      let showLabel = isSelected || isHovered;
      if (!showLabel && isProject && globalScale >= 1.0) {
        showLabel = true;
      }
      if (!showLabel && isHubNode && globalScale >= 1.5) {
        showLabel = true;
      }
      // Show more labels as we zoom in - for moderately connected nodes
      if (!showLabel && degree >= 3 && globalScale >= 3.0) {
        showLabel = true;
      }
      // At high zoom, show all labels with enough connections
      if (!showLabel && degree >= 1 && globalScale >= 5.0) {
        showLabel = true;
      }

      if (showLabel) {
        const label = node.label || node.name || node.id.slice(0, 8);
        const displayLabel = label.length > 20 ? `${label.slice(0, 17)}...` : label;
        const fontSize = Math.max(8, Math.min(12, 10 / globalScale));

        ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
        const textWidth = ctx.measureText(displayLabel).width;

        // Simple overlap check - skip if too close to another label
        const labelX = x;
        const labelY = y + size + 3;
        const minSpacing = 20 / globalScale; // Adjust spacing based on zoom

        // Only check overlap for non-priority labels
        const isPriority = isSelected || isHovered || isProject;
        let shouldDraw = isPriority;

        if (!isPriority) {
          // Check if this label would overlap with already drawn labels
          const overlaps = drawnLabelsRef.current.some(existing => {
            const dx = Math.abs(labelX - existing.x);
            const dy = Math.abs(labelY - existing.y);
            return dx < (textWidth + existing.width) / 2 + minSpacing && dy < fontSize + 4;
          });
          shouldDraw = !overlaps;
        }

        if (shouldDraw) {
          ctx.textAlign = 'center';
          ctx.textBaseline = 'top';

          // Text shadow
          ctx.fillStyle = 'rgba(0, 0, 0, 0.8)';
          ctx.fillText(displayLabel, x + 0.5, labelY + 0.5);

          ctx.fillStyle = isSelected ? '#ffffff' : isProject ? '#ffffffee' : '#ffffffcc';
          ctx.fillText(displayLabel, x, labelY);

          // Track this label position
          drawnLabelsRef.current.push({ x: labelX, y: labelY, width: textWidth });
        }
      }
    },
    [selectedNodeId, hoveredNode, graphData.maxDegree]
  );

  // Clear label tracking before each frame - runs once on mount
  useEffect(() => {
    const fg = graphRef.current;
    if (!fg) return;

    // Hook into the render cycle to clear labels before drawing
    const originalRender = (fg as any)._renderFrame;
    if (originalRender) {
      (fg as any)._renderFrame = function (...args: any[]) {
        drawnLabelsRef.current = [];
        return originalRender.apply(this, args);
      };
    }
  }, []); // Only run once on mount

  // Clean link rendering
  const paintLink = useCallback(
    (link: GraphLink, ctx: CanvasRenderingContext2D) => {
      const source = link.sourceNode || (link as any).source;
      const target = link.targetNode || (link as any).target;
      if (!source?.x || !target?.x) return;

      const sourceNode = source as GraphNode;
      const targetNode = target as GraphNode;

      // Highlight links connected to selected/hovered node
      const isHighlighted =
        sourceNode.id === selectedNodeId ||
        targetNode.id === selectedNodeId ||
        sourceNode.id === hoveredNode ||
        targetNode.id === hoveredNode;

      ctx.beginPath();
      ctx.moveTo(source.x, source.y);
      ctx.lineTo(target.x, target.y);

      if (isHighlighted) {
        ctx.strokeStyle = '#ffffff50';
        ctx.lineWidth = 1.5;
      } else {
        ctx.strokeStyle = '#ffffff12';
        ctx.lineWidth = 0.5;
      }
      ctx.stroke();
    },
    [selectedNodeId, hoveredNode]
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
      className={`flex flex-col ${isFullscreen ? 'fixed inset-0 z-50 bg-sc-bg-dark' : 'h-full'}`}
    >
      {!isFullscreen && <Breadcrumb className="hidden md:flex" />}

      <div className="flex-1 flex gap-4 min-h-0 mt-0 md:mt-4">
        <div className="flex-1 relative bg-sc-bg-dark md:rounded-xl md:border border-sc-fg-subtle/20 overflow-hidden">
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
            <div className="absolute inset-0 flex items-center justify-center bg-sc-bg-dark/80 z-20">
              <div className="flex items-center gap-3 text-sc-fg-muted">
                <Loader2 width={20} height={20} className="animate-spin text-sc-purple" />
                <span>Detecting communities & building graph...</span>
              </div>
            </div>
          )}

          {/* Empty state */}
          {!isLoading && graphData.nodes.length === 0 && (
            <div className="flex items-center justify-center h-full bg-sc-bg-dark">
              <GraphEmptyState />
            </div>
          )}

          {/* Graph */}
          {!isLoading && graphData.nodes.length > 0 && (
            <ForceGraph2D
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
              backgroundColor="#0a0812"
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
