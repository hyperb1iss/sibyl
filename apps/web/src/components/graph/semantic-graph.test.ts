import { describe, expect, it } from 'vitest';
import type { HierarchicalGraphResponse } from '@/lib/api';
import type { GraphNode } from './graph-types';
import {
  buildSemanticGraphData,
  collectClusters,
  type HierarchySource,
  LOOSE_CLUSTER_ID,
  LOOSE_NODE_ID,
} from './semantic-graph';

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

/**
 * Two named domains plus two tail communities: `tail` links into domain a,
 * `island` links to nothing on the map.
 */
function source(): HierarchySource {
  return {
    overview: response(
      [bubble('a', 900, 'Sibyl'), bubble('b', 40, 'Hypercolor')],
      [{ source: 'cluster:a', target: 'cluster:b' }]
    ),
    detail: response(
      [
        member('a1', 'a'),
        member('a2', 'a'),
        member('b1', 'b'),
        member('loose', 'tail'),
        member('lonely', 'island'),
      ],
      [
        { source: 'a1', target: 'a2' },
        { source: 'a1', target: 'b1' },
        { source: 'loose', target: 'a1' },
      ],
      [
        { id: 'a', member_count: 900 },
        { id: 'b', member_count: 40 },
        { id: 'tail', member_count: 1 },
        { id: 'island', member_count: 1 },
      ]
    ),
  };
}

function build(
  expanded: string[],
  nodeCache = new Map<string, GraphNode>(),
  satellites: string[] = expanded
) {
  const hierarchy = source();
  return buildSemanticGraphData({
    source: hierarchy,
    clusters: collectClusters(hierarchy),
    expanded: new Set(expanded),
    satellites: new Set(satellites),
    clusterColorMap: COLORS,
    searchTerm: '',
    nodeCache,
  });
}

function ids(graph: { nodes: GraphNode[] }): string[] {
  return graph.nodes.map(node => node.id).sort();
}

describe('collectClusters', () => {
  it('lists bubbles first, then a loose-ends bubble, then the hosted tail', () => {
    expect(collectClusters(source())).toEqual([
      {
        id: 'a',
        memberCount: 900,
        label: 'Sibyl',
        loadedMembers: 2,
        hasBubble: true,
        nodeId: 'cluster:a',
      },
      {
        id: 'b',
        memberCount: 40,
        label: 'Hypercolor',
        loadedMembers: 1,
        hasBubble: true,
        nodeId: 'cluster:b',
      },
      {
        id: LOOSE_CLUSTER_ID,
        memberCount: 1,
        label: 'Loose ends',
        loadedMembers: 1,
        hasBubble: true,
        nodeId: LOOSE_NODE_ID,
      },
      { id: 'tail', memberCount: 1, label: 'tail', loadedMembers: 1, hasBubble: false, host: 'a' },
      {
        id: 'island',
        memberCount: 1,
        label: 'island',
        loadedMembers: 1,
        hasBubble: false,
        host: LOOSE_CLUSTER_ID,
      },
    ]);
  });

  it('hosts a tail cluster on the domain its members link to most', () => {
    const hierarchy: HierarchySource = {
      overview: response([bubble('a', 5), bubble('b', 5)], []),
      detail: response(
        [member('a1', 'a'), member('b1', 'b'), member('b2', 'b'), member('t1', 'tail')],
        [
          { source: 't1', target: 'a1' },
          { source: 't1', target: 'b1' },
          { source: 't1', target: 'b2' },
        ],
        [{ id: 'tail', member_count: 1 }]
      ),
    };

    const tail = collectClusters(hierarchy).find(cluster => cluster.id === 'tail');
    expect(tail?.host).toBe('b');
  });

  it('adds no loose-ends bubble when every tail has a host', () => {
    const hierarchy: HierarchySource = {
      overview: response([bubble('a', 5)], []),
      detail: response(
        [member('a1', 'a'), member('t1', 'tail')],
        [{ source: 't1', target: 'a1' }],
        [{ id: 'tail', member_count: 1 }]
      ),
    };

    expect(collectClusters(hierarchy).map(cluster => cluster.id)).toEqual(['a', 'tail']);
  });
});

