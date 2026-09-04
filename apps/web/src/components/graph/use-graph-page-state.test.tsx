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
    mocks.useHierarchicalGraph.mockReturnValue({ data: response, isLoading: false, error: null });
  });

  it('keeps all projects as the default and adds shared only in explicit focus mode', async () => {
    const { result } = renderHook(() => useGraphPageState('neon'));

    expect(mocks.useHierarchicalGraph).toHaveBeenCalledWith(
      expect.objectContaining({ projects: undefined, resolution: 'detail' })
    );
    expect(mocks.useHierarchicalGraph).toHaveBeenCalledWith(
      expect.objectContaining({ projects: undefined, resolution: 'overview' })
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
});
