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
  resolveSatelliteHosts,
  type Viewport,
  zoomBoundsFor,
  zoomLevelName,
} from './semantic-zoom';

export function useGraphPageState(theme: Theme) {
  const { selectedProjects } = useProjectContext();
  const { data: projectsData } = useProjects();
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  // Bubble clusters currently showing their members. Zoom drives this set; a
  // reader who opens one by hand pins it so panning away does not shut it.
  const [expandedClusters, setExpandedClusters] = useState<ReadonlySet<string>>(
    () => new Set<string>()
  );
  const [pinnedClusters, setPinnedClusters] = useState<ReadonlySet<string>>(
    () => new Set<string>()
  );
  // Open clusters whose satellite communities are out too: one stage past
  // expansion, so a domain blooms into its members before its long tail.
  const [satelliteHosts, setSatelliteHosts] = useState<ReadonlySet<string>>(
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
  // Expansion deliberately stays out of the render key: remounting the canvas
  // on every level change would throw away the layout that makes a bloom read
  // as one continuous move.
  const graphRenderKey = `${theme}-${projectKey}-${selectedTypesKey}`;

  // One request carries both levels: the detail sample with every node's
  // cluster_id, and inside it the domain map built from the same community
  // run. Fetching the map separately let the two straddle a cache refresh
  // and describe different clusterings, which no client rule can reconcile.
  const {
    data,
    isLoading,
    error: graphError,
  } = useHierarchicalGraph({
    max_nodes: GRAPH_DEFAULTS.SEMANTIC_MAX_NODES,
    max_edges: GRAPH_DEFAULTS.MAX_EDGES,
    projects: projectFilter,
    types: selectedTypes.length > 0 ? selectedTypes : undefined,
    resolution: 'detail',
  });
  const detailData = data;
  const hierarchySource = useMemo(() => {
    const overview = detailData?.overview;
    return {
      overview:
        detailData && overview
          ? {
              ...detailData,
              nodes: overview.nodes,
              edges: overview.edges,
              clusters: overview.clusters,
              resolution: 'overview' as const,
            }
          : undefined,
      detail: detailData,
    };
  }, [detailData]);
  const semanticClusters = useMemo(() => collectClusters(hierarchySource), [hierarchySource]);
  // The unit of expansion: bubbles with something behind them. A bubble whose
  // members never made the detail sample has nothing to open into, so it
  // stays a bubble at every zoom and stays out of the level readout.
  const expandableClusters = useMemo(
    () => semanticClusters.filter(cluster => cluster.hasBubble && cluster.loadedMembers > 0),
    [semanticClusters]
  );
  const zoomBounds = useMemo(() => zoomBoundsFor(expandableClusters), [expandableClusters]);

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
        clusters: semanticClusters,
        expanded: expandedClusters,
        satellites: satelliteHosts,
        clusterColorMap,
        searchTerm,
        nodeCache: nodeCacheRef.current,
      }),
    [
      hierarchySource,
      semanticClusters,
      expandedClusters,
      satelliteHosts,
      clusterColorMap,
      searchTerm,
    ]
  );

  // Filters rebuild the world, so the cached layout no longer describes it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: filterKey is the intentional reset trigger
  useEffect(() => {
    nodeCacheRef.current = new Map();
    setExpandedClusters(new Set());
    setSatelliteHosts(new Set());
    setPinnedClusters(new Set());
  }, [filterKey]);

  // Mirrors for the viewport handler, which runs on every zoom tick and must
  // read the current sets without queuing a state updater to get at them.
  const expandedRef = useRef(expandedClusters);
  const satellitesRef = useRef(satelliteHosts);
  useEffect(() => {
    expandedRef.current = expandedClusters;
    satellitesRef.current = satelliteHosts;
  }, [expandedClusters, satelliteHosts]);

  const sameSet = (a: ReadonlySet<string>, b: ReadonlySet<string>) =>
    a.size === b.size && [...a].every(id => b.has(id));

  const handleViewportChange = useCallback(
    (zoom: number, viewport: Viewport | null) => {
      const cache = nodeCacheRef.current;
      const extents: ClusterExtent[] = expandableClusters.map(cluster => {
        const bubble = cluster.nodeId ? cache.get(cluster.nodeId) : undefined;
        const extent: ClusterExtent = { id: cluster.id, memberCount: cluster.memberCount };
        if (bubble?.x !== undefined) extent.x = bubble.x;
        if (bubble?.y !== undefined) extent.y = bubble.y;
        return extent;
      });

      const previous = expandedRef.current;
      const next = resolveExpandedClusters({
        clusters: extents,
        zoom,
        viewport,
        previous,
        pinned: pinnedClusters,
      });
      setCollapsedInView(countCollapsedInView(extents, next, viewport));
      const nextSatellites = resolveSatelliteHosts({
        clusters: extents,
        zoom,
        viewport,
        expanded: next,
        previous: satellitesRef.current,
        pinned: pinnedClusters,
      });
      if (!sameSet(nextSatellites, satellitesRef.current)) {
        satellitesRef.current = nextSatellites;
        setSatelliteHosts(nextSatellites);
      }
      if (sameSet(next, previous)) return;
      expandedRef.current = next;
      setExpandedClusters(next);
    },
    [expandableClusters, pinnedClusters]
  );

  const expandedCount = useMemo(
    () => expandableClusters.filter(cluster => expandedClusters.has(cluster.id)).length,
    [expandableClusters, expandedClusters]
  );
  // The map is what appears first, so the readout starts there while the
  // responses are still in flight rather than on what an empty graph would
  // be called.
  const zoomLevel = useMemo(
    () =>
      isLoading || !data
        ? 'domains'
        : zoomLevelName(expandedCount, collapsedInView, expandableClusters.length),
    [isLoading, data, expandedCount, collapsedInView, expandableClusters.length]
  );

  const expandedClusterLabels = useMemo(() => {
    const labels = new Map<string, string>();
    for (const cluster of semanticClusters) {
      if (cluster.hasBubble && expandedClusters.has(cluster.id)) {
        labels.set(cluster.id, cluster.label);
      }
    }
    return labels;
  }, [semanticClusters, expandedClusters]);

  useEffect(() => {
    if (!selectedNodeId) return;
    if (graphData.nodes.some(node => node.id === selectedNodeId)) return;
    setSelectedNodeId(null);
  }, [graphData.nodes, selectedNodeId]);

  // Pins land on bubble clusters. Opening a tail cluster from the legend
  // means opening the domain it lives in.
  const pinCluster = useCallback(
    (clusterId: string) => {
      const cluster = semanticClusters.find(item => item.id === clusterId);
      const target = cluster?.host ?? clusterId;
      setPinnedClusters(previous => {
        const next = new Set(previous);
        next.add(target);
        return next;
      });
      setExpandedClusters(previous => {
        const next = new Set(previous);
        next.add(target);
        expandedRef.current = next;
        return next;
      });
      setSatelliteHosts(previous => {
        const next = new Set(previous);
        next.add(target);
        satellitesRef.current = next;
        return next;
      });
    },
    [semanticClusters]
  );

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
        setSatelliteHosts(new Set());
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
    setSatelliteHosts(new Set());
  }, []);

  const selectedNodeRelated = useMemo(
    () => getRelatedEntities(selectedNodeId, graphData),
    [graphData, selectedNodeId]
  );
  const selectedClusterLabel = useMemo(() => {
    if (!selectedCluster) return null;
    const cluster = data?.clusters.find(item => item.id === selectedCluster);
    if (cluster) return cluster.label || getClusterLabel(cluster, allNodesWithDegree);
    return semanticClusters.find(item => item.id === selectedCluster)?.label ?? null;
  }, [selectedCluster, data?.clusters, allNodesWithDegree, semanticClusters]);

  return {
    data,
    isLoading,
    graphError,
    graphData,
    graphRenderKey,
    filterKey,
    zoomLevel,
    zoomBounds,
    expandedClusters,
    satelliteHosts,
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
