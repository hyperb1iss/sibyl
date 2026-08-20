import type {
  HierarchicalCluster,
  HierarchicalGraphResponse,
  RelatedEntitySummary,
} from '@/lib/api';
import { getEntityColor } from '@/lib/constants/entities';
import { getClusterColor } from '@/lib/constants/graph';
import type { GraphData, GraphLink, GraphNode } from './graph-types';

const EMPTY_GRAPH_DATA: GraphData = {
  nodes: [],
  links: [],
  maxDegree: 1,
  matchCount: 0,
};

export function createClusterColorMap(clusters: HierarchicalCluster[] | undefined) {
  const colors = new Map<string, string>();
  clusters?.forEach((cluster, index) => {
    colors.set(cluster.id, getClusterColor(cluster.id, index));
  });
  return colors;
}

export function addNodeDegrees(data: HierarchicalGraphResponse | undefined): GraphNode[] {
  if (!data) return [];

  const degreeMap = new Map<string, number>();
  for (const edge of data.edges) {
    degreeMap.set(edge.source, (degreeMap.get(edge.source) || 0) + 1);
    degreeMap.set(edge.target, (degreeMap.get(edge.target) || 0) + 1);
  }

  return data.nodes.map(node => ({
    ...node,
    degree: degreeMap.get(node.id) || 0,
  }));
}

function endpointId(endpoint: unknown): string {
  if (typeof endpoint === 'object' && endpoint !== null && 'id' in endpoint) {
    return String(endpoint.id);
  }
  return String(endpoint);
}

function freshLinks(edges: HierarchicalGraphResponse['edges']): GraphLink[] {
  return edges.map(edge => ({
    ...edge,
    source: endpointId(edge.source),
    target: endpointId(edge.target),
  }));
}

function matchesSearch(node: Pick<GraphNode, 'id' | 'label' | 'name'>, searchTerm: string) {
  if (!searchTerm) return false;
  const term = searchTerm.toLowerCase();
  const name = (node.label || node.name || '').toLowerCase();
  return name.includes(term) || node.id.toLowerCase().includes(term);
}

function decorateNode(
  node: HierarchicalGraphResponse['nodes'][number],
  degree: number,
  clusterColorMap: Map<string, string>,
  searchTerm: string,
  isNeighbor?: boolean
): GraphNode {
  const isProject = node.type === 'project';
  const entityType = node.type || 'unknown';
  const isSearchMatch = matchesSearch(node, searchTerm);
  let zIndex = degree;

  if (isProject) zIndex += 1000;
  else if (entityType === 'task') zIndex += 50;
  else if (entityType === 'pattern') zIndex += 30;
  if (isNeighbor) zIndex -= 500;
  if (isSearchMatch) zIndex += 2000;

  const decorated: GraphNode = {
    ...node,
    clusterColor: clusterColorMap.get(node.cluster_id) || '#8b85a0',
    entityColor: getEntityColor(entityType),
    degree,
    isProject,
    zIndex,
    isSearchMatch,
  };
  if (isNeighbor !== undefined) decorated.isNeighbor = isNeighbor;
  return decorated;
}

