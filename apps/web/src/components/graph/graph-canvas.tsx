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
import { canvasNodeColor, GRAPH_DEFAULTS } from '@/lib/constants/graph';
import type {
  CanvasColors,
  GraphData,
  GraphLink,
  GraphNode,
  KnowledgeGraphRef,
} from './graph-types';
import { SEMANTIC_ZOOM, type Viewport } from './semantic-zoom';

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

/** Smallest a domain bubble paints on screen, in pixels, whatever the zoom. */
const MIN_BUBBLE_SCREEN_RADIUS = 26;

interface GraphCanvasProps {
  graphData: GraphData;
  graphRenderKey: string;
  fitKey: string;
  filterKey: string;
  /** True while every cluster is still summarized, which the forces tune for. */
  isDomainLevel: boolean;
  /** Labels for clusters currently showing members, drawn under the nodes. */
  expandedClusterLabels: Map<string, string>;
  onViewportChange: (zoom: number, viewport: Viewport | null) => void;
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
    isDomainLevel,
    expandedClusterLabels,
    onViewportChange,
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
  const canvasWrapperRef = useRef<HTMLDivElement>(null);
  // ForceGraph2D is imported dynamically, so its instance lands well after the
  // first render. Force configuration and the opening fit both need it, and a
  // plain ref never re-runs an effect, so its arrival is state.
  const [graphReady, setGraphReady] = useState(false);
  // The zoom that frames the whole domain map. Expansion thresholds are
  // measured against it rather than against the raw d3 scale: a sparse graph
  // fits at 0.2 and a dense one at 1.5, and "two notches in from the map"
  // should open the same domain on both.
  const fitZoomRef = useRef<number | null>(null);
  // Effective zoom (multiples of the fitted map), for label thresholds that
  // should mean the same thing on a sparse graph and a dense one.
  const levelZoomRef = useRef(1);
  const attachGraph = useCallback((instance: ForceGraphMethods | null) => {
    graphRef.current = instance ?? undefined;
    setGraphReady(Boolean(instance));
  }, []);
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
  // biome-ignore lint/correctness/useExhaustiveDependencies: graphReady re-runs this once the dynamically imported instance exists
  useEffect(() => {
    const nodeCount = graphData.nodes.length;
    const linkCount = graphData.links.length;
    if (!graphRef.current || !graphRenderKey || (nodeCount === 0 && linkCount === 0)) return;

    const isOverview = isDomainLevel;

    // Overview: a few large bubbles that must spread and never overlap — strong
    // repulsion, long links, and a per-node collision radius matching each
    // bubble's painted size. Detail: strong repulsion + long links spread a
    // dense subgraph into an explorable web instead of a hairball.
    // Overview is a handful of big bubbles: let collision do the spacing, keep
    // charge low and centering strong so the map stays a compact, framed cluster
    // instead of flinging outliers off-canvas.
    // Domain bubbles are an order of magnitude larger than entity nodes, so
    // they need repulsion and link lengths to match: at entity-scale settings
    // a dozen bubbles pile into one overlapping clump.
    const chargeStrength = isOverview
      ? -520
      : nodeCount >= 600
        ? -240
        : nodeCount >= 300
          ? -200
          : -150;
    const linkDistance = isOverview
      ? 170
      : nodeCount >= 600
        ? 75
        : nodeCount >= 300
          ? 65
          : GRAPH_DEFAULTS.LINK_DISTANCE;
    const baseCollision = nodeCount >= 600 ? 18 : nodeCount >= 300 ? 16 : 14;
    const centerStrength = isOverview ? 0.08 : 0.04;

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
            ? aggregateRadius(graphNode.member_count || 1) + 34
            : baseCollision;
        })
        .strength(0.95)
    );

    // Opened members are pulled gently back toward the bubble they came from,
    // so a domain that opens stays a region on the map rather than a burst of
    // nodes radiating across it. Bubbles and unanchored nodes feel nothing.
    const anchorStrength = (node: d3Force.SimulationNodeDatum) =>
      (node as GraphNode).clusterAnchor ? 0.14 : 0;
    graphRef.current.d3Force(
      'anchorX',
      d3Force
        .forceX((node: d3Force.SimulationNodeDatum) => {
          const graphNode = node as GraphNode;
          return graphNode.clusterAnchor?.x ?? graphNode.x ?? 0;
        })
        .strength(anchorStrength)
    );
    graphRef.current.d3Force(
      'anchorY',
      d3Force
        .forceY((node: d3Force.SimulationNodeDatum) => {
          const graphNode = node as GraphNode;
          return graphNode.clusterAnchor?.y ?? graphNode.y ?? 0;
        })
        .strength(anchorStrength)
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
  }, [graphData.nodes.length, graphData.links.length, graphRenderKey, isDomainLevel, graphReady]);

  // Reliably frame the layout once it has settled. onEngineStop can fire before
  // the reheated simulation finishes spreading, leaving the graph small and
  // off-center, so re-fit on a short delay whenever the dataset changes. The
  // node count is deliberately not a trigger: a cluster opening changes it
  // too, and refitting there would zoom the camera back out and shut the
  // cluster the reader just opened.
  // biome-ignore lint/correctness/useExhaustiveDependencies: graphRenderKey and fitKey are the intentional re-fit triggers
  useEffect(() => {
    if (!graphRef.current || graphData.nodes.length === 0) return;
    const timer = setTimeout(() => {
      graphRef.current?.zoomToFit(600, GRAPH_DEFAULTS.FIT_PADDING);
      window.setTimeout(() => {
        const zoom = graphRef.current?.zoom();
        if (typeof zoom === 'number' && zoom > 0) fitZoomRef.current = zoom;
      }, 700);
    }, 1500);
    return () => clearTimeout(timer);
  }, [graphRenderKey, fitKey, graphReady]);

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
        // A bubble is a summary the reader has to be able to read at any zoom,
        // so it never paints below a fixed screen size. The natural radius
        // takes over once the camera is close enough for it to be larger.
        const naturalRadius = aggregateRadius(memberCount) + combinedScale * 6;
        size = Math.max(naturalRadius, MIN_BUBBLE_SCREEN_RADIUS / globalScale);
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

        // A bubble close to its opening threshold says so, so the reader knows
        // the zoom is about to buy them something rather than just scale.
        const screenRadius = size * globalScale;
        if (screenRadius > SEMANTIC_ZOOM.COLLAPSE_SCREEN_RADIUS * 0.8) {
          const hintFont = Math.max(5, 9 / globalScale);
          ctx.font = `${hintFont}px "JetBrains Mono", monospace`;
          ctx.fillStyle = `${colors.fgMuted}cc`;
          ctx.fillText('zoom to open', x, y + size * 0.55);
        }
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
      // Thresholds are in multiples of the fitted map, not the raw d3 scale:
      // the map fits at wildly different scales depending on how spread the
      // layout is, and "labels appear two notches in" has to hold on all of
      // them. Font sizes still counter-scale on globalScale below.
      const levelZoom = levelZoomRef.current;

      const isHubNode = degree > Math.max(3, maxDegree * 0.05) || (isAggregate && memberCount >= 3);

      // Determine if label should show based on zoom + importance
      // Neighbors only show labels when hovered/selected to keep focus on cluster
      // Search matches always show labels for discoverability
      let showLabel = false;

      if (isSelected || isHovered || isSearchMatch) {
        showLabel = true;
      } else if (isNeighbor) {
        // Neighbors only show label when zoomed in very close
        showLabel = levelZoom >= 6;
      } else if (isAggregate) {
        // Domain bubbles are always named — the label is the meaningful part.
        showLabel = true;
      } else if (isProject) {
        // Projects are the anchors — always name them.
        showLabel = true;
      } else if (isHubNode && levelZoom >= 1.5) {
        showLabel = true;
      } else if (degree >= 5 && levelZoom >= 2.2) {
        showLabel = true;
      } else if (degree >= 3 && levelZoom >= 3) {
        showLabel = true;
      } else if (degree >= 1 && levelZoom >= 4) {
        showLabel = true;
      } else if (levelZoom >= 5.5) {
        showLabel = true;
      }

      if (showLabel) {
        const label = node.label || node.name || node.id.slice(0, 8);

        // Truncate based on zoom - show more text as you zoom in
        const maxLen = Math.min(40, Math.floor(10 + levelZoom * 4));
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

  // An opened cluster loses its bubble, and with it the reader's sense of
  // which domain they are inside. Its name is painted under the nodes at the
  // centre of its members so the answer stays on screen while the contents
  // are what is being read.
  const paintClusterContext = useCallback(
    (ctx: CanvasRenderingContext2D, globalScale: number) => {
      if (expandedClusterLabels.size === 0) return;

      const sums = new Map<string, { x: number; y: number; count: number }>();
      for (const node of graphData.nodes) {
        if (node.aggregate) continue;
        const clusterId = node.cluster_id;
        if (!clusterId || !expandedClusterLabels.has(clusterId)) continue;
        if (node.x === undefined || node.y === undefined) continue;
        const sum = sums.get(clusterId) ?? { x: 0, y: 0, count: 0 };
        sum.x += node.x;
        sum.y += node.y;
        sum.count += 1;
        sums.set(clusterId, sum);
      }

      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      for (const [clusterId, sum] of sums) {
        if (sum.count < 2) continue;
        const label = expandedClusterLabels.get(clusterId);
        if (!label) continue;
        const fontSize = Math.min(34, 15 / globalScale + 9);
        ctx.font = `700 ${fontSize}px "Space Grotesk", sans-serif`;
        ctx.fillStyle = theme === 'neon' ? 'rgba(255, 255, 255, 0.09)' : 'rgba(43, 37, 64, 0.11)';
        ctx.fillText(label.toUpperCase(), sum.x / sum.count, sum.y / sum.count);
      }
      ctx.restore();
    },
    [expandedClusterLabels, graphData.nodes, theme]
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

      // Click zooms are multiples of the fitted map, like the thresholds: a
      // bubble opens at a zoom where its members are legible, an entity at
      // one where its label and neighbours are.
      const fitZoom = fitZoomRef.current ?? graphRef.current?.zoom() ?? 1;
      if (node.aggregate) {
        if (graphRef.current && node.x !== undefined && node.y !== undefined) {
          graphRef.current.centerAt(node.x, node.y, 800);
          graphRef.current.zoom(Math.max(graphRef.current.zoom(), fitZoom * 3.5), 800);
        }
        return;
      }

      const isDeselecting = selectedNodeId === node.id;
      if (!isDeselecting && graphRef.current && node.x !== undefined && node.y !== undefined) {
        graphRef.current.centerAt(node.x, node.y, 800);
        const currentZoom = graphRef.current.zoom();
        if (currentZoom < fitZoom * 6) {
          graphRef.current.zoom(fitZoom * 6, 800);
        }
      }
    },
    [onNodeClick, selectedNodeId]
  );

  // Largest bubble at its natural size, in graph units. At the fitted zoom
  // that bubble paints at the minimum screen radius, so mapping it there
  // makes the expansion thresholds mean "this many times the fitted view".
  const maxNaturalRadius = (() => {
    let largest = 0;
    for (const node of graphData.nodes) {
      if (!node.aggregate) continue;
      largest = Math.max(largest, aggregateRadius(node.member_count || 1));
    }
    return largest;
  })();

  const effectiveZoom = useCallback(
    (rawZoom: number) => {
      const fitZoom = fitZoomRef.current;
      if (!fitZoom || maxNaturalRadius === 0) return rawZoom;
      return (rawZoom / fitZoom) * (MIN_BUBBLE_SCREEN_RADIUS / maxNaturalRadius);
    },
    [maxNaturalRadius]
  );

  // The canvas is the only place that knows the camera, so it reports each
  // change up to the state that decides which clusters are open. Nothing here
  // is throttled: the expansion set only changes when a threshold is actually
  // crossed, and an unchanged set returns the previous reference.
  const reportViewport = useCallback(
    (transform: { k: number }) => {
      const graph = graphRef.current;
      if (!graph) {
        onViewportChange(effectiveZoom(transform.k), null);
        return;
      }
      const element = canvasWrapperRef.current;
      const width = element?.clientWidth ?? 0;
      const height = element?.clientHeight ?? 0;
      // Surfaced on the wrapper so tests and browser automation can read the
      // camera without reaching into the canvas.
      levelZoomRef.current = effectiveZoom(transform.k);
      if (element) {
        element.dataset.zoom = transform.k.toFixed(3);
        element.dataset.levelZoom = levelZoomRef.current.toFixed(3);
      }
      if (width === 0 || height === 0) {
        onViewportChange(effectiveZoom(transform.k), null);
        return;
      }
      const topLeft = graph.screen2GraphCoords(0, 0);
      const bottomRight = graph.screen2GraphCoords(width, height);
      onViewportChange(effectiveZoom(transform.k), {
        minX: Math.min(topLeft.x, bottomRight.x),
        minY: Math.min(topLeft.y, bottomRight.y),
        maxX: Math.max(topLeft.x, bottomRight.x),
        maxY: Math.max(topLeft.y, bottomRight.y),
      });
    },
    [onViewportChange, effectiveZoom]
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

  const zoomToLevel = useCallback((level: 'domains' | 'entities') => {
    const graph = graphRef.current;
    if (!graph) return;
    if (level === 'domains') {
      graph.zoomToFit(600, GRAPH_DEFAULTS.FIT_PADDING);
      return;
    }
    const fitZoom = fitZoomRef.current ?? graph.zoom();
    graph.zoom(fitZoom * SEMANTIC_ZOOM.ENTITY_ZOOM_FACTOR, 600);
  }, []);

  useImperativeHandle(ref, () => ({ zoomIn, zoomOut, fitView, resetView, zoomToLevel }), [
    zoomIn,
    zoomOut,
    fitView,
    resetView,
    zoomToLevel,
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
        <div ref={canvasWrapperRef} className="absolute inset-0">
          <ForceGraph2D
            key={graphRenderKey}
            ref={attachGraph as unknown as MutableRefObject<ForceGraphMethods | undefined>}
            graphData={graphData as { nodes: object[]; links: object[] }}
            nodeLabel={() => ''}
            nodeCanvasObject={
              paintNode as (
                node: object,
                ctx: CanvasRenderingContext2D,
                globalScale: number
              ) => void
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
            onZoom={reportViewport}
            onRenderFramePre={paintClusterContext}
            onZoomEnd={reportViewport}
          />
        </div>
      )}
    </>
  );
});