describe('buildSemanticGraphData', () => {
  it('draws only bubbles when every cluster is collapsed', () => {
    const graph = build([]);

    expect(ids(graph)).toEqual(['cluster:a', 'cluster:b', LOOSE_NODE_ID].sort());
    expect(graph.links).toHaveLength(1);
  });

  it('opens a hosted tail cluster one stage after its domain', () => {
    expect(ids(build([]))).not.toContain('loose');
    // Domain open, satellites still folded: the tail waits and its edge into
    // the domain waits with it.
    const membersOnly = build(['a'], new Map(), []);
    expect(ids(membersOnly)).not.toContain('loose');
    expect(membersOnly.links.map(link => `${link.source}>${link.target}`)).not.toContain(
      'loose>a1'
    );
    expect(ids(build(['a'], new Map(), ['a']))).toContain('loose');
  });

  it('opens the loose-ends bubble straight into its communities', () => {
    expect(ids(build([LOOSE_CLUSTER_ID], new Map(), []))).toContain('lonely');
  });

  it('replaces the loose-ends bubble with the members no domain claims', () => {
    expect(ids(build([]))).not.toContain('lonely');
    const graph = build([LOOSE_CLUSTER_ID]);
    expect(ids(graph)).toContain('lonely');
    expect(ids(graph)).not.toContain(LOOSE_NODE_ID);
  });

  it('replaces a bubble with its members when the cluster opens', () => {
    const graph = build(['a']);

    expect(ids(graph)).toEqual(['a1', 'a2', 'cluster:b', LOOSE_NODE_ID, 'loose'].sort());
  });

  it('routes an edge that crosses a level boundary to the standing bubble', () => {
    const graph = build(['a']);
    const pairs = graph.links.map(link => [link.source, link.target].sort().join('>')).sort();

    // a1-a2 and a1-loose are real edges now; a1-b1 lands on b's bubble.
    expect(pairs).toEqual(['a1>a2', 'a1>cluster:b', 'a1>loose']);
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
      clusters: collectClusters(wide),
      expanded: new Set(['a']),
      satellites: new Set(),
      clusterColorMap: COLORS,
      searchTerm: '',
      nodeCache: new Map(),
    });

    const toBubble = graph.links.filter(link =>
      [link.source, link.target].includes('cluster:b' as never)
    );
    expect(toBubble).toHaveLength(2);
  });

  it('refreshes what the payload says about a cached node', () => {
    const cache = new Map<string, GraphNode>();
    build([], cache);
    const bubbleNode = cache.get('cluster:a');
    if (!bubbleNode) throw new Error('expected bubble');
    bubbleNode.x = 5;
    bubbleNode.y = 7;

    const renamed = source();
    const aggregate = renamed.overview?.nodes.find(node => node.id === 'cluster:a');
    if (!aggregate) throw new Error('expected aggregate');
    aggregate.member_count = 12;
    aggregate.label = 'Sibyl (filtered)';
    const graph = buildSemanticGraphData({
      source: renamed,
      clusters: collectClusters(renamed),
      expanded: new Set(),
      satellites: new Set(),
      clusterColorMap: COLORS,
      searchTerm: '',
      nodeCache: cache,
    });

    const again = graph.nodes.find(node => node.id === 'cluster:a');
    expect(again).toBe(bubbleNode);
    expect(again?.member_count).toBe(12);
    expect(again?.label).toBe('Sibyl (filtered)');
    expect(again?.x).toBe(5);
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

  it('seeds new members, tail included, around the bubble they came from', () => {
    const cache = new Map<string, GraphNode>();
    const collapsed = build([], cache);
    const bubbleNode = collapsed.nodes.find(node => node.id === 'cluster:a');
    if (!bubbleNode) throw new Error('expected bubble');
    bubbleNode.x = 300;
    bubbleNode.y = 120;

    const expanded = build(['a'], cache);
    const members = expanded.nodes.filter(node => ['a1', 'a2', 'loose'].includes(node.id));

    expect(members).toHaveLength(3);
    for (const node of members) {
      expect(Math.hypot((node.x ?? 0) - 300, (node.y ?? 0) - 120)).toBeLessThan(60);
      expect(node.clusterAnchor).toEqual({ x: 300, y: 120 });
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
    const hierarchy = source();
    const graph = buildSemanticGraphData({
      source: hierarchy,
      clusters: collectClusters(hierarchy),
      expanded: new Set(['a']),
      satellites: new Set(['a']),
      clusterColorMap: COLORS,
      searchTerm: 'a1',
      nodeCache: new Map(),
    });

    expect(graph.matchCount).toBe(1);
    const a1 = graph.nodes.find(node => node.id === 'a1');
    expect(a1?.isSearchMatch).toBe(true);
    expect(a1?.degree).toBe(3);
    expect(graph.maxDegree).toBe(3);
  });

  it('returns an empty graph when neither level has loaded', () => {
    const graph = buildSemanticGraphData({
      source: { overview: undefined, detail: undefined },
      clusters: [],
      expanded: new Set(),
      satellites: new Set(),
      clusterColorMap: COLORS,
      searchTerm: '',
      nodeCache: new Map(),
    });

    expect(graph.nodes).toHaveLength(0);
    expect(graph.links).toHaveLength(0);
  });
});
