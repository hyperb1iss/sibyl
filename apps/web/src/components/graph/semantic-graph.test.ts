import { describe, expect, it } from 'vitest';
import type { HierarchicalGraphResponse } from '@/lib/api';
import type { GraphNode } from './graph-types';
import { buildSemanticGraphData, collectClusters, type HierarchySource } from './semantic-graph';

type ResponseNode = HierarchicalGraphResponse['nodes'][number];

function bubble(clusterId: string, memberCount: number, label = clusterId): ResponseNode {
  return {
    id: `cluster:${clusterId}`,
    name: label,
    label,
    type: 'cluster',
    cluster_id: clusterId,
    aggregate: true,
    member_count: memberCount,
  } as ResponseNode;
}

function member(id: string, clusterId: string, type = 'task'): ResponseNode {
  return { id, name: id, label: id, type, cluster_id: clusterId } as ResponseNode;
}

function response(
  nodes: ResponseNode[],
  edges: Array<{ source: string; target: string; type?: string }>,
  clusters: Array<{ id: string; member_count: number; label?: string }> = []
): HierarchicalGraphResponse {
  return {
    nodes,
    edges: edges.map(edge => ({ ...edge, type: edge.type ?? 'relates_to' })),
    clusters,
    cluster_edges: [],
    total_nodes: nodes.length,
    total_edges: edges.length,
    displayed_nodes: nodes.length,
    displayed_edges: edges.length,
    resolution: 'detail',
  } as unknown as HierarchicalGraphResponse;
}

const COLORS = new Map<string, string>([
  ['a', '#ff0000'],
  ['b', '#00ff00'],
]);

function source(): HierarchySource {
  return {
    overview: response(
      [bubble('a', 900, 'Sibyl'), bubble('b', 40, 'Hypercolor')],
      [{ source: 'cluster:a', target: 'cluster:b' }]
    ),
    detail: response(
      [member('a1', 'a'), member('a2', 'a'), member('b1', 'b'), member('loose', 'tail')],
      [
        { source: 'a1', target: 'a2' },
        { source: 'a1', target: 'b1' },
        { source: 'loose', target: 'a1' },
      ],
      [
        { id: 'a', member_count: 900 },
        { id: 'b', member_count: 40 },
        { id: 'tail', member_count: 1 },
      ]
    ),
  };
}

function build(expanded: string[], nodeCache = new Map<string, GraphNode>()) {
  return buildSemanticGraphData({
    source: source(),
    expanded: new Set(expanded),
    clusterColorMap: COLORS,
    searchTerm: '',
    nodeCache,
  });
}

describe('collectClusters', () => {
  it('reports bubble clusters and the tail the map does not draw', () => {
    expect(collectClusters(source())).toEqual([
      { id: 'a', memberCount: 900, label: 'Sibyl', loadedMembers: 2, hasBubble: true },
      { id: 'b', memberCount: 40, label: 'Hypercolor', loadedMembers: 1, hasBubble: true },
      { id: 'tail', memberCount: 1, label: 'tail', loadedMembers: 1, hasBubble: false },
    ]);
  });
});