export function buildGraphData(
  data: HierarchicalGraphResponse | undefined,
  selectedCluster: string | null,
  clusterColorMap: Map<string, string>,
  searchTerm: string
): GraphData {
  if (!data) return EMPTY_GRAPH_DATA;

  const nodeIdToNode = new Map(data.nodes.map(node => [node.id, node]));

  if (selectedCluster) {
    const clusterNodeIds = new Set(
      data.nodes.filter(node => node.cluster_id === selectedCluster).map(node => node.id)
    );
    const neighborIds = new Set<string>();

    for (const edge of data.edges) {
      const sourceInCluster = clusterNodeIds.has(edge.source);
      const targetInCluster = clusterNodeIds.has(edge.target);
      if (sourceInCluster && !targetInCluster && nodeIdToNode.has(edge.target)) {
        neighborIds.add(edge.target);
      } else if (targetInCluster && !sourceInCluster && nodeIdToNode.has(edge.source)) {
        neighborIds.add(edge.source);
      }
    }

    const visibleIds = new Set([...clusterNodeIds, ...neighborIds]);
    const filteredEdges: HierarchicalGraphResponse['edges'] = [];
    const degreeMap = new Map<string, number>();
    let maxDegree = 1;

    for (const edge of data.edges) {
      if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue;
      filteredEdges.push(edge);
      const sourceDegree = (degreeMap.get(edge.source) || 0) + 1;
      const targetDegree = (degreeMap.get(edge.target) || 0) + 1;
      degreeMap.set(edge.source, sourceDegree);
      degreeMap.set(edge.target, targetDegree);
      maxDegree = Math.max(maxDegree, sourceDegree, targetDegree);
    }

    let matchCount = 0;
    const nodes: GraphNode[] = [];
    for (const id of visibleIds) {
      const node = nodeIdToNode.get(id);
      if (!node) continue;
      const decorated = decorateNode(
        node,
        degreeMap.get(node.id) || 0,
        clusterColorMap,
        searchTerm,
        neighborIds.has(id)
      );
      if (decorated.isSearchMatch) matchCount++;
      nodes.push(decorated);
    }
    nodes.sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));

    return { nodes, links: freshLinks(filteredEdges), maxDegree, matchCount };
  }

  const nodeIds = new Set(data.nodes.map(node => node.id));
  const filteredEdges: HierarchicalGraphResponse['edges'] = [];
  const degreeMap = new Map<string, number>();
  let maxDegree = 1;

  for (const edge of data.edges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) continue;
    filteredEdges.push(edge);
    const sourceDegree = (degreeMap.get(edge.source) || 0) + 1;
    const targetDegree = (degreeMap.get(edge.target) || 0) + 1;
    degreeMap.set(edge.source, sourceDegree);
    degreeMap.set(edge.target, targetDegree);
    maxDegree = Math.max(maxDegree, sourceDegree, targetDegree);
  }

  let matchCount = 0;
  const nodes = data.nodes.map(node => {
    const decorated = decorateNode(node, degreeMap.get(node.id) || 0, clusterColorMap, searchTerm);
    if (decorated.isSearchMatch) matchCount++;
    return decorated;
  });
  nodes.sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));

  return { nodes, links: freshLinks(filteredEdges), maxDegree, matchCount };
}

export function getLinkEndpointId(
  endpoint: GraphLink['source'] | undefined,
  nodes: GraphNode[]
): string | null {
  if (typeof endpoint === 'string') return endpoint;
  if (typeof endpoint === 'number') return nodes[endpoint]?.id ?? null;
  if (endpoint && typeof endpoint === 'object') return endpoint.id;
  return null;
}

export function getRelatedEntities(
  selectedNodeId: string | null,
  graphData: GraphData
): RelatedEntitySummary[] {
  if (!selectedNodeId) return [];

  const nodesById = new Map(graphData.nodes.map(node => [node.id, node]));
  const related: RelatedEntitySummary[] = [];
  const seenIds = new Set<string>();

  for (const edge of graphData.links) {
    const sourceId = getLinkEndpointId(edge.source, graphData.nodes);
    const targetId = getLinkEndpointId(edge.target, graphData.nodes);
    if (sourceId !== selectedNodeId && targetId !== selectedNodeId) continue;

    const otherId = sourceId === selectedNodeId ? targetId : sourceId;
    if (!otherId || seenIds.has(otherId)) continue;

    const otherNode = nodesById.get(otherId);
    if (!otherNode) continue;

    seenIds.add(otherId);
    related.push({
      id: otherId,
      name: otherNode.label || otherNode.name || otherId,
      entity_type: otherNode.type,
      relationship: edge.type,
      direction: sourceId === selectedNodeId ? 'outgoing' : 'incoming',
    });
  }

  return related;
}
