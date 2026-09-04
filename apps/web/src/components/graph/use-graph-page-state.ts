'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { GRAPH_DEFAULTS } from '@/lib/constants/graph';
import { useHierarchicalGraph, useProjects } from '@/lib/hooks';
import { useProjectContext } from '@/lib/project-context';
import type { Theme } from '@/lib/theme';
import { getClusterLabel } from './cluster-legend';
import { addNodeDegrees, createClusterColorMap, getRelatedEntities } from './graph-data';
import type { GraphNode } from './graph-types';
import { buildSemanticGraphData, collectClusters } from './semantic-graph';
import {
  type ClusterExtent,
  countCollapsedInView,
  resolveExpandedClusters,
  type Viewport,
  zoomLevelName,
} from './semantic-zoom';

export function useGraphPageState(theme: Theme) {
  const { selectedProjects } = useProjectContext();
  const { data: projectsData } = useProjects();
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  // Clusters currently showing their members. Zoom drives this set; a reader
  // who opens one by hand pins it so panning away does not shut it.
  const [expandedClusters, setExpandedClusters] = useState<ReadonlySet<string>>(
    () => new Set<string>()
  );
  const [pinnedClusters, setPinnedClusters] = useState<ReadonlySet<string>>(
    () => new Set<string>()
  );
  // Closed bubbles inside the viewport, which is what separates "mixed" from
  // "every domain in view is open" in the level readout.
  const [collapsedInView, setCollapsedInView] = useState(0);
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [includeShared, setIncludeShared] = useState(true);
  const [focusProjects, setFocusProjects] = useState(false);
  const previousSelectedProjectsRef = useRef<string[]>(selectedProjects);

  const sharedProject = useMemo(() => {
    const projects = projectsData?.entities ?? [];
    return projects.find(project => {
      const meta = project.metadata ?? {};
      const slug = typeof meta.slug === 'string' ? meta.slug : '';
      const name = (project.name || '').toLowerCase();
      return Boolean(meta.is_shared) || slug === '_shared' || name === 'shared';
    });
  }, [projectsData?.entities]);
  const sharedProjectId = sharedProject?.id;
  const sharedProjectLabel = sharedProject?.name || 'Shared';
  const hasProjectSelection = selectedProjects.length > 0;

  useEffect(() => {
    if (!hasProjectSelection && focusProjects) {
      setFocusProjects(false);
    }
  }, [focusProjects, hasProjectSelection]);

  useEffect(() => {
    const previous = previousSelectedProjectsRef.current;
    const changed =
      previous.length !== selectedProjects.length ||
      previous.some((projectId, index) => selectedProjects[index] !== projectId);

    if (changed && selectedProjects.length > 0) {
      setFocusProjects(true);
    }

    previousSelectedProjectsRef.current = selectedProjects;
  }, [selectedProjects]);

  const projectFilter = useMemo(() => {
    if (!focusProjects || selectedProjects.length === 0) return undefined;
    const ids = new Set(selectedProjects);
    if (includeShared && sharedProjectId) {
      ids.add(sharedProjectId);
    }
    return Array.from(ids);
  }, [focusProjects, selectedProjects, includeShared, sharedProjectId]);

  const projectKey = projectFilter?.join(',') || 'all';
  const selectedTypesKey = selectedTypes.join(',');
  const filterKey = `${projectKey}:${selectedTypesKey}`;
  const fitKey = `${projectKey}:${selectedTypesKey}`;
  // Expansion deliberately stays out of the render key: remounting the canvas
  // on every level change would throw away the layout that makes a bloom read
  // as one continuous move.
  const graphRenderKey = `${theme}-${projectKey}-${selectedTypesKey}`;

  // Both levels at once. The overview is the domain map, the detail response
  // carries every node's cluster_id, and semantic zoom picks between them per
  // cluster without another round trip.
  const {
    data: detailData,
    isLoading: detailLoading,
    error: graphError,
  } = useHierarchicalGraph({
    max_nodes: GRAPH_DEFAULTS.SEMANTIC_MAX_NODES,
    max_edges: GRAPH_DEFAULTS.MAX_EDGES,
    projects: projectFilter,
    types: selectedTypes.length > 0 ? selectedTypes : undefined,
    resolution: 'detail',
  });
  const { data: overviewData, isLoading: overviewLoading } = useHierarchicalGraph({
    max_nodes: GRAPH_DEFAULTS.MAX_NODES,
    max_edges: GRAPH_DEFAULTS.MAX_EDGES,
    projects: projectFilter,
    types: selectedTypes.length > 0 ? selectedTypes : undefined,
    resolution: 'overview',
  });

  const data = detailData ?? overviewData;
  const isLoading = detailLoading || overviewLoading;
  const hierarchySource = useMemo(
    () => ({ overview: overviewData, detail: detailData }),
    [overviewData, detailData]
  );
  const semanticClusters = useMemo(() => collectClusters(hierarchySource), [hierarchySource]);

  useEffect(() => {
    if (!filterKey) return;
    setSelectedCluster(null);
    setSelectedNodeId(null);
  }, [filterKey]);

  const clusterColorMap = useMemo(() => createClusterColorMap(data?.clusters), [data?.clusters]);
  const allNodesWithDegree = useMemo(() => addNodeDegrees(detailData), [detailData]);

  // d3-force writes positions onto the node objects it is handed, so the same
  // object has to come back on every rebuild for the layout to persist.
  const nodeCacheRef = useRef<Map<string, GraphNode>>(new Map());
  const graphData = useMemo(
    () =>
      buildSemanticGraphData({
        source: hierarchySource,
        expanded: expandedClusters,
        clusterColorMap,
        searchTerm,
        nodeCache: nodeCacheRef.current,
      }),
    [hierarchySource, expandedClusters, clusterColorMap, searchTerm]
  );

  // Filters rebuild the world, so the cached layout no longer describes it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: filterKey is the intentional reset trigger
  useEffect(() => {
    nodeCacheRef.current = new Map();
    setExpandedClusters(new Set());
    setPinnedClusters(new Set());
  }, [filterKey]);

  const handleViewportChange = useCallback(
    (zoom: number, viewport: Viewport | null) => {
      const cache = nodeCacheRef.current;
      const extents: ClusterExtent[] = semanticClusters.map(cluster => {
        const bubble = cache.get(`cluster:${cluster.id}`) ?? cache.get(cluster.id);
        const extent: ClusterExtent = { id: cluster.id, memberCount: cluster.memberCount };
        if (bubble?.x !== undefined) extent.x = bubble.x;
        if (bubble?.y !== undefined) extent.y = bubble.y;
        return extent;
      });

      setExpandedClusters(previous => {
        const next = resolveExpandedClusters({
          clusters: extents,
          zoom,
          viewport,
          previous,
          pinned: pinnedClusters,
        });
        setCollapsedInView(countCollapsedInView(extents, next, viewport));
        if (next.size === previous.size && [...next].every(id => previous.has(id))) {
          return previous;
        }
        return next;
      });
    },
    [semanticClusters, pinnedClusters]
  );

  const zoomLevel = useMemo(
    () => zoomLevelName(expandedClusters.size, collapsedInView),
    [expandedClusters.size, collapsedInView]
  );

  // Bubble clusters arrive named; the tail the map does not draw only has an
  // id, so those borrow the legend's rule and name themselves after their
  // best-connected members.
  const expandedClusterLabels = useMemo(() => {
    const labels = new Map<string, string>();
    for (const cluster of semanticClusters) {
      if (!expandedClusters.has(cluster.id)) continue;
      if (cluster.hasBubble) {
        labels.set(cluster.id, cluster.label);
        continue;
      }
      const meta = data?.clusters.find(item => item.id === cluster.id);
      const derived = meta ? getClusterLabel(meta, allNodesWithDegree) : '';
      if (derived && !derived.startsWith('comm_')) labels.set(cluster.id, derived);
    }
    return labels;
  }, [semanticClusters, expandedClusters, data?.clusters, allNodesWithDegree]);

  useEffect(() => {
    if (!selectedNodeId) return;
    if (graphData.nodes.some(node => node.id === selectedNodeId)) return;
    setSelectedNodeId(null);
  }, [graphData.nodes, selectedNodeId]);

  const pinCluster = useCallback((clusterId: string) => {
    setPinnedClusters(previous => {
      const next = new Set(previous);
      next.add(clusterId);
      return next;
    });
    setExpandedClusters(previous => {
      const next = new Set(previous);
      next.add(clusterId);
      return next;
    });
  }, []);

  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      if (node.aggregate) {
        const clusterId = node.cluster_id || node.id;
        setSelectedNodeId(null);
        setSelectedCluster(clusterId);
        pinCluster(clusterId);
        return;
      }

      const isDeselecting = selectedNodeId === node.id;
      setSelectedNodeId(isDeselecting ? null : node.id);
    },
    [selectedNodeId, pinCluster]
  );

  const handleClusterClick = useCallback(
    (clusterId: string | null) => {
      if (clusterId) {
        setSelectedCluster(clusterId);
        pinCluster(clusterId);
      } else {
        setSelectedCluster(null);
        setPinnedClusters(new Set());
        setExpandedClusters(new Set());
      }
    },
    [pinCluster]
  );

  const clearCluster = useCallback(() => {
    setSelectedCluster(null);
    setPinnedClusters(new Set());
  }, []);
  const closeNodeDetails = useCallback(() => setSelectedNodeId(null), []);
  const resetState = useCallback(() => {
    setSelectedNodeId(null);
    setSelectedCluster(null);
    setPinnedClusters(new Set());
    setExpandedClusters(new Set());
  }, []);

  const selectedNodeRelated = useMemo(
    () => getRelatedEntities(selectedNodeId, graphData),
    [graphData, selectedNodeId]
  );
  const selectedClusterLabel = useMemo(() => {
    if (!selectedCluster || !data?.clusters) return null;
    const cluster = data.clusters.find(item => item.id === selectedCluster);
    if (!cluster) return null;
    return cluster.label || getClusterLabel(cluster, allNodesWithDegree);
  }, [selectedCluster, data?.clusters, allNodesWithDegree]);

  return {
    data,
    isLoading,
    graphError,
    graphData,
    graphRenderKey,
    fitKey,
    filterKey,
    zoomLevel,
    expandedClusters,
    expandedClusterLabels,
    semanticClusters,
    selectedNodeId,
    selectedCluster,
    selectedClusterLabel,
    selectedNodeRelated,
    clusterColorMap,
    allNodesWithDegree,
    searchTerm,
    selectedTypes,
    includeShared,
    focusProjects,
    sharedProjectLabel,
    canToggleShared: Boolean(sharedProjectId && selectedProjects.length > 0 && focusProjects),
    canToggleFocus: hasProjectSelection,
    focusedProjectCount: selectedProjects.length,
    setSearchTerm,
    setSelectedTypes,
    setIncludeShared,
    setFocusProjects,
    handleViewportChange,
    handleNodeClick,
    handleClusterClick,
    clearCluster,
    closeNodeDetails,
    resetState,
  };
}
