import { describe, expect, it } from 'vitest';
import type { HierarchicalGraphResponse } from '@/lib/api';
import { addNodeDegrees, getRelatedEntities } from './graph-data';
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

describe('graph data helpers', () => {
  it('calculates legend degrees from the complete graph', () => {
    const nodes = addNodeDegrees(graphResponse);

    expect(nodes.find(node => node.id === 'alpha')?.degree).toBe(2);
    expect(nodes.find(node => node.id === 'delta')?.degree).toBe(1);
  });

  it('resolves mutated object and numeric endpoints into deduplicated relationships', () => {
    const nodes = addNodeDegrees(graphResponse);
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
