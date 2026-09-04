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
import { clusterRadius, SEMANTIC_ZOOM, type Viewport, type ZoomBounds } from './semantic-zoom';

const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-base">
      <div className="text-sc-fg-muted">Loading graph...</div>
    </div>
  ),
});

/** Smallest a bubble paints on screen, so its count stays legible far out. */
const MIN_BUBBLE_SCREEN_RADIUS = 12;
/** Largest an entity paints on screen, so a close zoom shows labels, not discs. */
const MAX_NODE_SCREEN_RADIUS = 16;
const MAX_PROJECT_SCREEN_RADIUS = 24;
const MIN_ZOOM = 0.1;
const MAX_ZOOM = 10;

/**
 * Layout tuning. Bubbles are an order of magnitude larger than entity nodes,
 * so every force is per node rather than per mode: opening one domain must
 * not retune the forces on the rest of the map.
 */
const LAYOUT = {
  BUBBLE_CHARGE: -420,
  MEMBER_CHARGE: -40,
  CHARGE_REACH: 700,
  /** Gravity on bubbles, which gives repulsion something to balance against. */
  BUBBLE_GRAVITY: 0.06,
  /** Pull on an opened member toward where its bubble sat. */
  ANCHOR_GRAVITY: 0.18,
  /** Gravity on a node with no cluster, so it cannot drift off the map. */
  LOOSE_GRAVITY: 0.03,
  BUBBLE_PADDING: 40,
  MEMBER_COLLISION: 9,
  BUBBLE_LINK: 230,
  BRIDGE_LINK: 120,
  MEMBER_LINK: 45,
  BUBBLE_LINK_STRENGTH: 0.03,
} as const;

interface GraphCanvasProps {
  graphData: GraphData;
  graphRenderKey: string;
  filterKey: string;
  /** Labels for clusters currently showing members, drawn under the nodes. */
  expandedClusterLabels: Map<string, string>;
  /** Zooms that produce each level, derived from the bubble sizes. */
  zoomBounds: ZoomBounds;
  onViewportChange: (zoom: number, viewport: Viewport | null) => void;
  selectedNodeId: string | null;
  colors: CanvasColors;
  theme: 'neon' | 'dawn';
  isLoading: boolean;
  graphError: unknown;
  onNodeClick: (node: GraphNode) => void;
}

interface CameraTarget {
  k: number;
  x: number;
  y: number;
}

