/**
 * Semantic zoom: which level of the graph hierarchy is visible at a given
 * zoom, for each cluster independently.
 *
 * Geometric zoom makes everything bigger. Semantic zoom trades a summary for
 * its contents once there is room to read them, so pushing in past a domain
 * bubble replaces it with the entities it stands for while the rest of the
 * map stays summarized.
 *
 * The decision is per cluster and depends on apparent size rather than the
 * zoom number alone: a 900-member domain earns its detail sooner than a
 * 12-member one because its bubble covers more of the screen at the same
 * zoom. Clusters far outside the viewport stay collapsed however far in the
 * camera is, which is what keeps a push into one corner from paying for the
 * whole graph.
 */

/** Painted radius of a cluster bubble in graph units, before zoom. */
export function clusterRadius(memberCount: number): number {
  return 10 + Math.log2(Math.max(memberCount, 1) + 1) * 3.2;
}

export const SEMANTIC_ZOOM = {
  /** Screen radius (px) at which a bubble opens into its members. */
  EXPAND_SCREEN_RADIUS: 78,
  /**
   * Screen radius at which an open cluster collapses again. Lower than the
   * expand threshold on purpose: without the gap a cluster sitting exactly on
   * the boundary flickers between levels on every scroll tick.
   */
  COLLAPSE_SCREEN_RADIUS: 58,
  /**
   * How far outside the viewport a cluster still counts as on screen, as a
   * fraction of viewport size. Expanding just past the edge means panning
   * reveals detail that is already laid out instead of blooming in view.
   */
  VIEWPORT_MARGIN: 0.6,
  /**
   * How many times the fitted domain map the Entities jump zooms to. Large
   * enough that every bubble on screen has crossed its opening threshold.
   */
  ENTITY_ZOOM_FACTOR: 7,
} as const;

export interface ClusterExtent {
  id: string;
  memberCount: number;
  /** Bubble centre in graph coordinates, absent until the layout places it. */
  x?: number;
  y?: number;
}

export interface Viewport {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

function withinViewport(cluster: ClusterExtent, viewport: Viewport, radius: number): boolean {
  if (cluster.x === undefined || cluster.y === undefined) return true;
  const width = viewport.maxX - viewport.minX;
  const height = viewport.maxY - viewport.minY;
  const marginX = width * SEMANTIC_ZOOM.VIEWPORT_MARGIN + radius;
  const marginY = height * SEMANTIC_ZOOM.VIEWPORT_MARGIN + radius;
  return (
    cluster.x >= viewport.minX - marginX &&
    cluster.x <= viewport.maxX + marginX &&
    cluster.y >= viewport.minY - marginY &&
    cluster.y <= viewport.maxY + marginY
  );
}

export interface ResolveExpansionArgs {
  clusters: ClusterExtent[];
  /** d3-zoom scale factor: 1 is the layout's own scale. */
  zoom: number;
  /** Visible region in graph coordinates, or null before the canvas reports one. */
  viewport: Viewport | null;
  /** Clusters expanded on the previous frame, for hysteresis. */
  previous: ReadonlySet<string>;
  /** Clusters the reader opened by hand; they ignore the size thresholds. */
  pinned?: ReadonlySet<string>;
}

/**
 * The clusters whose members should be drawn instead of their bubble.
 *
 * Pure so the thresholds can be tested without a canvas.
 */
export function resolveExpandedClusters({
  clusters,
  zoom,
  viewport,
  previous,
  pinned,
}: ResolveExpansionArgs): Set<string> {
  // A pin is the reader's own decision, so it holds even while the cluster is
  // missing from the current list: a filter change can empty that list for a
  // frame, and dropping the pin there would shut an open cluster underneath
  // someone mid-read.
  const expanded = new Set<string>(pinned ?? []);

  for (const cluster of clusters) {
    if (expanded.has(cluster.id)) continue;

    const screenRadius = clusterRadius(cluster.memberCount) * zoom;
    const wasExpanded = previous.has(cluster.id);
    const threshold = wasExpanded
      ? SEMANTIC_ZOOM.COLLAPSE_SCREEN_RADIUS
      : SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS;

    if (screenRadius < threshold) continue;
    if (viewport && !withinViewport(cluster, viewport, clusterRadius(cluster.memberCount))) {
      continue;
    }

    expanded.add(cluster.id);
  }

  return expanded;
}

export type ZoomLevelName = 'domains' | 'mixed' | 'entities';

/** What to call the current level in the toolbar. */
export function zoomLevelName(totalClusters: number, expandedCount: number): ZoomLevelName {
  if (expandedCount === 0) return 'domains';
  if (expandedCount >= totalClusters && totalClusters > 0) return 'entities';
  return 'mixed';
}

/**
 * Where to seed a member node the first time its cluster opens.
 *
 * Members appear on a ring around the bubble they came from, so the bubble
 * visibly becomes its contents instead of the layout jumping. The ring is
 * sized to the bubble and the angle is derived from the member's index so a
 * reopened cluster lands its members in the same places.
 */
export function memberSeedPosition(
  bubbleX: number,
  bubbleY: number,
  memberIndex: number,
  memberTotal: number,
  memberCount: number
): { x: number; y: number } {
  const radius = clusterRadius(memberCount) * 0.9;
  const angle = (memberIndex / Math.max(memberTotal, 1)) * Math.PI * 2;
  return {
    x: bubbleX + Math.cos(angle) * radius,
    y: bubbleY + Math.sin(angle) * radius,
  };
}
