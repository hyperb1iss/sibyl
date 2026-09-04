import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { HierarchicalGraphResponse } from '@/lib/api';

const mocks = vi.hoisted(() => ({
  useHierarchicalGraph: vi.fn(),
  useProjects: vi.fn(),
  useProjectContext: vi.fn(),
}));

vi.mock('@/lib/hooks', () => ({
  useHierarchicalGraph: mocks.useHierarchicalGraph,
  useProjects: mocks.useProjects,
}));
vi.mock('@/lib/project-context', () => ({
  useProjectContext: mocks.useProjectContext,
}));

import { useGraphPageState } from './use-graph-page-state';

const response: HierarchicalGraphResponse = {
  nodes: [
    {
      id: 'node-a',
      name: 'Node A',
      label: 'Node A',
      type: 'task',
      color: '',
      summary: '',
      cluster_id: 'cluster-a',
    },
  ],
  edges: [],
  clusters: [
    {
      id: 'cluster-a',
      member_count: 1,
      level: 0,
      type_distribution: { task: 1 },
      dominant_type: 'task',
    },
  ],
  cluster_edges: [],
  total_nodes: 1,
  total_edges: 0,
};

const overview: NonNullable<HierarchicalGraphResponse['overview']> = {
  nodes: [
    {
      id: 'cluster:cluster-a',
      name: 'Cluster A',
      label: 'Cluster A',
      type: 'cluster',
      color: '',
      summary: '',
      cluster_id: 'cluster-a',
      aggregate: true,
      member_count: 1,
    },
  ],
  edges: [],
  clusters: response.clusters,
};
const withOverview: HierarchicalGraphResponse = { ...response, overview };

describe('useGraphPageState', () => {
  beforeEach(() => {
    mocks.useProjectContext.mockReturnValue({ selectedProjects: ['project-a'] });
    mocks.useProjects.mockReturnValue({
      data: {
        entities: [
          { id: 'project-a', name: 'Project A', metadata: {} },
          { id: 'shared', name: 'Shared', metadata: { is_shared: true } },
        ],
      },
    });
    mocks.useHierarchicalGraph.mockReturnValue({
      data: withOverview,
      isLoading: false,
      error: null,
    });
  });

  it('keeps all projects as the default and adds shared only in explicit focus mode', async () => {
    const { result } = renderHook(() => useGraphPageState('neon'));

    expect(mocks.useHierarchicalGraph).toHaveBeenCalledWith(
      expect.objectContaining({ projects: undefined, resolution: 'detail' })
    );
    expect(mocks.useHierarchicalGraph).not.toHaveBeenCalledWith(
      expect.objectContaining({ resolution: 'overview' })
    );
    expect(result.current.focusProjects).toBe(false);

    act(() => result.current.setFocusProjects(true));
    await waitFor(() => {
      expect(mocks.useHierarchicalGraph).toHaveBeenCalledWith(
        expect.objectContaining({ projects: ['project-a', 'shared'], resolution: 'detail' })
      );
    });

    act(() => result.current.setIncludeShared(false));
    await waitFor(() => {
      expect(mocks.useHierarchicalGraph).toHaveBeenCalledWith(
        expect.objectContaining({ projects: ['project-a'], resolution: 'detail' })
      );
    });
  });

  it('clears cluster and node focus when entity filters change', async () => {
    const { result } = renderHook(() => useGraphPageState('neon'));

    act(() => result.current.handleClusterClick('cluster-a'));
    await waitFor(() => expect(result.current.selectedCluster).toBe('cluster-a'));
    act(() => result.current.handleNodeClick(response.nodes[0]));
    expect(result.current.selectedNodeId).toBe('node-a');

    act(() => result.current.setSelectedTypes(['task']));
    await waitFor(() => {
      expect(result.current.selectedCluster).toBeNull();
      expect(result.current.selectedNodeId).toBeNull();
    });
  });

  it('opens a cluster the reader clicks and keeps it open across zoom changes', async () => {
    const { result } = renderHook(() => useGraphPageState('neon'));

    act(() => result.current.handleClusterClick('cluster-a'));
    await waitFor(() => expect([...result.current.expandedClusters]).toContain('cluster-a'));

    // Zooming back out to the domain map leaves a pinned cluster open.
    act(() => result.current.handleViewportChange(0.2, null));
    expect([...result.current.expandedClusters]).toContain('cluster-a');

    act(() => result.current.clearCluster());
    act(() => result.current.handleViewportChange(0.2, null));
    await waitFor(() => expect(result.current.expandedClusters.size).toBe(0));
  });

  it('reports the level name from how many clusters are open', async () => {
    const { result } = renderHook(() => useGraphPageState('neon'));

    expect(result.current.zoomLevel).toBe('domains');

    act(() => result.current.handleClusterClick('cluster-a'));
    await waitFor(() => expect(result.current.zoomLevel).not.toBe('domains'));
  });

  it('shows the loading state instead of the previous filter payload', () => {
    mocks.useHierarchicalGraph.mockReturnValue({
      data: withOverview,
      isLoading: false,
      isPlaceholderData: true,
      error: null,
    });
    const { result } = renderHook(() => useGraphPageState('neon'));

    expect(result.current.isLoading).toBe(true);
    expect(result.current.graphData.nodes).toHaveLength(0);
  });

  it('derives the level jumps from the bubbles on the map', () => {
    const { result } = renderHook(() => useGraphPageState('neon'));

    expect(result.current.zoomBounds.entity).toBeGreaterThan(0);
    expect(result.current.zoomBounds.domainCap).toBeLessThan(result.current.zoomBounds.entity ?? 0);
  });

  it('opens a tail cluster by opening the domain that hosts it', async () => {
    mocks.useHierarchicalGraph.mockReturnValue({
      data: {
        ...withOverview,
        nodes: [
          ...response.nodes,
          { ...response.nodes[0], id: 'node-t', name: 'Tail', cluster_id: 'tail' },
        ],
        edges: [{ source: 'node-t', target: 'node-a', type: 'relates_to' }],
        clusters: [...response.clusters, { ...response.clusters[0], id: 'tail' }],
      },
      isLoading: false,
      error: null,
    });
    const { result } = renderHook(() => useGraphPageState('neon'));

    act(() => result.current.handleClusterClick('tail'));
    await waitFor(() => expect([...result.current.expandedClusters]).toEqual(['cluster-a']));
    expect([...result.current.satelliteHosts]).toEqual(['cluster-a']);
    expect(result.current.selectedCluster).toBe('tail');
    expect(result.current.graphData.nodes.map(node => node.id).sort()).toEqual([
      'node-a',
      'node-t',
    ]);
  });
});
