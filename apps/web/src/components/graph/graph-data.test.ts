import { describe, expect, it } from 'vitest';
import type { HierarchicalGraphResponse } from '@/lib/api';
import {
  addNodeDegrees,
  buildGraphData,
  createClusterColorMap,
  getRelatedEntities,
} from './graph-data';
import type { GraphData, GraphLink } from './graph-types';

const graphResponse: HierarchicalGraphResponse = {
  nodes: [
    {
      id: 'alpha',
      name: 'Alpha',
      label: 'Alpha',
      type: 'project',
      color: '',
      summary: '',
      cluster_id: 'cluster-a',
    },
    {
      id: 'beta',
      name: 'Beta',
      label: 'Beta',
      type: 'task',
      color: '',
      summary: '',
      cluster_id: 'cluster-b',
    },
    {
      id: 'gamma',
      name: 'Gamma',
      label: 'Gamma',
      type: 'pattern',
      color: '',
      summary: '',
      cluster_id: 'cluster-a',
    },
    {
      id: 'delta',
      name: 'Delta',
      label: 'Delta',
      type: 'note',
      color: '',
      summary: '',
      cluster_id: 'cluster-c',
    },
  ],
  edges: [
    { source: 'alpha', target: 'beta', type: 'contains' },
    { source: 'alpha', target: 'gamma', type: 'relates_to' },
    { source: 'beta', target: 'delta', type: 'blocks' },
  ],
  clusters: [
    {
      id: 'cluster-a',
      member_count: 2,
      level: 0,
      type_distribution: { project: 1, pattern: 1 },
      dominant_type: 'project',
    },
    {
      id: 'cluster-b',
      member_count: 1,
      level: 0,
      type_distribution: { task: 1 },
      dominant_type: 'task',
    },
  ],
  cluster_edges: [],
  total_nodes: 4,
  total_edges: 3,
};

describe('graph data projection', () => {
  it('decorates the full graph without exposing cached edges to canvas mutation', () => {
    const colors = createClusterColorMap(graphResponse.clusters);
    const projected = buildGraphData(graphResponse, null, colors, 'alpha');

    expect(projected.maxDegree).toBe(2);
    expect(projected.matchCount).toBe(1);
    expect(projected.links).toHaveLength(3);
    expect(projected.links[0]).not.toBe(graphResponse.edges[0]);
    expect(projected.links[0]).toMatchObject({ source: 'alpha', target: 'beta' });
    expect(projected.nodes.find(node => node.id === 'alpha')).toMatchObject({
      isProject: true,
      isSearchMatch: true,
      degree: 2,
    });

    projected.links[0].source = projected.nodes[0];
    expect(graphResponse.edges[0].source).toBe('alpha');
    expect(buildGraphData(graphResponse, null, colors, '').links[0].source).toBe('alpha');
  });

  it('keeps a selected cluster and its one-hop context while excluding farther nodes', () => {
    const projected = buildGraphData(
      graphResponse,
      'cluster-a',
      createClusterColorMap(graphResponse.clusters),
      ''
    );

    expect(projected.nodes.map(node => node.id).sort()).toEqual(['alpha', 'beta', 'gamma']);
    expect(projected.links).toHaveLength(2);
    expect(projected.nodes.find(node => node.id === 'beta')?.isNeighbor).toBe(true);
    expect(projected.nodes.find(node => node.id === 'alpha')?.isNeighbor).toBe(false);
    expect(projected.nodes.some(node => node.id === 'delta')).toBe(false);
  });

  it('calculates legend degrees from the complete graph', () => {
    const nodes = addNodeDegrees(graphResponse);

    expect(nodes.find(node => node.id === 'alpha')?.degree).toBe(2);
    expect(nodes.find(node => node.id === 'delta')?.degree).toBe(1);
  });

  it('resolves mutated object and numeric endpoints into deduplicated relationships', () => {
    const nodes = buildGraphData(
      graphResponse,
      null,
      createClusterColorMap(graphResponse.clusters),
      ''
    ).nodes;
    const alpha = nodes.find(node => node.id === 'alpha');
    const betaIndex = nodes.findIndex(node => node.id === 'beta');
    const links = [
      { source: alpha, target: betaIndex, type: 'contains' },
      { source: alpha, target: betaIndex, type: 'duplicate' },
    ] as GraphLink[];
    const graphData: GraphData = { nodes, links, maxDegree: 1, matchCount: 0 };

    expect(getRelatedEntities('alpha', graphData)).toEqual([
      {
        id: 'beta',
        name: 'Beta',
        entity_type: 'task',
        relationship: 'contains',
        direction: 'outgoing',
      },
    ]);
  });
});
