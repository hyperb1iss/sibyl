import type { HierarchicalGraphResponse } from '@/lib/api';
import { getEntityColor } from '@/lib/constants/entities';
import type { GraphData, GraphLink, GraphNode } from './graph-types';
import { memberSeedPosition } from './semantic-zoom';

/**
 * Assembling one canvas dataset from both levels of the hierarchy.
 *
 * The two levels arrive as separate responses: aggregate bubbles from the
 * overview resolution, entity nodes carrying `cluster_id` from the detail
 * resolution. A collapsed cluster contributes its bubble, an expanded one
 * contributes its members, and the picture stays connected because an edge
 * that crosses a level boundary is routed to whichever bubble stands in for
 * the hidden end.
 *
 * The overview only draws bubbles for the domains worth naming. The long
 * tail of small communities has no bubble of its own, so each tail cluster
 * is attached to the domain its members link to most and opens along with
 * it; a tail cluster linked to no domain at all belongs to a synthetic
 * "Loose ends" bubble, so nothing in the response is unreachable by zoom.
 *
 * Node objects are reused across rebuilds through a cache. d3-force writes
 * position and velocity onto the objects it is given, so handing back the
 * same object for an unchanged node keeps the layout where the reader left
 * it; a rebuild only moves what actually changed level.
 */

type ResponseNode = HierarchicalGraphResponse['nodes'][number];

export const LOOSE_CLUSTER_ID = '__loose';
export const LOOSE_NODE_ID = 'cluster:__loose';
export const LOOSE_LABEL = 'Loose ends';

export interface HierarchySource {
  overview: HierarchicalGraphResponse | undefined;
  detail: HierarchicalGraphResponse | undefined;
}

export interface SemanticCluster {
  id: string;
  memberCount: number;
  label: string;
  /** Members present in the detail sample, which can be fewer than memberCount. */
  loadedMembers: number;
  /**
   * Whether the map draws a bubble for this cluster. Bubble clusters are the
   * unit of expansion; a tail cluster without one opens with its host.
   */
  hasBubble: boolean;
  /** Id of the aggregate node drawn while a bubble cluster is collapsed. */
  nodeId?: string;
  /** For a tail cluster, the bubble cluster it opens with. */
  host?: string;
}

const EMPTY: GraphData = { nodes: [], links: [], maxDegree: 1, matchCount: 0 };

function matchesSearch(node: { id: string; label?: string; name?: string }, term: string): boolean {
  if (!term) return false;
  const needle = term.toLowerCase();
  const name = (node.label || node.name || '').toLowerCase();
  return name.includes(needle) || node.id.toLowerCase().includes(needle);
}

function looseBubble(memberCount: number): ResponseNode {
  return {
    id: LOOSE_NODE_ID,
    name: LOOSE_LABEL,
    label: LOOSE_LABEL,
    type: 'cluster',
    color: '',
    summary: 'Small communities with no link to a named domain',
    cluster_id: LOOSE_CLUSTER_ID,
    aggregate: true,
    member_count: memberCount,
  };
}

/** Every cluster the canvas can show, bubbles first. */
export function collectClusters(source: HierarchySource): SemanticCluster[] {
  const members = source.detail?.nodes ?? [];
  const loaded = new Map<string, number>();
  const clusterOf = new Map<string, string>();
  for (const node of members) {
    const clusterId = node.cluster_id;
    if (!clusterId) continue;
    clusterOf.set(node.id, clusterId);
    loaded.set(clusterId, (loaded.get(clusterId) ?? 0) + 1);
  }

  const clusters: SemanticCluster[] = [];
  const bubbles = new Set<string>();

  for (const node of source.overview?.nodes ?? []) {
    if (!node.aggregate) continue;
    const id = node.cluster_id || node.id;
    if (bubbles.has(id)) continue;
    bubbles.add(id);
    clusters.push({
      id,
      memberCount: node.member_count || loaded.get(id) || 1,
      label: node.label || node.name || id,
      loadedMembers: loaded.get(id) ?? 0,
      hasBubble: true,
      nodeId: node.id,
    });
  }

  // A tail cluster is hosted by the bubble its members link to most.
  const votes = new Map<string, Map<string, number>>();
  for (const edge of source.detail?.edges ?? []) {
    const a = clusterOf.get(String(edge.source));
    const b = clusterOf.get(String(edge.target));
    if (!a || !b || a === b) continue;
    for (const [tail, bubble] of [
      [a, b],
      [b, a],
    ]) {
      if (bubbles.has(tail) || !bubbles.has(bubble)) continue;
      const tally = votes.get(tail) ?? new Map<string, number>();
      tally.set(bubble, (tally.get(bubble) ?? 0) + 1);
      votes.set(tail, tally);
    }
  }

  const tails: SemanticCluster[] = [];
  const seen = new Set(bubbles);
  let looseMembers = 0;
  const addTail = (id: string, memberCount: number, label: string) => {
    if (seen.has(id)) return;
    seen.add(id);
    let host = LOOSE_CLUSTER_ID;
    let best = 0;
    for (const [bubble, count] of votes.get(id) ?? []) {
      if (count > best) {
        best = count;
        host = bubble;
      }
    }
    const loadedMembers = loaded.get(id) ?? 0;
    if (host === LOOSE_CLUSTER_ID) looseMembers += loadedMembers;
    tails.push({ id, memberCount, label, loadedMembers, hasBubble: false, host });
  };

  for (const meta of source.detail?.clusters ?? []) {
    addTail(meta.id, meta.member_count || loaded.get(meta.id) || 1, meta.label || meta.id);
  }
  for (const [id, count] of loaded) addTail(id, count, id);

  if (looseMembers > 0) {
    clusters.push({
      id: LOOSE_CLUSTER_ID,
      memberCount: looseMembers,
      label: LOOSE_LABEL,
      loadedMembers: looseMembers,
      hasBubble: true,
      nodeId: LOOSE_NODE_ID,
    });
  }

  return clusters.concat(tails);
}

