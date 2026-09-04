/**
 * Semantic zoom: which level of the graph hierarchy is visible at a given
 * zoom, for each cluster independently.
 *
 * Geometric zoom makes everything bigger. Semantic zoom trades a summary for
 * its contents once there is room to read them, so pushing in past a domain
 * bubble replaces it with the entities it stands for while the rest of the
 * map stays summarized.
 *
 * The rule is stated in screen pixels: a bubble opens when it paints large
 * enough that its members would be legible in its place. That makes the
 * decision per cluster and about apparent size rather than the zoom number
 * alone: a 900-member domain earns its detail sooner than a 12-member one
 * because its bubble covers more of the screen at the same zoom. Clusters far
 * outside the viewport stay collapsed however far in the camera is, which is
 * what keeps a push into one corner from paying for the whole graph.
 */

/** Radius of a cluster bubble in graph units. The layout and the paint share it. */
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
   * Largest a bubble may paint at the domain fit, as a fraction of the
   * opening radius. The fit is capped there so a small graph on a large
   * screen does not open its biggest domain before the reader has seen the
   * map.
   */
  DOMAIN_FIT_FRACTION: 0.6,
  /**
   * How much further past its opening radius a domain has to grow before the
   * small communities hanging off it come out too. Opening them with the
   * domain's own members turns the first bloom into a wall; one stage later
   * they read as detail on detail.
   */
  SATELLITE_FACTOR: 1.6,
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

/** Zooms that produce each level, derived from the bubbles on the map. */
export interface ZoomBounds {
  /** Zoom at which every bubble has crossed its opening radius, or null with no bubbles. */
  entity: number | null;
  /** Highest zoom the domain fit may land on without opening a bubble. */
  domainCap: number;
}

export function withinViewport(
  cluster: ClusterExtent,
  viewport: Viewport,
  radius: number
): boolean {
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
  /** Screen pixels per graph unit: the d3-zoom scale factor. */
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

export interface ResolveSatellitesArgs {
  clusters: ClusterExtent[];
  zoom: number;
  viewport: Viewport | null;
  /** Bubble clusters currently open. Satellites never show under a closed bubble. */
  expanded: ReadonlySet<string>;
  /** Hosts whose satellites were open on the previous frame, for hysteresis. */
  previous: ReadonlySet<string>;
  /** Pinned clusters show everything they have. */
  pinned?: ReadonlySet<string>;
}

/**
 * The open clusters whose satellite communities should be drawn as well.
 *
 * Same shape as the expansion rule, one stage further in: a host's
 * satellites open once its bubble would have painted at SATELLITE_FACTOR
 * times the opening radius.
 */
export function resolveSatelliteHosts({
  clusters,
  zoom,
  viewport,
  expanded,
  previous,
  pinned,
}: ResolveSatellitesArgs): Set<string> {
  const hosts = new Set<string>();
  for (const cluster of clusters) {
    if (!expanded.has(cluster.id)) continue;
    if (pinned?.has(cluster.id)) {
      hosts.add(cluster.id);
      continue;
    }
    const screenRadius = clusterRadius(cluster.memberCount) * zoom;
    const threshold =
      (previous.has(cluster.id)
        ? SEMANTIC_ZOOM.COLLAPSE_SCREEN_RADIUS
        : SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS) * SEMANTIC_ZOOM.SATELLITE_FACTOR;
    if (screenRadius < threshold) continue;
    if (viewport && !withinViewport(cluster, viewport, clusterRadius(cluster.memberCount))) {
      continue;
    }
    hosts.add(cluster.id);
  }
  return hosts;
}

/**
 * Zooms that produce each level for this set of bubbles.
 *
 * The entities zoom is where the smallest bubble crosses its opening radius,
 * with a little margin so the jump lands past the threshold rather than on
 * it. The domain cap keeps the largest bubble under its opening radius at
 * the fit.
 */
export function zoomBoundsFor(clusters: ClusterExtent[]): ZoomBounds {
  if (clusters.length === 0) return { entity: null, domainCap: Number.POSITIVE_INFINITY };
  let smallest = Number.POSITIVE_INFINITY;
  let largest = 0;
  for (const cluster of clusters) {
    const radius = clusterRadius(cluster.memberCount);
    smallest = Math.min(smallest, radius);
    largest = Math.max(largest, radius);
  }
  return {
    entity: (SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS / smallest) * 1.02,
    domainCap: (SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS * SEMANTIC_ZOOM.DOMAIN_FIT_FRACTION) / largest,
  };
}

export type ZoomLevelName = 'domains' | 'mixed' | 'entities';

/**
 * What to call the current level in the toolbar.
 *
 * Entities means every bubble the reader can see has opened, not every
 * bubble in the graph: expansion is viewport scoped on purpose, so the whole
 * graph is never open at once and a definition that demanded it would never
 * be met.
 */
export function zoomLevelName(
  expandedCount: number,
  collapsedInView: number,
  bubbleCount: number
): ZoomLevelName {
  // A graph too small to form any domain has nothing to summarize: every
  // node on it is an entity, whatever the zoom.
  if (bubbleCount === 0) return 'entities';
  if (expandedCount === 0) return 'domains';
  if (collapsedInView === 0) return 'entities';
  return 'mixed';
}

/** Bubbles still closed inside the viewport, for the level readout. */
export function countCollapsedInView(
  clusters: ClusterExtent[],
  expanded: ReadonlySet<string>,
  viewport: Viewport | null
): number {
  let count = 0;
  for (const cluster of clusters) {
    if (expanded.has(cluster.id)) continue;
    if (viewport && !withinViewport(cluster, viewport, clusterRadius(cluster.memberCount))) {
      continue;
    }
    count += 1;
  }
  return count;
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
