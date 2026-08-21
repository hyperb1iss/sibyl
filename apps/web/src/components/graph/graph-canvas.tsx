'use client';

import * as d3Force from 'd3-force';
import dynamic from 'next/dynamic';
import {
  forwardRef,
  type MutableRefObject,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import { ErrorState, GraphEmptyState } from '@/components/ui/empty-state';
import { Loader2 } from '@/components/ui/icons';
import type { GraphResolution } from '@/lib/api';
import { canvasNodeColor, GRAPH_DEFAULTS } from '@/lib/constants/graph';
import type {
  CanvasColors,
  GraphData,
  GraphLink,
  GraphNode,
  KnowledgeGraphRef,
} from './graph-types';

const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-base">
      <div className="text-sc-fg-muted">Loading graph...</div>
    </div>
  ),
});

function aggregateRadius(memberCount: number): number {
  return 10 + Math.log2(memberCount + 1) * 3.2;
}

interface GraphCanvasProps {
  graphData: GraphData;
  graphRenderKey: string;
  fitKey: string;
  filterKey: string;
  resolution: GraphResolution;
  selectedNodeId: string | null;
  colors: CanvasColors;
  theme: 'neon' | 'dawn';
  isLoading: boolean;
  graphError: unknown;
  onNodeClick: (node: GraphNode) => void;
}