interface BuildArgs {
  source: HierarchySource;
  clusters: SemanticCluster[];
  /** Bubble clusters currently showing their members. */
  expanded: ReadonlySet<string>;
  /** Open clusters also showing the tail communities attached to them. */
  satellites: ReadonlySet<string>;
  clusterColorMap: Map<string, string>;
  searchTerm: string;
  /** Reused across rebuilds so d3 keeps positions for nodes that did not change level. */
  nodeCache: Map<string, GraphNode>;
}

export function buildSemanticGraphData({
  source,
  clusters,
  expanded,
  satellites,
  clusterColorMap,
  searchTerm,
  nodeCache,
}: BuildArgs): GraphData {
  const { overview, detail } = source;
  if (!overview && !detail) return EMPTY;

  const bubbleByCluster = new Map<string, ResponseNode>();
  for (const node of overview?.nodes ?? []) {
    if (!node.aggregate) continue;
    bubbleByCluster.set(node.cluster_id || node.id, node);
  }
  const hostOf = new Map<string, string>();
  for (const cluster of clusters) {
    if (cluster.host) hostOf.set(cluster.id, cluster.host);
    if (cluster.id === LOOSE_CLUSTER_ID && !bubbleByCluster.has(LOOSE_CLUSTER_ID)) {
      bubbleByCluster.set(LOOSE_CLUSTER_ID, looseBubble(cluster.memberCount));
    }
  }
  // The bubble cluster a node opens with: its own, or its host's for a tail
  // member. A node with no cluster at all has nothing that could stand in
  // for it, so it is always drawn.
  const levelClusterOf = (node: ResponseNode): string | undefined => {
    const clusterId = node.cluster_id;
    if (!clusterId) return undefined;
    return hostOf.get(clusterId) ?? clusterId;
  };
  // A domain's own members come out when it opens; the tail communities
  // attached to it wait for the satellite stage. The loose-ends bubble has
  // nothing but satellites, so opening it shows them at once.
  const isDrawn = (node: ResponseNode, levelCluster: string): boolean => {
    if (!expanded.has(levelCluster)) return false;
    if (levelCluster === node.cluster_id || levelCluster === LOOSE_CLUSTER_ID) return true;
    return satellites.has(levelCluster);
  };

  // Which concrete node stands in for each entity at this level: itself when
  // its cluster is open, otherwise the bubble that covers it.
  const standIn = new Map<string, string>();
  const visibleNodeIds = new Set<string>();
  const drawnPerCluster = new Map<string, number>();
  const members = detail?.nodes ?? [];

  for (const node of members) {
    const levelCluster = levelClusterOf(node);
    if (!levelCluster || isDrawn(node, levelCluster)) {
      standIn.set(node.id, node.id);
      visibleNodeIds.add(node.id);
      if (levelCluster) {
        drawnPerCluster.set(levelCluster, (drawnPerCluster.get(levelCluster) ?? 0) + 1);
      }
      continue;
    }
    // Hidden behind a closed bubble, the bubble stands in for it. Hidden
    // behind an open domain whose satellites are still folded, nothing does:
    // its edges wait for the next stage along with it.
    if (expanded.has(levelCluster)) continue;
    const bubble = bubbleByCluster.get(levelCluster);
    if (bubble) {
      standIn.set(node.id, bubble.id);
      visibleNodeIds.add(bubble.id);
    }
  }

  // A collapsed cluster whose members never made the detail sample still gets
  // its bubble: the summary is all there is to show for it.
  for (const [clusterId, bubble] of bubbleByCluster) {
    if (!expanded.has(clusterId)) visibleNodeIds.add(bubble.id);
  }

  const degree = new Map<string, number>();
  const linkKeys = new Set<string>();
  const links: GraphLink[] = [];

  function addLink(sourceId: string, targetId: string, type: string) {
    if (sourceId === targetId) return;
    const key = sourceId < targetId ? `${sourceId} ${targetId}` : `${targetId} ${sourceId}`;
    if (linkKeys.has(key)) return;
    linkKeys.add(key);
    links.push({ source: sourceId, target: targetId, type } as GraphLink);
    degree.set(sourceId, (degree.get(sourceId) ?? 0) + 1);
    degree.set(targetId, (degree.get(targetId) ?? 0) + 1);
  }

  for (const edge of detail?.edges ?? []) {
    const sourceId = standIn.get(String(edge.source));
    const targetId = standIn.get(String(edge.target));
    if (!sourceId || !targetId) continue;
    if (!visibleNodeIds.has(sourceId) || !visibleNodeIds.has(targetId)) continue;
    addLink(sourceId, targetId, edge.type);
  }

  // Cluster-level edges only carry information while both ends are still
  // bubbles; once a cluster opens, its members' own edges say it better.
  for (const edge of overview?.edges ?? []) {
    const sourceId = String(edge.source);
    const targetId = String(edge.target);
    if (!visibleNodeIds.has(sourceId) || !visibleNodeIds.has(targetId)) continue;
    addLink(sourceId, targetId, edge.type);
  }

  let maxDegree = 1;
  for (const value of degree.values()) maxDegree = Math.max(maxDegree, value);

  const nodes: GraphNode[] = [];
  let matchCount = 0;

  function emit(
    raw: ResponseNode,
    options: { seed?: { x: number; y: number }; anchor?: { x: number; y: number } }
  ) {
    const cached = nodeCache.get(raw.id);
    const nodeDegree = degree.get(raw.id) ?? 0;
    const isProject = raw.type === 'project';
    const isSearchMatch = matchesSearch(raw, searchTerm);
    if (isSearchMatch) matchCount++;

    let zIndex = nodeDegree;
    if (raw.aggregate) zIndex += 500;
    if (isProject) zIndex += 1000;
    else if (raw.type === 'task') zIndex += 50;
    if (isSearchMatch) zIndex += 2000;

    // A cached object keeps its position and velocity; everything the
    // response says about the node is refreshed, so a bubble that survives a
    // rebuild paints the count and name the current payload gives it.
    const node: GraphNode = cached ? Object.assign(cached, raw) : { ...raw };
    if (options.anchor) node.clusterAnchor = options.anchor;
    node.degree = nodeDegree;
    node.clusterColor = clusterColorMap.get(raw.cluster_id) || '#8b85a0';
    node.entityColor = raw.aggregate
      ? clusterColorMap.get(raw.cluster_id || raw.id) || '#8b85a0'
      : getEntityColor(raw.type || 'unknown');
    node.isProject = isProject;
    node.isSearchMatch = isSearchMatch;
    node.zIndex = zIndex;

    if (!cached) {
      if (options.seed) {
        node.x = options.seed.x;
        node.y = options.seed.y;
      }
      nodeCache.set(raw.id, node);
    }

    nodes.push(node);
  }

  for (const [clusterId, bubble] of bubbleByCluster) {
    if (expanded.has(clusterId)) continue;
    if (!visibleNodeIds.has(bubble.id)) continue;
    emit(bubble, {});
  }

  const seedIndex = new Map<string, number>();
  for (const raw of members) {
    if (!visibleNodeIds.has(raw.id)) continue;
    const levelCluster = levelClusterOf(raw);
    const bubble = levelCluster ? bubbleByCluster.get(levelCluster) : undefined;
    const cachedBubble = bubble ? nodeCache.get(bubble.id) : undefined;

    let seed: { x: number; y: number } | undefined;
    let anchor: { x: number; y: number } | undefined;
    if (levelCluster && cachedBubble?.x !== undefined && cachedBubble.y !== undefined) {
      const index = seedIndex.get(levelCluster) ?? 0;
      seedIndex.set(levelCluster, index + 1);
      anchor = { x: cachedBubble.x, y: cachedBubble.y };
      seed = memberSeedPosition(
        cachedBubble.x,
        cachedBubble.y,
        index,
        drawnPerCluster.get(levelCluster) ?? 1,
        bubble?.member_count || 1
      );
    }

    emit(raw, { ...(seed ? { seed } : {}), ...(anchor ? { anchor } : {}) });
  }

  nodes.sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));

  // Drop cache entries for members that left the canvas, so a cluster reopened
  // after the layout moved blooms from the bubble's new home rather than its
  // old one. Bubbles stay cached even while expanded: their last position is
  // exactly what the next bloom seeds from.
  const live = new Set(nodes.map(node => node.id));
  const bubbleNodeIds = new Set([...bubbleByCluster.values()].map(node => node.id));
  for (const id of nodeCache.keys()) {
    if (!live.has(id) && !bubbleNodeIds.has(id)) nodeCache.delete(id);
  }

  return { nodes, links, maxDegree, matchCount };
}
