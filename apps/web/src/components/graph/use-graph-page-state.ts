'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GraphResolution } from '@/lib/api';
import { GRAPH_DEFAULTS } from '@/lib/constants/graph';
import { useHierarchicalGraph, useProjects } from '@/lib/hooks';
import { useProjectContext } from '@/lib/project-context';
import type { Theme } from '@/lib/theme';
import { getClusterLabel } from './cluster-legend';
import {
  addNodeDegrees,
  buildGraphData,
  createClusterColorMap,
  getRelatedEntities,
} from './graph-data';
import type { GraphNode } from './graph-types';

export function useGraphPageState(theme: Theme) {
  const { selectedProjects } = useProjectContext();
  const { data: projectsData } = useProjects();
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [graphResolution, setGraphResolution] = useState<GraphResolution>('detail');
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
  const fitKey = `${graphResolution}:${projectKey}:${selectedTypesKey}:${selectedCluster ?? 'all'}`;
  const graphRenderKey = `${theme}-${graphResolution}-${projectKey}-${selectedTypesKey}-${selectedCluster || 'all'}`;

  const {
    data,
    isLoading,
    error: graphError,
  } = useHierarchicalGraph({
    max_nodes: GRAPH_DEFAULTS.MAX_NODES,
    max_edges: GRAPH_DEFAULTS.MAX_EDGES,
    projects: projectFilter,
    types: selectedTypes.length > 0 ? selectedTypes : undefined,
    resolution: graphResolution,
    cluster_id: selectedCluster ?? undefined,
  });

  useEffect(() => {
    if (!filterKey) return;
    setSelectedCluster(null);
    setSelectedNodeId(null);
  }, [filterKey]);

  const initialResolutionAppliedRef = useRef(false);
  useEffect(() => {
    if (initialResolutionAppliedRef.current) return;
    const recommended = data?.recommended_resolution;
    if (!recommended) return;
    initialResolutionAppliedRef.current = true;
    if (recommended !== graphResolution && !selectedCluster) {
      setGraphResolution(recommended);
    }
  }, [data?.recommended_resolution, graphResolution, selectedCluster]);

  const clusterColorMap = useMemo(() => createClusterColorMap(data?.clusters), [data?.clusters]);
  const allNodesWithDegree = useMemo(() => addNodeDegrees(data), [data]);
  const graphData = useMemo(
    () => buildGraphData(data, selectedCluster, clusterColorMap, searchTerm),
    [data, selectedCluster, clusterColorMap, searchTerm]
  );

  useEffect(() => {
    if (!selectedNodeId) return;
    if (graphData.nodes.some(node => node.id === selectedNodeId)) return;
    setSelectedNodeId(null);
  }, [graphData.nodes, selectedNodeId]);

  const handleResolutionChange = useCallback((next: GraphResolution) => {
    setGraphResolution(next);
    if (next === 'overview') {
      setSelectedCluster(null);
      setSelectedNodeId(null);
    }
  }, []);

  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      if (node.aggregate) {
        setSelectedNodeId(null);
        setSelectedCluster(node.cluster_id || null);
        setGraphResolution('detail');
        return;
      }

      const isDeselecting = selectedNodeId === node.id;
      setSelectedNodeId(isDeselecting ? null : node.id);
    },
    [selectedNodeId]
  );

  const handleClusterClick = useCallback((clusterId: string | null) => {
    if (clusterId) {
      setSelectedCluster(clusterId);
      setGraphResolution('detail');
    } else {
      setSelectedCluster(null);
      setGraphResolution('overview');
    }
  }, []);

  const clearCluster = useCallback(() => setSelectedCluster(null), []);
  const closeNodeDetails = useCallback(() => setSelectedNodeId(null), []);
  const resetState = useCallback(() => {
    setSelectedNodeId(null);
    setSelectedCluster(null);
    setGraphResolution('detail');
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
    graphResolution,
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
    handleResolutionChange,
    handleNodeClick,
    handleClusterClick,
    clearCluster,
    closeNodeDetails,
    resetState,
  };
}