export const GraphCanvas = forwardRef<KnowledgeGraphRef, GraphCanvasProps>(function GraphCanvas(
  {
    graphData,
    graphRenderKey,
    fitKey,
    filterKey,
    resolution: graphResolution,
    selectedNodeId,
    colors,
    theme,
    isLoading,
    graphError,
    onNodeClick,
  },
  ref
) {
  const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const [hasInitialFit, setHasInitialFit] = useState(false);
  const fitKeyRef = useRef('');

  useEffect(() => {
    if (!filterKey) return;
    setHoveredNode(null);
  }, [filterKey]);

  useEffect(() => {
    if (fitKeyRef.current !== fitKey) {
      fitKeyRef.current = fitKey;
      setHasInitialFit(false);
    }
  }, [fitKey]);

  // Configure d3 forces whenever the graph dataset/mode changes.
  useEffect(() => {
    const nodeCount = graphData.nodes.length;
    const linkCount = graphData.links.length;
    if (!graphRef.current || !graphRenderKey || (nodeCount === 0 && linkCount === 0)) return;

    const isOverview = graphResolution === 'overview';

    // Overview: a few large bubbles that must spread and never overlap — strong
    // repulsion, long links, and a per-node collision radius matching each
    // bubble's painted size. Detail: strong repulsion + long links spread a
    // dense subgraph into an explorable web instead of a hairball.
    // Overview is a handful of big bubbles: let collision do the spacing, keep
    // charge low and centering strong so the map stays a compact, framed cluster
    // instead of flinging outliers off-canvas.
    const chargeStrength = isOverview
      ? -260
      : nodeCount >= 600
        ? -240
        : nodeCount >= 300
          ? -200
          : -150;
    const linkDistance = isOverview
      ? 130
      : nodeCount >= 600
        ? 75
        : nodeCount >= 300
          ? 65
          : GRAPH_DEFAULTS.LINK_DISTANCE;
    const baseCollision = nodeCount >= 600 ? 18 : nodeCount >= 300 ? 16 : 14;
    const centerStrength = isOverview ? 0.3 : 0.04;

    graphRef.current.d3Force(
      'charge',
      d3Force
        .forceManyBody()
        .strength(chargeStrength)
        .distanceMax(linkDistance * 12)
    );
    graphRef.current.d3Force('center', d3Force.forceCenter().strength(centerStrength));
    graphRef.current.d3Force(
      'collision',
      d3Force
        .forceCollide()
        .radius((node: d3Force.SimulationNodeDatum) => {
          const graphNode = node as GraphNode;
          return graphNode.aggregate
            ? aggregateRadius(graphNode.member_count || 1) + 22
            : baseCollision;
        })
        .strength(0.95)
    );

    // Link force with distance - ForceFn has [key: string]: any so we can access distance directly
    const linkForce = graphRef.current.d3Force('link');
    if (linkForce && typeof linkForce.distance === 'function') {
      linkForce.distance(linkDistance);
    }
    // In overview, keep links weak so a heavily-bridged set of domains doesn't
    // yank into a tight clump — collision and centering set the spacing instead.
    if (linkForce && typeof linkForce.strength === 'function' && isOverview) {
      linkForce.strength(0.04);
    }

    // Reheat simulation after re-keyed graph mounts (project/type/cluster switches).
    const graph = graphRef.current as ForceGraphMethods & {
      d3ReheatSimulation?: () => void;
    };
    if (typeof graph.d3ReheatSimulation === 'function') {
      graph.d3ReheatSimulation();
    }
  }, [graphData.nodes.length, graphData.links.length, graphRenderKey, graphResolution]);

  // Reliably frame the layout once it has settled. onEngineStop can fire before
  // the reheated simulation finishes spreading, leaving the graph small and
  // off-center, so re-fit on a short delay whenever the dataset/mode changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: graphRenderKey is an intentional re-fit trigger on mode/filter/theme change
  useEffect(() => {
    if (!graphRef.current || graphData.nodes.length === 0) return;
    const timer = setTimeout(() => {
      graphRef.current?.zoomToFit(600, GRAPH_DEFAULTS.FIT_PADDING);
    }, 1500);
    return () => clearTimeout(timer);
  }, [graphRenderKey, graphData.nodes.length]);

  // Clean node rendering - entity colors + degree-based sizing
  // Labels scale with zoom: more labels appear as you zoom in
  const paintNode = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = node.x || 0;
      const y = node.y || 0;
      const isSelected = node.id === selectedNodeId;
      const isHovered = node.id === hoveredNode;
      const isProject = node.isProject;
      const isNeighbor = node.isNeighbor;
      const isSearchMatch = node.isSearchMatch;
      const isAggregate = Boolean(node.aggregate);
      const degree = node.degree || 0;
      const maxDegree = graphData.maxDegree || 1;
      const memberCount = node.member_count || 1;

      // Size based on degree (connections) - more connections = bigger
      const degreeScale = Math.sqrt(degree / maxDegree);
      const logDegree = degree > 0 ? Math.log2(degree + 1) / Math.log2(maxDegree + 1) : 0;
      const combinedScale = (degreeScale + logDegree) / 2;

      // Minimum size of 5px ensures all nodes are visible
      // Neighbors are slightly smaller to emphasize cluster nodes
      // Search matches are enlarged for visibility
      let size: number;
      if (isProject) {
        size = 14 + combinedScale * 10;
      } else if (isAggregate) {
        size = Math.max(14, aggregateRadius(memberCount) + combinedScale * 6);
      } else if (isSelected) {
        size = Math.max(12, 6 + combinedScale * 10);
      } else if (isHovered) {
        size = Math.max(10, 5 + combinedScale * 9);
      } else if (isSearchMatch) {
        size = Math.max(10, 6 + combinedScale * 10); // Enlarged for visibility
      } else if (isNeighbor) {
        size = 4 + combinedScale * 8; // Smaller context nodes
      } else {
        size = 5 + combinedScale * 12;
      }

      const isDawn = theme === 'dawn';
      // Color by entity type so projects, tasks, and memory are distinguishable
      // at a glance. Canvas can't read CSS vars, so darken hues for dawn.
      const baseColor = canvasNodeColor(node.entityColor || '#8b85a0', theme);
      // Neighbors are rendered at 40% opacity to fade into background
      // Search matches keep full opacity
      const color =
        isNeighbor && !isSelected && !isHovered && !isSearchMatch ? `${baseColor}66` : baseColor;

      // Outer glow for search matches (electric purple pulse)
      if (isSearchMatch && !isSelected && !isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, size + 8, 0, 2 * Math.PI);
        ctx.fillStyle = 'rgba(225, 53, 255, 0.15)'; // Electric purple outer
        ctx.fill();
        ctx.beginPath();
        ctx.arc(x, y, size + 4, 0, 2 * Math.PI);
        ctx.fillStyle = 'rgba(225, 53, 255, 0.3)'; // Electric purple inner
        ctx.fill();
      }

      // Glow for selected/hovered
      if (isSelected || isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, size + 4, 0, 2 * Math.PI);
        ctx.fillStyle = `${color}40`;
        ctx.fill();
      }

      if (isAggregate) {
        // Cluster bubble: translucent fill, solid ring, member count inside.
        ctx.beginPath();
        ctx.arc(x, y, size, 0, 2 * Math.PI);
        ctx.fillStyle = `${baseColor}2e`;
        ctx.fill();
        ctx.strokeStyle = baseColor;
        ctx.lineWidth = 2;
        ctx.stroke();

        const countText =
          memberCount >= 1000 ? `${(memberCount / 1000).toFixed(1)}k` : String(memberCount);
        const countFont = Math.max(6, Math.min(size * 0.8, 22));
        ctx.font = `600 ${countFont}px "Space Grotesk", sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = colors.fgPrimary;
        ctx.fillText(countText, x, y);
      } else {
        // Main node
        ctx.beginPath();
        ctx.arc(x, y, size, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.fill();

        // On the light dawn canvas a thin dark outline keeps pale nodes legible.
        if (isDawn && !isSelected && !isHovered && !isSearchMatch) {
          ctx.strokeStyle = 'rgba(43, 37, 64, 0.55)';
          ctx.lineWidth = 0.6;
          ctx.stroke();
        }
      }

      // Border for selected/hovered/search match
      if (isSelected) {
        ctx.strokeStyle = isDawn ? '#2b2540' : '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();
      } else if (isHovered) {
        ctx.strokeStyle = isDawn ? 'rgba(43, 37, 64, 0.5)' : 'rgba(255, 255, 255, 0.5)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      } else if (isSearchMatch) {
        ctx.strokeStyle = '#e135ff'; // Electric purple border
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // =================================================================
      // LABEL VISIBILITY - Progressive reveal based on zoom level
      // =================================================================
      // globalScale: 0.3 = zoomed out, 1.0 = default, 4.0+ = zoomed in

      const isHubNode = degree > Math.max(3, maxDegree * 0.05) || (isAggregate && memberCount >= 3);

      // Determine if label should show based on zoom + importance
      // Neighbors only show labels when hovered/selected to keep focus on cluster
      // Search matches always show labels for discoverability
      let showLabel = false;

      if (isSelected || isHovered || isSearchMatch) {
        showLabel = true;
      } else if (isNeighbor) {
        // Neighbors only show label when zoomed in very close
        showLabel = globalScale >= 4.0;
      } else if (isAggregate) {
        // Domain bubbles are always named — the label is the meaningful part.
        showLabel = true;
      } else if (isProject) {
        // Projects are the anchors — always name them.
        showLabel = true;
      } else if (isHubNode && globalScale >= 0.7) {
        showLabel = true;
      } else if (degree >= 5 && globalScale >= 1.2) {
        showLabel = true;
      } else if (degree >= 3 && globalScale >= 1.8) {
        showLabel = true;
      } else if (degree >= 1 && globalScale >= 2.5) {
        showLabel = true;
      } else if (globalScale >= 3.5) {
        showLabel = true;
      }

      if (showLabel) {
        const label = node.label || node.name || node.id.slice(0, 8);

        // Truncate based on zoom - show more text as you zoom in
        const maxLen = Math.min(40, Math.floor(10 + globalScale * 5));
        const displayLabel = label.length > maxLen ? `${label.slice(0, maxLen - 3)}...` : label;

        // Font size: DIVIDE by globalScale to keep consistent screen size
        // Canvas is scaled by globalScale, so counter-scale the font
        const screenFontSize = 11; // desired size on screen in pixels
        const fontSize = screenFontSize / globalScale;

        ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';

        const labelY = y + size + 2 / globalScale; // small gap below node

        // Text shadow for readability
        const shadowOffset = 0.5 / globalScale;
        ctx.fillStyle = theme === 'neon' ? 'rgba(0, 0, 0, 0.8)' : 'rgba(255, 255, 255, 0.9)';
        ctx.fillText(displayLabel, x + shadowOffset, labelY + shadowOffset);

        // Text color - slightly transparent for non-priority labels
        const textColor = colors.fgPrimary;
        const isPriority = isSelected || isHovered || isProject || isHubNode || isSearchMatch;
        ctx.fillStyle = isPriority ? textColor : `${textColor}bb`;
        ctx.fillText(displayLabel, x, labelY);
      }
    },
    [selectedNodeId, hoveredNode, graphData.maxDegree, theme, colors]
  );

  // Use the library's native link renderer for robustness; only customize
  // width/color callbacks for highlight behavior.
  const getLinkEndpointId = useCallback(
    (endpoint: string | number | GraphNode | undefined): string | null => {
      if (typeof endpoint === 'string') return endpoint;
      if (typeof endpoint === 'number') return graphData.nodes[endpoint]?.id ?? null;
      if (endpoint && typeof endpoint === 'object') return endpoint.id;
      return null;
    },
    [graphData.nodes]
  );

  const linkColor = useCallback(
    (link: GraphLink) => {
      const sourceId = getLinkEndpointId(link.source);
      const targetId = getLinkEndpointId(link.target);
      const isHighlighted =
        sourceId === selectedNodeId ||
        targetId === selectedNodeId ||
        sourceId === hoveredNode ||
        targetId === hoveredNode;

      if (isHighlighted) {
        return theme === 'neon' ? 'rgba(255, 255, 255, 0.72)' : 'rgba(43, 37, 64, 0.78)';
      }
      return theme === 'neon' ? 'rgba(255, 255, 255, 0.52)' : 'rgba(43, 37, 64, 0.6)';
    },
    [getLinkEndpointId, selectedNodeId, hoveredNode, theme]
  );

  const linkWidth = useCallback(
    (link: GraphLink) => {
      const sourceId = getLinkEndpointId(link.source);
      const targetId = getLinkEndpointId(link.target);
      const isHighlighted =
        sourceId === selectedNodeId ||
        targetId === selectedNodeId ||
        sourceId === hoveredNode ||
        targetId === hoveredNode;
      return isHighlighted ? 2 : 1.2;
    },
    [getLinkEndpointId, selectedNodeId, hoveredNode]
  );

  const handleEngineStop = useCallback(() => {
    if (hasInitialFit || !graphRef.current) return;
    graphRef.current.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
    setHasInitialFit(true);
  }, [hasInitialFit]);

  const handleCanvasNodeClick = useCallback(
    (node: GraphNode) => {
      onNodeClick(node);

      if (node.aggregate) {
        if (graphRef.current && node.x !== undefined && node.y !== undefined) {
          graphRef.current.centerAt(node.x, node.y, 800);
          graphRef.current.zoom(2.1, 800);
        }
        return;
      }

      const isDeselecting = selectedNodeId === node.id;
      if (!isDeselecting && graphRef.current && node.x !== undefined && node.y !== undefined) {
        graphRef.current.centerAt(node.x, node.y, 800);
        const currentZoom = graphRef.current.zoom();
        if (currentZoom < 2.5) {
          graphRef.current.zoom(2.5, 800);
        }
      }
    },
    [onNodeClick, selectedNodeId]
  );

  const zoomIn = useCallback(() => {
    if (graphRef.current) {
      const currentZoom = graphRef.current.zoom();
      graphRef.current.zoom(currentZoom * 1.5, 300);
    }
  }, []);

  const zoomOut = useCallback(() => {
    if (graphRef.current) {
      const currentZoom = graphRef.current.zoom();
      graphRef.current.zoom(currentZoom / 1.5, 300);
    }
  }, []);

  const fitView = useCallback(() => {
    graphRef.current?.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
  }, []);

  const resetView = useCallback(() => {
    graphRef.current?.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
    graphRef.current?.centerAt(0, 0, 300);
  }, []);

  useImperativeHandle(ref, () => ({ zoomIn, zoomOut, fitView, resetView }), [
    zoomIn,
    zoomOut,
    fitView,
    resetView,
  ]);

  return (
    <>
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

      {!isLoading && graphError && graphData.nodes.length === 0 && (
        <div
          className="flex items-center justify-center h-full"
          style={{ backgroundColor: colors.bg }}
          suppressHydrationWarning
        >
          <ErrorState
            title="Couldn't load the graph"
            message={
              graphError instanceof Error
                ? graphError.message
                : 'The graph request failed. Check your connection and try again.'
            }
          />
        </div>
      )}

      {!isLoading && !graphError && graphData.nodes.length === 0 && (
        <div
          className="flex items-center justify-center h-full"
          style={{ backgroundColor: colors.bg }}
          suppressHydrationWarning
        >
          <GraphEmptyState />
        </div>
      )}

      {!isLoading && graphData.nodes.length > 0 && (
        <ForceGraph2D
          key={graphRenderKey}
          ref={graphRef as MutableRefObject<ForceGraphMethods | undefined>}
          graphData={graphData as { nodes: object[]; links: object[] }}
          nodeLabel={() => ''}
          nodeCanvasObject={
            paintNode as (node: object, ctx: CanvasRenderingContext2D, globalScale: number) => void
          }
          nodeCanvasObjectMode={() => 'replace'}
          linkColor={linkColor as (link: object) => string}
          linkWidth={linkWidth as (link: object) => number}
          onNodeClick={handleCanvasNodeClick as (node: object, event: MouseEvent) => void}
          onNodeHover={node => setHoveredNode((node as GraphNode)?.id || null)}
          onEngineStop={handleEngineStop}
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
    </>
  );
});