export const GraphCanvas = forwardRef<KnowledgeGraphRef, GraphCanvasProps>(function GraphCanvas(
  {
    graphData,
    graphRenderKey,
    filterKey,
    expandedClusterLabels,
    zoomBounds,
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
  // Camera bookkeeping for the mounted instance, reset with the render key.
  // The fit zoom frames the domain map; label thresholds are measured in
  // multiples of it so "two notches in" means the same on a sparse graph and
  // a dense one. Expansion is gated on the fit having run: before it, the
  // camera is wherever the library left it, and a cluster opened from there
  // would bloom on a map the reader has not seen yet.
  const fitZoomRef = useRef<number | null>(null);
  const fitSettledRef = useRef(false);
  // Once the reader moves the camera, no automatic fit may move it back.
  const interactedRef = useRef(false);
  const levelZoomRef = useRef(1);
  // Recent camera fits, surfaced on the wrapper for tests and automation.
  const fitLogRef = useRef<Array<{ t: number; k: number; why: string }>>([]);
  const graphDataRef = useRef(graphData);
  graphDataRef.current = graphData;
  const zoomBoundsRef = useRef(zoomBounds);
  zoomBoundsRef.current = zoomBounds;
  const attachGraph = useCallback((instance: ForceGraphMethods | null) => {
    graphRef.current = instance ?? undefined;
    setGraphReady(Boolean(instance));
  }, []);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const showCanvas = !isLoading && graphData.nodes.length > 0;

  // The library sizes its canvas to the window unless told otherwise, which
  // puts the camera's centre well outside the wrapper's. Every fit, pan, and
  // viewport calculation assumes the canvas is the wrapper.
  const [canvasSize, setCanvasSize] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    const element = canvasWrapperRef.current;
    if (!element || !showCanvas) return;
    const measure = () => {
      const { clientWidth, clientHeight } = element;
      if (clientWidth > 0 && clientHeight > 0) {
        setCanvasSize(size =>
          size?.width === clientWidth && size.height === clientHeight
            ? size
            : { width: clientWidth, height: clientHeight }
        );
      }
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [showCanvas]);

  useEffect(() => {
    if (!filterKey) return;
    setHoveredNode(null);
  }, [filterKey]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: a new render key mounts a fresh instance with a fresh camera
  useEffect(() => {
    fitZoomRef.current = null;
    fitSettledRef.current = false;
    interactedRef.current = false;
    levelZoomRef.current = 1;
    fitLogRef.current = [];
    if (flightRef.current !== null) {
      window.clearTimeout(flightRef.current);
      flightRef.current = null;
    }
  }, [graphRenderKey]);

  // Forces are configured once per mounted instance. Every strength is a
  // per-node accessor, so a dataset change (a cluster opening or closing)
  // only needs the reheat the library already performs.
  // biome-ignore lint/correctness/useExhaustiveDependencies: graphReady re-runs this once the dynamically imported instance exists
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph || !graphRenderKey) return;

    const isBubble = (node: d3Force.SimulationNodeDatum) => Boolean((node as GraphNode).aggregate);
    const members = (node: d3Force.SimulationNodeDatum) => (node as GraphNode).member_count || 1;

    // Bubbles repel hard and members softly; gravity toward the origin (or
    // toward the bubble a member came from) is what repulsion balances
    // against, so the map settles instead of spreading on every reheat.
    graph.d3Force('center', null);
    graph.d3Force(
      'charge',
      d3Force
        .forceManyBody()
        .strength(node => (isBubble(node) ? LAYOUT.BUBBLE_CHARGE : LAYOUT.MEMBER_CHARGE))
        .distanceMax(LAYOUT.CHARGE_REACH)
    );
    const gravityStrength = (node: d3Force.SimulationNodeDatum) => {
      const graphNode = node as GraphNode;
      if (graphNode.clusterAnchor) return LAYOUT.ANCHOR_GRAVITY;
      return graphNode.aggregate ? LAYOUT.BUBBLE_GRAVITY : LAYOUT.LOOSE_GRAVITY;
    };
    graph.d3Force(
      'gravityX',
      d3Force
        .forceX((node: d3Force.SimulationNodeDatum) => (node as GraphNode).clusterAnchor?.x ?? 0)
        .strength(gravityStrength)
    );
    graph.d3Force(
      'gravityY',
      d3Force
        .forceY((node: d3Force.SimulationNodeDatum) => (node as GraphNode).clusterAnchor?.y ?? 0)
        .strength(gravityStrength)
    );
    // Collision radius matches the painted radius, so spacing on the map is
    // spacing on screen.
    graph.d3Force(
      'collision',
      d3Force
        .forceCollide()
        .radius(node =>
          isBubble(node)
            ? clusterRadius(members(node)) + LAYOUT.BUBBLE_PADDING
            : LAYOUT.MEMBER_COLLISION
        )
        .strength(0.9)
    );

    // Links between bubbles stay long and weak so a heavily bridged set of
    // domains does not yank into a clump; member links use d3's degree-scaled
    // default so hubs do not collapse their neighbourhoods.
    const linkForce = graph.d3Force('link');
    if (linkForce && typeof linkForce.distance === 'function') {
      linkForce.distance((link: GraphLink) => {
        const ends = [link.source, link.target].filter(
          (end): end is GraphNode => typeof end === 'object' && end !== null
        );
        const bubbles = ends.filter(end => end.aggregate).length;
        if (bubbles === 2) return LAYOUT.BUBBLE_LINK;
        if (bubbles === 1) return LAYOUT.BRIDGE_LINK;
        return LAYOUT.MEMBER_LINK;
      });
    }
    if (linkForce && typeof linkForce.strength === 'function') {
      linkForce.strength((link: GraphLink) => {
        const ends = [link.source, link.target].filter(
          (end): end is GraphNode => typeof end === 'object' && end !== null
        );
        if (ends.length === 2 && ends.every(end => end.aggregate)) {
          return LAYOUT.BUBBLE_LINK_STRENGTH;
        }
        const degrees = ends.map(end => Math.max(end.degree || 1, 1));
        return 1 / Math.max(Math.min(...degrees), 1);
      });
    }

    const reheat = graph as ForceGraphMethods & { d3ReheatSimulation?: () => void };
    if (typeof reheat.d3ReheatSimulation === 'function') reheat.d3ReheatSimulation();
  }, [graphRenderKey, graphReady]);

  const markInteracted = useCallback(() => {
    interactedRef.current = true;
  }, []);

  // The camera that frames the domain map, capped so the largest bubble
  // stays under its opening radius: a fit that opened a domain by itself
  // would make the map impossible to return to. An open cluster contributes
  // the spot its bubble occupied (its members' anchor) rather than the
  // spread of its members, so the fit means the same thing whether or not
  // anything is open and never has to wait for a collapse to land.
  const computeFit = useCallback((): CameraTarget | null => {
    const element = canvasWrapperRef.current;
    if (!element) return null;
    const width = element.clientWidth;
    const height = element.clientHeight;
    if (width === 0 || height === 0) return null;

    let minX = Number.POSITIVE_INFINITY;
    let minY = Number.POSITIVE_INFINITY;
    let maxX = Number.NEGATIVE_INFINITY;
    let maxY = Number.NEGATIVE_INFINITY;
    for (const node of graphDataRef.current.nodes) {
      let x = node.x;
      let y = node.y;
      let radius = 8;
      if (node.aggregate) {
        radius = clusterRadius(node.member_count || 1);
      } else if (node.clusterAnchor) {
        x = node.clusterAnchor.x;
        y = node.clusterAnchor.y;
        radius = node.clusterAnchor.radius;
      }
      if (x === undefined || y === undefined) continue;
      minX = Math.min(minX, x - radius);
      minY = Math.min(minY, y - radius);
      maxX = Math.max(maxX, x + radius);
      maxY = Math.max(maxY, y + radius);
    }
    if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null;

    // Extra room below for the labels painted under each node.
    const pad = GRAPH_DEFAULTS.FIT_PADDING + 20;
    const spanX = Math.max(maxX - minX, 1);
    const spanY = Math.max(maxY - minY, 1);
    const fit = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY);
    const k = Math.min(Math.max(fit, MIN_ZOOM), zoomBoundsRef.current.domainCap, MAX_ZOOM);
    return { k, x: (minX + maxX) / 2, y: (minY + maxY) / 2 };
  }, []);

  // Pan, then zoom. The library runs centerAt and zoom as two transitions on
  // the same element, and starting both at once lets the second interrupt
  // the first, which lands the camera short of the centre every time.
  const flightRef = useRef<number | null>(null);
  const flyTo = useCallback((target: CameraTarget, durationMs: number, why: string) => {
    const graph = graphRef.current;
    if (!graph) return;
    fitLogRef.current = [
      ...fitLogRef.current.slice(-7),
      { t: Math.round(performance.now()), k: Number(target.k.toFixed(3)), why },
    ];
    const element = canvasWrapperRef.current;
    if (element) element.dataset.fitLog = JSON.stringify(fitLogRef.current);
    if (flightRef.current !== null) window.clearTimeout(flightRef.current);
    const half = Math.round(durationMs / 2);
    graph.centerAt(target.x, target.y, half);
    flightRef.current = window.setTimeout(() => {
      flightRef.current = null;
      graphRef.current?.zoom(target.k, half);
    }, half);
  }, []);

  // Once the domain map has settled, its bubbles stay where they are. Members
  // bloom around fixed anchors, the rest of the map holds still while one
  // domain is open, and the reader comes back to the same picture they left.
  // A bubble the reader drags keeps its new place for the same reason.
  const freezeBubbles = useCallback(() => {
    for (const node of graphDataRef.current.nodes) {
      if (!node.aggregate || node.fx !== undefined) continue;
      if (node.x === undefined || node.y === undefined) continue;
      node.fx = node.x;
      node.fy = node.y;
    }
  }, []);

  const effectiveZoom = useCallback((rawZoom: number) => {
    const fitZoom = fitZoomRef.current;
    return fitZoom ? rawZoom / fitZoom : 1;
  }, []);

  // The canvas is the only place that knows the camera, so it reports each
  // change up to the state that decides which clusters are open. Nothing here
  // is throttled: the expansion set only changes when a threshold is actually
  // crossed, and an unchanged set returns the previous reference.
  const reportViewport = useCallback(
    (transform: { k: number }) => {
      const graph = graphRef.current;
      const element = canvasWrapperRef.current;
      levelZoomRef.current = effectiveZoom(transform.k);
      // Surfaced on the wrapper so tests and browser automation can read the
      // camera without reaching into the canvas.
      if (element) {
        element.dataset.zoom = transform.k.toFixed(3);
        element.dataset.levelZoom = levelZoomRef.current.toFixed(3);
        element.dataset.fitSettled = String(fitSettledRef.current);
      }
      if (!graph || !fitSettledRef.current) return;

      const width = element?.clientWidth ?? 0;
      const height = element?.clientHeight ?? 0;
      if (width === 0 || height === 0) {
        onViewportChange(transform.k, null);
        return;
      }
      const topLeft = graph.screen2GraphCoords(0, 0);
      const bottomRight = graph.screen2GraphCoords(width, height);
      onViewportChange(transform.k, {
        minX: Math.min(topLeft.x, bottomRight.x),
        minY: Math.min(topLeft.y, bottomRight.y),
        maxX: Math.max(topLeft.x, bottomRight.x),
        maxY: Math.max(topLeft.y, bottomRight.y),
      });
    },
    [onViewportChange, effectiveZoom]
  );

  // Frame the domain map once the layout has a shape. The fit is recorded
  // even when it is not applied, so a reader who started zooming before the
  // layout settled still gets label thresholds measured against the map.
  const settleFit = useCallback(
    (durationMs: number, why: string) => {
      const graph = graphRef.current;
      if (!graph) return;
      const target = computeFit();
      if (!target) return;
      fitZoomRef.current = target.k;
      const first = !fitSettledRef.current;
      fitSettledRef.current = true;
      if (!interactedRef.current) {
        flyTo(target, durationMs, why);
      } else if (first) {
        reportViewport({ k: graph.zoom() });
      }
    },
    [computeFit, flyTo, reportViewport]
  );

  // The layout keeps spreading for a few seconds after the first frame, so
  // the fit runs early (a short delay, so the reader is not staring at an
  // off-centre map) and again once the simulation actually stops. Neither
  // moves a camera the reader has already taken hold of, and node count is
  // deliberately not a trigger: a cluster opening changes it too, and
  // refitting there would zoom the camera back out and shut the cluster the
  // reader just opened.
  const handleEngineStop = useCallback(() => {
    freezeBubbles();
    if (fitSettledRef.current && interactedRef.current) return;
    settleFit(400, 'engine-stop');
  }, [freezeBubbles, settleFit]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: graphRenderKey is the intentional re-fit trigger
  useEffect(() => {
    if (!graphReady) return;
    const timer = setTimeout(() => settleFit(600, 'backstop'), 1500);
    return () => clearTimeout(timer);
  }, [graphRenderKey, graphReady, settleFit]);

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
        // A bubble paints at the radius the expansion rule measures, so the
        // reader watches it grow toward its opening size. A floor keeps the
        // count legible when the camera is far out.
        size = Math.max(clusterRadius(memberCount), MIN_BUBBLE_SCREEN_RADIUS / globalScale);
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
      // Entities are sized in graph units for the layout; on screen they stop
      // growing once the camera is close, so a deep zoom reads as labels with
      // room between them rather than overlapping discs.
      if (!isAggregate) {
        const cap = isProject ? MAX_PROJECT_SCREEN_RADIUS : MAX_NODE_SCREEN_RADIUS;
        size = Math.min(size, cap / globalScale);
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
        if (screenRadius > SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS * SEMANTIC_ZOOM.DOMAIN_FIT_FRACTION) {
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

        // Truncate based on zoom - show more text as you zoom in. Domain
        // names are the map's main text and there are only a dozen or two,
        // so they get room from the start.
        const maxLen = Math.min(40, Math.floor((isAggregate ? 24 : 10) + levelZoom * 4));
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

  const handleCanvasNodeClick = useCallback(
    (node: GraphNode) => {
      onNodeClick(node);
      const graph = graphRef.current;
      if (!graph || node.x === undefined || node.y === undefined) return;
      interactedRef.current = true;

      if (node.aggregate) {
        // Fly to just past the zoom where this bubble crosses its own opening
        // radius, so the pin and the zoom agree about what the reader sees.
        const opening =
          (SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS / clusterRadius(node.member_count || 1)) * 1.2;
        const k = Math.min(Math.max(graph.zoom(), opening), MAX_ZOOM);
        flyTo({ k, x: node.x, y: node.y }, 800, 'bubble');
        return;
      }

      if (selectedNodeId === node.id) return;
      // An entity is read at a zoom where its label and neighbours fit.
      const readable = Math.min((fitZoomRef.current ?? 1) * 6, MAX_ZOOM);
      flyTo({ k: Math.max(graph.zoom(), readable), x: node.x, y: node.y }, 800, 'entity');
    },
    [onNodeClick, selectedNodeId, flyTo]
  );

  const zoomIn = useCallback(() => {
    const graph = graphRef.current;
    if (!graph) return;
    interactedRef.current = true;
    graph.zoom(Math.min(graph.zoom() * 1.5, MAX_ZOOM), 300);
  }, []);

  const zoomOut = useCallback(() => {
    const graph = graphRef.current;
    if (!graph) return;
    interactedRef.current = true;
    graph.zoom(Math.max(graph.zoom() / 1.5, MIN_ZOOM), 300);
  }, []);

  const fitView = useCallback(() => {
    interactedRef.current = true;
    const target = computeFit();
    if (target) flyTo(target, 400, 'fit');
  }, [computeFit, flyTo]);

  const zoomToLevel = useCallback(
    (level: 'domains' | 'entities') => {
      const graph = graphRef.current;
      if (!graph) return;
      interactedRef.current = true;
      if (level === 'domains') {
        // The fit frames the map by bubble positions and anchors, so it is
        // the same camera whether the clusters have folded yet or not.
        const target = computeFit();
        if (!target) return;
        fitZoomRef.current = target.k;
        flyTo(target, 600, 'domains');
        return;
      }
      const entityZoom = zoomBoundsRef.current.entity;
      if (entityZoom) graph.zoom(Math.min(entityZoom, MAX_ZOOM), 600);
    },
    [computeFit, flyTo]
  );

  useImperativeHandle(ref, () => ({ zoomIn, zoomOut, fitView, resetView: fitView, zoomToLevel }), [
    zoomIn,
    zoomOut,
    fitView,
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

      {showCanvas && (
        <div
          ref={canvasWrapperRef}
          className="absolute inset-0"
          onWheelCapture={markInteracted}
          onPointerDownCapture={markInteracted}
        >
          <ForceGraph2D
            key={graphRenderKey}
            ref={attachGraph as unknown as MutableRefObject<ForceGraphMethods | undefined>}
            graphData={graphData as { nodes: object[]; links: object[] }}
            width={canvasSize?.width}
            height={canvasSize?.height}
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
            minZoom={MIN_ZOOM}
            maxZoom={MAX_ZOOM}
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
