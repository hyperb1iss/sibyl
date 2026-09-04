import type {
  HierarchicalCluster,
  HierarchicalGraphResponse,
  RelatedEntitySummary,
} from '@/lib/api';
import { getClusterColor } from '@/lib/constants/graph';
import type { GraphData, GraphLink, GraphNode } from './graph-types';

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