describe('buildSemanticGraphData', () => {
  it('draws only bubbles when every cluster is collapsed', () => {
    const graph = build([]);
    const ids = graph.nodes.map(node => node.id).sort();

    expect(ids).toEqual(['cluster:a', 'cluster:b']);
    expect(graph.links).toHaveLength(1);
  });

  it('holds back the tail the domain map does not draw a bubble for', () => {
    expect(build([]).nodes.map(node => node.id)).not.toContain('loose');
    expect(build(['tail']).nodes.map(node => node.id)).toContain('loose');
  });

  it('replaces a bubble with its members when the cluster opens', () => {
    const graph = build(['a']);
    const ids = graph.nodes.map(node => node.id).sort();

    expect(ids).toEqual(['a1', 'a2', 'cluster:b']);
    expect(ids).not.toContain('cluster:a');
  });

  it('routes an edge that crosses a level boundary to the standing bubble', () => {
    const graph = build(['a']);
    const pairs = graph.links.map(link => [link.source, link.target].sort().join('>')).sort();

    // a1-a2 is a real edge now and a1-b1 lands on b's bubble; the tail member
    // is still closed, so its edge has no visible end to attach to.
    expect(pairs).toEqual(['a1>a2', 'a1>cluster:b']);
  });

  it('collapses many crossing edges into one edge per bubble', () => {
    const wide: HierarchySource = {
      overview: response([bubble('a', 5), bubble('b', 5)], []),
      detail: response(
        [member('a1', 'a'), member('a2', 'a'), member('b1', 'b'), member('b2', 'b')],
        [
          { source: 'a1', target: 'b1' },
          { source: 'a1', target: 'b2' },
          { source: 'a2', target: 'b1' },
        ],
        [
          { id: 'a', member_count: 5 },
          { id: 'b', member_count: 5 },
        ]
      ),
    };

    const graph = buildSemanticGraphData({
      source: wide,
      expanded: new Set(['a']),
      clusterColorMap: COLORS,
      searchTerm: '',
      nodeCache: new Map(),
    });

    const toBubble = graph.links.filter(link =>
      [link.source, link.target].includes('cluster:b' as never)
    );
    expect(toBubble).toHaveLength(2);
  });

  it('keeps node objects across rebuilds so the layout survives', () => {
    const cache = new Map<string, GraphNode>();
    const first = build(['a'], cache);
    const a1 = first.nodes.find(node => node.id === 'a1');
    if (!a1) throw new Error('expected a1');
    a1.x = 42;
    a1.y = -17;

    const second = build(['a'], cache);
    const again = second.nodes.find(node => node.id === 'a1');

    expect(again).toBe(a1);
    expect(again?.x).toBe(42);
  });

  it('seeds new members around the bubble they came from', () => {
    const cache = new Map<string, GraphNode>();
    const collapsed = build([], cache);
    const bubbleNode = collapsed.nodes.find(node => node.id === 'cluster:a');
    if (!bubbleNode) throw new Error('expected bubble');
    bubbleNode.x = 300;
    bubbleNode.y = 120;

    const expanded = build(['a'], cache);
    const members = expanded.nodes.filter(node => node.id === 'a1' || node.id === 'a2');

    expect(members).toHaveLength(2);
    for (const node of members) {
      expect(Math.hypot((node.x ?? 0) - 300, (node.y ?? 0) - 120)).toBeLessThan(60);
    }
  });

  it('re-seeds from the bubble current position after a collapse', () => {
    const cache = new Map<string, GraphNode>();
    build([], cache);
    const first = cache.get('cluster:a');
    if (!first) throw new Error('expected bubble');
    first.x = 0;
    first.y = 0;

    build(['a'], cache);
    // The reader pans away and the bubble settles somewhere else.
    build([], cache);
    first.x = 900;
    first.y = 900;
    const reopened = build(['a'], cache);

    const a1 = reopened.nodes.find(node => node.id === 'a1');
    expect(Math.hypot((a1?.x ?? 0) - 900, (a1?.y ?? 0) - 900)).toBeLessThan(60);
  });

  it('degrees and marks search matches on whatever level is showing', () => {
    const graph = buildSemanticGraphData({
      source: source(),
      expanded: new Set(['a']),
      clusterColorMap: COLORS,
      searchTerm: 'a1',
      nodeCache: new Map(),
    });

    expect(graph.matchCount).toBe(1);
    const a1 = graph.nodes.find(node => node.id === 'a1');
    expect(a1?.isSearchMatch).toBe(true);
    expect(a1?.degree).toBe(2);
    expect(graph.maxDegree).toBe(2);
  });

  it('returns an empty graph when neither level has loaded', () => {
    const graph = buildSemanticGraphData({
      source: { overview: undefined, detail: undefined },
      expanded: new Set(),
      clusterColorMap: COLORS,
      searchTerm: '',
      nodeCache: new Map(),
    });

    expect(graph.nodes).toHaveLength(0);
    expect(graph.links).toHaveLength(0);
  });
});
