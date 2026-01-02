'use client';

import * as d3Force from 'd3-force';
import dynamic from 'next/dynamic';
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import { EntityDetailPanel } from '@/components/graph/entity-detail-panel';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { Card } from '@/components/ui/card';
import { GraphEmptyState } from '@/components/ui/empty-state';
import {
  Check,
  ChevronDown,
  ChevronUp,
  Filter,
  Focus,
  Loader2,
  Maximize2,
  Minimize2,
  MinusCircle,
  PlusCircle,
  RotateCcw,
  Search,
  X,
} from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import type { HierarchicalCluster, HierarchicalEdge, HierarchicalNode } from '@/lib/api';
import { ENTITY_TYPES, GRAPH_DEFAULTS, getClusterColor, getEntityColor } from '@/lib/constants';
import { useHierarchicalGraph } from '@/lib/hooks';
import { useProjectContext } from '@/lib/project-context';
import { useTheme } from '@/lib/theme';

// Canvas requires hex colors - OKLCH CSS vars don't work directly
const CANVAS_COLORS = {
  neon: { bg: '#0a0812', fgPrimary: '#fafaf5', fgMuted: '#9b93b8' },
  dawn: { bg: '#f1ecff', fgPrimary: '#2b2540', fgMuted: '#8e84a8' },
};

// Dynamic import to avoid SSR issues with canvas
const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-base">
      <div className="text-sc-fg-muted">Loading graph...</div>
    </div>
  ),
});

// Extended node type for force graph
interface GraphNode extends HierarchicalNode {
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
  clusterColor?: string;
  entityColor?: string; // Color based on entity type
  degree?: number; // Number of connections (for sizing)
  isProject?: boolean; // Projects are STARS in our galaxy!
  isNeighbor?: boolean; // Context node from 1-hop expansion (render dimmed)
  isSearchMatch?: boolean; // Matches current search term
  zIndex?: number; // Render order (higher = on top, gets label priority)
  __highlightTime?: number; // For pulse animation
}

// d3-force mutates source/target from string IDs to node objects at runtime
interface GraphLink extends Omit<HierarchicalEdge, 'source' | 'target'> {
  source: string | GraphNode;
  target: string | GraphNode;
  sourceNode?: GraphNode;
  targetNode?: GraphNode;
}

export interface KnowledgeGraphRef {
  zoomIn: () => void;
  zoomOut: () => void;
  fitView: () => void;
  resetView: () => void;
}

// Mobile bottom sheet for entity details
function MobileEntitySheet({ entityId, onClose }: { entityId: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 md:hidden">
      <button
        type="button"
        className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default"
        onClick={onClose}
        onKeyDown={e => e.key === 'Escape' && onClose()}
        aria-label="Close panel"
      />
      <div className="absolute bottom-0 left-0 right-0 max-h-[70vh] bg-sc-bg-base rounded-t-2xl overflow-hidden animate-slide-up">
        <div className="flex justify-center py-2">
          <div className="w-10 h-1 bg-sc-fg-subtle/30 rounded-full" />
        </div>
        <EntityDetailPanel entityId={entityId} onClose={onClose} variant="sheet" />
      </div>
    </div>
  );
}

// Generate a descriptive label for a cluster from its top nodes
function getClusterLabel(cluster: HierarchicalCluster, nodes: GraphNode[]): string {
  // Find nodes belonging to this cluster, sorted by degree (most connected first)
  const clusterNodes = nodes
    .filter(n => n.cluster_id === cluster.id)
    .sort((a, b) => (b.degree || 0) - (a.degree || 0));

  if (clusterNodes.length === 0) {
    return cluster.dominant_type?.replace(/_/g, ' ') || 'Mixed';
  }

  // Get top 2 node names as the cluster label
  const topNames = clusterNodes
    .slice(0, 2)
    .map(n => {
      const name = n.label || n.name || '';
      // Truncate long names
      return name.length > 15 ? `${name.slice(0, 12)}...` : name;
    })
    .filter(Boolean);

  if (topNames.length === 0) {
    return cluster.dominant_type?.replace(/_/g, ' ') || 'Mixed';
  }

  return topNames.join(', ');
}

// Cluster legend component
function ClusterLegend({
  clusters,
  clusterColorMap,
  selectedCluster,
  onClusterClick,
  nodes,
}: {
  clusters: HierarchicalCluster[];
  clusterColorMap: Map<string, string>;
  selectedCluster: string | null;
  onClusterClick: (clusterId: string | null) => void;
  nodes: GraphNode[];
}) {
  const [expanded, setExpanded] = useState(true);

  if (clusters.length === 0) return null;

  return (
    <Card className="!p-0 max-w-xs">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
      >
        <span>Clusters ({clusters.length})</span>
        {expanded ? <ChevronUp width={14} height={14} /> : <ChevronDown width={14} height={14} />}
      </button>
      {expanded && (
        <div className="px-3 pb-3 space-y-1 max-h-48 overflow-y-auto">
          <button
            type="button"
            onClick={() => onClusterClick(null)}
            className={`w-full flex items-center gap-2 px-2 py-1 rounded text-xs transition-colors ${
              selectedCluster === null
                ? 'bg-sc-purple/20 text-sc-purple'
                : 'text-sc-fg-muted hover:text-sc-fg-primary'
            }`}
          >
            <div className="w-2 h-2 rounded-full bg-gradient-to-r from-sc-purple to-sc-cyan" />
            <span>All clusters</span>
          </button>
          {[...clusters]
            .sort((a, b) => b.member_count - a.member_count)
            .map(cluster => {
              const color = clusterColorMap.get(cluster.id) || '#8b85a0';
              const isSelected = selectedCluster === cluster.id;
              const label = getClusterLabel(cluster, nodes);
              return (
                <button
                  key={cluster.id}
                  type="button"
                  onClick={() => onClusterClick(cluster.id)}
                  className={`w-full flex items-center gap-2 px-2 py-1 rounded text-xs transition-colors ${
                    isSelected
                      ? 'bg-sc-purple/20 text-sc-fg-primary'
                      : 'text-sc-fg-muted hover:text-sc-fg-primary'
                  }`}
                  title={label}
                >
                  <div
                    className="w-2 h-2 rounded-full flex-shrink-0"
                    style={{ backgroundColor: color }}
                  />
                  <span className="truncate">{label}</span>
                  <span className="ml-auto text-sc-fg-subtle flex-shrink-0">
                    {cluster.member_count}
                  </span>
                </button>
              );
            })}
        </div>
      )}
    </Card>
  );
}

// Entity type labels for the UI (prettier versions)
const ENTITY_TYPE_LABELS: Record<string, string> = {
  task: 'Tasks',
  project: 'Projects',
  epic: 'Epics',
  pattern: 'Patterns',
  episode: 'Episodes',
  topic: 'Topics',
  concept: 'Concepts',
  rule: 'Rules',
  template: 'Templates',
  convention: 'Conventions',
  tool: 'Tools',
  language: 'Languages',
  source: 'Sources',
  document: 'Documents',
  file: 'Files',
  function: 'Functions',
  error_pattern: 'Errors',
  milestone: 'Milestones',
  team: 'Teams',
};

// Stats overlay - shows real totals and displayed counts
function StatsOverlay({
  totalNodes,
  totalEdges,
  displayedNodes,
  displayedEdges,
  clusterCount,
}: {
  totalNodes: number;
  totalEdges: number;
  displayedNodes: number;
  displayedEdges: number;
  clusterCount: number;
}) {
  const showingAll = displayedNodes >= totalNodes;

  return (
    <div className="absolute top-4 right-4 z-10 bg-sc-bg-elevated/90 backdrop-blur-sm rounded-xl p-4 border border-sc-purple/20 min-w-[140px] hidden md:block">
      <div className="text-sm text-sc-fg-muted">Total Nodes</div>
      <div className="text-2xl font-bold text-sc-purple">{totalNodes.toLocaleString()}</div>
      {!showingAll && (
        <div className="text-xs text-sc-fg-subtle">showing {displayedNodes.toLocaleString()}</div>
      )}
      <div className="text-sm text-sc-fg-muted mt-2">Total Edges</div>
      <div className="text-2xl font-bold text-sc-cyan">{totalEdges.toLocaleString()}</div>
      {!showingAll && displayedEdges < totalEdges && (
        <div className="text-xs text-sc-fg-subtle">showing {displayedEdges.toLocaleString()}</div>
      )}
      <div className="text-sm text-sc-fg-muted mt-2">Clusters</div>
      <div className="text-2xl font-bold text-sc-coral">{clusterCount}</div>
    </div>
  );
}

// Unified graph toolbar - zoom, search, filters
function GraphToolbar({
  onZoomIn,
  onZoomOut,
  onFitView,
  onReset,
  isFullscreen,
  onToggleFullscreen,
  searchTerm,
  onSearchChange,
  selectedTypes,
  onTypesChange,
  matchCount,
  nodeCount,
  edgeCount,
}: {
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitView: () => void;
  onReset: () => void;
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
  searchTerm: string;
  onSearchChange: (term: string) => void;
  selectedTypes: string[];
  onTypesChange: (types: string[]) => void;
  matchCount: number;
  nodeCount: number;
  edgeCount: number;
}) {
  const [typeDropdownOpen, setTypeDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close dropdown on outside click
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setTypeDropdownOpen(false);
      }
    }
    if (typeDropdownOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [typeDropdownOpen]);

  const toggleType = (type: string) => {
    if (selectedTypes.includes(type)) {
      onTypesChange(selectedTypes.filter(t => t !== type));
    } else {
      onTypesChange([...selectedTypes, type]);
    }
  };

  const clearTypes = () => onTypesChange([]);

  const primaryTypes = ['task', 'project', 'epic', 'pattern', 'episode', 'topic', 'concept'];
  const secondaryTypes = ENTITY_TYPES.filter(t => !primaryTypes.includes(t));

  return (
    <>
      {/* Mobile compact toolbar */}
      <div className="absolute top-2 left-2 right-2 z-10 flex items-center gap-2 md:hidden">
        <div className="flex-1 flex items-center justify-center gap-3 text-xs bg-sc-bg-base/90 rounded-lg px-3 py-2 border border-sc-fg-subtle/20">
          <span>
            <span className="text-sc-purple font-medium">{nodeCount}</span>
            <span className="text-sc-fg-subtle ml-1">nodes</span>
          </span>
          <span>
            <span className="text-sc-cyan font-medium">{edgeCount}</span>
            <span className="text-sc-fg-subtle ml-1">edges</span>
          </span>
        </div>
        <button
          type="button"
          onClick={onToggleFullscreen}
          className="p-2.5 rounded-lg bg-sc-bg-base/90 text-sc-fg-subtle hover:text-sc-fg-primary border border-sc-fg-subtle/20 transition-colors"
        >
          {isFullscreen ? (
            <Minimize2 width={18} height={18} />
          ) : (
            <Maximize2 width={18} height={18} />
          )}
        </button>
      </div>

      {/* Desktop unified toolbar */}
      <div className="absolute top-4 left-4 z-10 hidden md:block">
        <Card className="!p-1.5 flex items-center gap-2">
          {/* Zoom controls */}
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={onZoomIn}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Zoom in"
            >
              <PlusCircle width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onZoomOut}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Zoom out"
            >
              <MinusCircle width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onFitView}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Fit to view"
            >
              <Focus width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onReset}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Reset view"
            >
              <RotateCcw width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onToggleFullscreen}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            >
              {isFullscreen ? (
                <Minimize2 width={16} height={16} />
              ) : (
                <Maximize2 width={16} height={16} />
              )}
            </button>
          </div>

          {/* Divider */}
          <div className="w-px h-5 bg-sc-fg-subtle/20" />

          {/* Search input */}
          <div className="relative">
            <Search
              width={14}
              height={14}
              className="absolute left-2 top-1/2 -translate-y-1/2 text-sc-fg-subtle"
            />
            <input
              type="text"
              placeholder="Search nodes..."
              value={searchTerm}
              onChange={e => onSearchChange(e.target.value)}
              className="pl-7 pr-7 py-1 w-44 text-xs bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg focus:border-sc-purple/50 focus:outline-none text-sc-fg-primary placeholder:text-sc-fg-subtle"
            />
            {searchTerm && (
              <button
                type="button"
                onClick={() => onSearchChange('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-sc-fg-subtle hover:text-sc-fg-primary"
              >
                <X width={12} height={12} />
              </button>
            )}
          </div>

          {/* Search result count */}
          {searchTerm && (
            <span className="text-xs text-sc-fg-muted whitespace-nowrap">
              {matchCount}/{nodeCount}
            </span>
          )}

          {/* Divider */}
          <div className="w-px h-5 bg-sc-fg-subtle/20" />

          {/* Entity type filter dropdown */}
          <div ref={dropdownRef} className="relative">
            <button
              type="button"
              onClick={() => setTypeDropdownOpen(!typeDropdownOpen)}
              className={`flex items-center gap-1.5 px-2 py-1 text-xs rounded-lg transition-colors ${
                selectedTypes.length > 0
                  ? 'bg-sc-purple/10 text-sc-purple'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              }`}
            >
              <Filter width={14} height={14} />
              <span>Types</span>
              {selectedTypes.length > 0 && (
                <span className="px-1 rounded bg-sc-purple/20 text-[10px]">
                  {selectedTypes.length}
                </span>
              )}
              <ChevronDown
                width={12}
                height={12}
                className={`transition-transform ${typeDropdownOpen ? 'rotate-180' : ''}`}
              />
            </button>

            {typeDropdownOpen && (
              <div className="absolute top-full left-0 mt-1 w-56 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl shadow-lg overflow-hidden z-50 animate-fade-in">
                {selectedTypes.length > 0 && (
                  <>
                    <button
                      type="button"
                      onClick={clearTypes}
                      className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-elevated transition-colors"
                    >
                      <X width={12} height={12} />
                      Clear filter
                    </button>
                    <div className="border-t border-sc-fg-subtle/10" />
                  </>
                )}
                <div className="max-h-64 overflow-y-auto p-2 space-y-1">
                  {primaryTypes.map(type => {
                    const isSelected = selectedTypes.includes(type);
                    const color = getEntityColor(type);
                    return (
                      <button
                        key={type}
                        type="button"
                        onClick={() => toggleType(type)}
                        className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs transition-colors ${
                          isSelected
                            ? 'bg-sc-purple/10 text-sc-fg-primary'
                            : 'text-sc-fg-muted hover:bg-sc-bg-elevated hover:text-sc-fg-primary'
                        }`}
                      >
                        <div
                          className="w-3 h-3 rounded-full flex-shrink-0"
                          style={{ backgroundColor: color }}
                        />
                        <span className="flex-1 text-left">{ENTITY_TYPE_LABELS[type] || type}</span>
                        {isSelected && <Check width={14} height={14} className="text-sc-purple" />}
                      </button>
                    );
                  })}
                  <div className="border-t border-sc-fg-subtle/10 my-1" />
                  {secondaryTypes.map(type => {
                    const isSelected = selectedTypes.includes(type);
                    const color = getEntityColor(type);
                    return (
                      <button
                        key={type}
                        type="button"
                        onClick={() => toggleType(type)}
                        className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs transition-colors ${
                          isSelected
                            ? 'bg-sc-purple/10 text-sc-fg-primary'
                            : 'text-sc-fg-muted hover:bg-sc-bg-elevated hover:text-sc-fg-primary'
                        }`}
                      >
                        <div
                          className="w-3 h-3 rounded-full flex-shrink-0"
                          style={{ backgroundColor: color }}
                        />
                        <span className="flex-1 text-left">{ENTITY_TYPE_LABELS[type] || type}</span>
                        {isSelected && <Check width={14} height={14} className="text-sc-purple" />}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </Card>
      </div>

      {/* Mobile zoom controls (bottom) */}
      <div className="absolute bottom-4 right-4 z-10 flex md:hidden">
        <Card className="!p-1 flex items-center gap-1">
          <button
            type="button"
            onClick={onZoomOut}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <MinusCircle width={20} height={20} />
          </button>
          <button
            type="button"
            onClick={onFitView}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <Focus width={20} height={20} />
          </button>
          <button
            type="button"
            onClick={onZoomIn}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <PlusCircle width={20} height={20} />
          </button>
        </Card>
      </div>
    </>
  );
}

function GraphPageContent() {
  const { theme } = useTheme();
  const colors = CANVAS_COLORS[theme];
  const { selectedProjects } = useProjectContext();
  const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  // Filter state
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);

  // Fetch hierarchical graph data with up to 1000 nodes
  // Filter by selected projects and entity types
  const {
    data,
    isLoading,
    error: _error,
  } = useHierarchicalGraph({
    max_nodes: GRAPH_DEFAULTS.MAX_NODES,
    max_edges: GRAPH_DEFAULTS.MAX_EDGES,
    projects: selectedProjects.length > 0 ? selectedProjects : undefined,
    types: selectedTypes.length > 0 ? selectedTypes : undefined,
  });

  // Build cluster color map
  const clusterColorMap = useMemo(() => {
    const map = new Map<string, string>();
    if (data?.clusters) {
      data.clusters.forEach((cluster, index) => {
        map.set(cluster.id, getClusterColor(cluster.id, index));
      });
    }
    return map;
  }, [data?.clusters]);

  // Calculate degree for ALL nodes (used by legend, unaffected by cluster filter)
  const allNodesWithDegree = useMemo(() => {
    if (!data) return [];

    const degreeMap = new Map<string, number>();
    for (const edge of data.edges) {
      degreeMap.set(edge.source, (degreeMap.get(edge.source) || 0) + 1);
      degreeMap.set(edge.target, (degreeMap.get(edge.target) || 0) + 1);
    }

    return data.nodes.map(node => ({
      ...node,
      degree: degreeMap.get(node.id) || 0,
    })) as GraphNode[];
  }, [data]);

  // Search matching helper
  const matchesSearch = useCallback(
    (node: { name?: string; label?: string; id: string }) => {
      if (!searchTerm) return false;
      const term = searchTerm.toLowerCase();
      const name = (node.label || node.name || '').toLowerCase();
      return name.includes(term) || node.id.toLowerCase().includes(term);
    },
    [searchTerm]
  );

  // Transform data for force graph with entity coloring and degree-based sizing
  // When a cluster is selected, include 1-hop neighbors for context
  const graphData = useMemo(() => {
    if (!data) return { nodes: [], links: [], maxDegree: 1, matchCount: 0 };

    // Build node ID set (also filters by cluster if selected)
    const clusterNodeIds = new Set<string>();
    const nodeIdToNode = new Map<string, (typeof data.nodes)[0]>();

    // Index all nodes
    for (const n of data.nodes) {
      nodeIdToNode.set(n.id, n);
    }

    if (selectedCluster) {
      // Collect cluster nodes
      for (const n of data.nodes) {
        if (n.cluster_id === selectedCluster) {
          clusterNodeIds.add(n.id);
        }
      }

      // Find 1-hop neighbors (nodes connected to cluster)
      const neighborIds = new Set<string>();
      for (const edge of data.edges) {
        const srcInCluster = clusterNodeIds.has(edge.source);
        const tgtInCluster = clusterNodeIds.has(edge.target);
        if (srcInCluster && !tgtInCluster && nodeIdToNode.has(edge.target)) {
          neighborIds.add(edge.target);
        } else if (tgtInCluster && !srcInCluster && nodeIdToNode.has(edge.source)) {
          neighborIds.add(edge.source);
        }
      }

      // Combine: cluster nodes + neighbors
      const allVisibleIds = new Set([...clusterNodeIds, ...neighborIds]);

      // Filter edges: include if at least one endpoint is in cluster
      const filteredEdges: typeof data.edges = [];
      const degreeMap = new Map<string, number>();
      let maxDegree = 1;

      for (const edge of data.edges) {
        if (allVisibleIds.has(edge.source) && allVisibleIds.has(edge.target)) {
          filteredEdges.push(edge);
          const srcDeg = (degreeMap.get(edge.source) || 0) + 1;
          const tgtDeg = (degreeMap.get(edge.target) || 0) + 1;
          degreeMap.set(edge.source, srcDeg);
          degreeMap.set(edge.target, tgtDeg);
          if (srcDeg > maxDegree) maxDegree = srcDeg;
          if (tgtDeg > maxDegree) maxDegree = tgtDeg;
        }
      }

      // Build nodes array with neighbor flag and search matching
      const graphNodes: GraphNode[] = [];
      let matchCount = 0;
      for (const id of allVisibleIds) {
        const node = nodeIdToNode.get(id);
        if (!node) continue;
        const degree = degreeMap.get(node.id) || 0;
        const isProject = node.type === 'project';
        const entityType = node.type || 'unknown';
        const isNeighbor = neighborIds.has(id);
        const isSearchMatch = matchesSearch(node);
        if (isSearchMatch) matchCount++;

        let zIndex = degree;
        if (isProject) zIndex += 1000;
        else if (entityType === 'task') zIndex += 50;
        else if (entityType === 'pattern') zIndex += 30;
        if (isNeighbor) zIndex -= 500; // Neighbors render behind cluster nodes
        if (isSearchMatch) zIndex += 2000; // Search matches render on top

        graphNodes.push({
          ...node,
          clusterColor: clusterColorMap.get(node.cluster_id) || '#8b85a0',
          entityColor: getEntityColor(entityType),
          degree,
          isProject,
          zIndex,
          isNeighbor, // Mark as neighbor for dimmed rendering
          isSearchMatch,
        } as GraphNode);
      }

      graphNodes.sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));
      return { nodes: graphNodes, links: filteredEdges, maxDegree, matchCount };
    }

    // No cluster filter - show all nodes
    const nodeIds = new Set<string>();
    for (const n of data.nodes) nodeIds.add(n.id);

    // Single pass: filter edges AND calculate degrees
    const degreeMap = new Map<string, number>();
    const filteredEdges: typeof data.edges = [];
    let maxDegree = 1;

    for (const edge of data.edges) {
      if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) {
        filteredEdges.push(edge);
        const srcDeg = (degreeMap.get(edge.source) || 0) + 1;
        const tgtDeg = (degreeMap.get(edge.target) || 0) + 1;
        degreeMap.set(edge.source, srcDeg);
        degreeMap.set(edge.target, tgtDeg);
        if (srcDeg > maxDegree) maxDegree = srcDeg;
        if (tgtDeg > maxDegree) maxDegree = tgtDeg;
      }
    }

    // Transform nodes with entity colors, degree, z-index, and search matching
    const graphNodes: GraphNode[] = new Array(data.nodes.length);
    let matchCount = 0;
    for (let i = 0; i < data.nodes.length; i++) {
      const node = data.nodes[i];
      const degree = degreeMap.get(node.id) || 0;
      const isProject = node.type === 'project';
      const entityType = node.type || 'unknown';
      const isSearchMatch = matchesSearch(node);
      if (isSearchMatch) matchCount++;

      // z-index for rendering order (higher = on top)
      let zIndex = degree;
      if (isProject) zIndex += 1000;
      else if (entityType === 'task') zIndex += 50;
      else if (entityType === 'pattern') zIndex += 30;
      if (isSearchMatch) zIndex += 2000; // Search matches render on top

      graphNodes[i] = {
        ...node,
        clusterColor: clusterColorMap.get(node.cluster_id) || '#8b85a0',
        entityColor: getEntityColor(entityType),
        degree,
        isProject,
        zIndex,
        isSearchMatch,
      };
    }

    // Sort by zIndex so important nodes render last (on top)
    graphNodes.sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));

    return { nodes: graphNodes, links: filteredEdges, maxDegree, matchCount };
  }, [data, selectedCluster, clusterColorMap, matchesSearch]);

  // Configure d3 forces
  useEffect(() => {
    if (!graphRef.current) return;

    graphRef.current.d3Force(
      'charge',
      d3Force.forceManyBody().strength(GRAPH_DEFAULTS.CHARGE_STRENGTH)
    );
    graphRef.current.d3Force(
      'center',
      d3Force.forceCenter().strength(GRAPH_DEFAULTS.CENTER_STRENGTH)
    );
    graphRef.current.d3Force(
      'collision',
      d3Force.forceCollide().radius(GRAPH_DEFAULTS.COLLISION_RADIUS)
    );

    // Link force with distance - ForceFn has [key: string]: any so we can access distance directly
    const linkForce = graphRef.current.d3Force('link');
    if (linkForce && typeof linkForce.distance === 'function') {
      linkForce.distance(GRAPH_DEFAULTS.LINK_DISTANCE);
    }
  }, []);

  // Clean node rendering - entity colors + degree-based sizing
  // Labels scale with zoom: more labels appear as you zoom in
  const paintNode = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = node.x || 0;
      const y = node.y || 0;
      const isSelected = node.id === selectedNodeId;
      const isHovered = node.id === hoveredNode;
      const isProject = node.isProject;
      const isNeighbor = node.isNeighbor;
      const isSearchMatch = node.isSearchMatch;
      const degree = node.degree || 0;
      const maxDegree = graphData.maxDegree || 1;

      // Size based on degree (connections) - more connections = bigger
      const degreeScale = Math.sqrt(degree / maxDegree);
      const logDegree = degree > 0 ? Math.log2(degree + 1) / Math.log2(maxDegree + 1) : 0;
      const combinedScale = (degreeScale + logDegree) / 2;

      // Minimum size of 5px ensures all nodes are visible
      // Neighbors are slightly smaller to emphasize cluster nodes
      // Search matches are enlarged for visibility
      let size: number;
      if (isProject) {
        size = 14 + combinedScale * 10;
      } else if (isSelected) {
        size = Math.max(12, 6 + combinedScale * 10);
      } else if (isHovered) {
        size = Math.max(10, 5 + combinedScale * 9);
      } else if (isSearchMatch) {
        size = Math.max(10, 6 + combinedScale * 10); // Enlarged for visibility
      } else if (isNeighbor) {
        size = 4 + combinedScale * 8; // Smaller context nodes
      } else {
        size = 5 + combinedScale * 12;
      }

      const baseColor = node.entityColor || '#8b85a0';
      // Neighbors are rendered at 40% opacity to fade into background
      // Search matches keep full opacity
      const color =
        isNeighbor && !isSelected && !isHovered && !isSearchMatch ? `${baseColor}66` : baseColor;

      // Outer glow for search matches (electric purple pulse)
      if (isSearchMatch && !isSelected && !isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, size + 8, 0, 2 * Math.PI);
        ctx.fillStyle = 'rgba(225, 53, 255, 0.15)'; // Electric purple outer
        ctx.fill();
        ctx.beginPath();
        ctx.arc(x, y, size + 4, 0, 2 * Math.PI);
        ctx.fillStyle = 'rgba(225, 53, 255, 0.3)'; // Electric purple inner
        ctx.fill();
      }

      // Glow for selected/hovered
      if (isSelected || isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, size + 4, 0, 2 * Math.PI);
        ctx.fillStyle = `${color}40`;
        ctx.fill();
      }

      // Main node
      ctx.beginPath();
      ctx.arc(x, y, size, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();

      // Border for selected/hovered/search match
      if (isSelected) {
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();
      } else if (isHovered) {
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.5)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      } else if (isSearchMatch) {
        ctx.strokeStyle = '#e135ff'; // Electric purple border
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // =================================================================
      // LABEL VISIBILITY - Progressive reveal based on zoom level
      // =================================================================
      // globalScale: 0.3 = zoomed out, 1.0 = default, 4.0+ = zoomed in

      const isHubNode = degree > Math.max(3, maxDegree * 0.05);

      // Determine if label should show based on zoom + importance
      // Neighbors only show labels when hovered/selected to keep focus on cluster
      // Search matches always show labels for discoverability
      let showLabel = false;

      if (isSelected || isHovered || isSearchMatch) {
        showLabel = true;
      } else if (isNeighbor) {
        // Neighbors only show label when zoomed in very close
        showLabel = globalScale >= 4.0;
      } else if (isProject && globalScale >= 0.4) {
        showLabel = true;
      } else if (isHubNode && globalScale >= 0.7) {
        showLabel = true;
      } else if (degree >= 5 && globalScale >= 1.2) {
        showLabel = true;
      } else if (degree >= 3 && globalScale >= 1.8) {
        showLabel = true;
      } else if (degree >= 1 && globalScale >= 2.5) {
        showLabel = true;
      } else if (globalScale >= 3.5) {
        showLabel = true;
      }

      if (showLabel) {
        const label = node.label || node.name || node.id.slice(0, 8);

        // Truncate based on zoom - show more text as you zoom in
        const maxLen = Math.min(40, Math.floor(10 + globalScale * 5));
        const displayLabel = label.length > maxLen ? `${label.slice(0, maxLen - 3)}...` : label;

        // Font size: DIVIDE by globalScale to keep consistent screen size
        // Canvas is scaled by globalScale, so counter-scale the font
        const screenFontSize = 11; // desired size on screen in pixels
        const fontSize = screenFontSize / globalScale;

        ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';

        const labelY = y + size + 2 / globalScale; // small gap below node

        // Text shadow for readability
        const shadowOffset = 0.5 / globalScale;
        ctx.fillStyle = theme === 'neon' ? 'rgba(0, 0, 0, 0.8)' : 'rgba(255, 255, 255, 0.9)';
        ctx.fillText(displayLabel, x + shadowOffset, labelY + shadowOffset);

        // Text color - slightly transparent for non-priority labels
        const textColor = colors.fgPrimary;
        const isPriority = isSelected || isHovered || isProject || isHubNode || isSearchMatch;
        ctx.fillStyle = isPriority ? textColor : `${textColor}bb`;
        ctx.fillText(displayLabel, x, labelY);
      }
    },
    [selectedNodeId, hoveredNode, graphData.maxDegree, theme, colors]
  );

  // Clean link rendering
  const paintLink = useCallback(
    (link: GraphLink, ctx: CanvasRenderingContext2D) => {
      // After d3-force processes, source/target become node objects (not strings)
      const source = link.sourceNode || (typeof link.source === 'object' ? link.source : null);
      const target = link.targetNode || (typeof link.target === 'object' ? link.target : null);
      if (!source || !target) return;
      const sx = source.x;
      const sy = source.y;
      const tx = target.x;
      const ty = target.y;
      if (sx === undefined || sy === undefined || tx === undefined || ty === undefined) return;

      // Highlight links connected to selected/hovered node
      const isHighlighted =
        source.id === selectedNodeId ||
        target.id === selectedNodeId ||
        source.id === hoveredNode ||
        target.id === hoveredNode;

      ctx.beginPath();
      ctx.moveTo(sx, sy);
      ctx.lineTo(tx, ty);

      // Theme-aware link colors
      const linkColor = theme === 'neon' ? '#ffffff' : '#2b2540';
      if (isHighlighted) {
        ctx.strokeStyle = theme === 'neon' ? `${linkColor}60` : `${linkColor}80`;
        ctx.lineWidth = 2;
      } else {
        ctx.strokeStyle = theme === 'neon' ? `${linkColor}30` : `${linkColor}50`;
        ctx.lineWidth = 0.8;
      }
      ctx.stroke();
    },
    [selectedNodeId, hoveredNode, theme]
  );

  // Smooth zoom to node on click
  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      const isDeselecting = selectedNodeId === node.id;
      setSelectedNodeId(isDeselecting ? null : node.id);

      if (!isDeselecting && graphRef.current && node.x !== undefined && node.y !== undefined) {
        // Smooth zoom and center on the clicked node
        graphRef.current.centerAt(node.x, node.y, 800);
        // Zoom in for detail view (but not too close)
        const currentZoom = graphRef.current.zoom();
        if (currentZoom < 2.5) {
          graphRef.current.zoom(2.5, 800);
        }
      }
    },
    [selectedNodeId]
  );

  const handleClosePanel = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  const handleZoomIn = useCallback(() => {
    if (graphRef.current) {
      const currentZoom = graphRef.current.zoom();
      graphRef.current.zoom(currentZoom * 1.5, 300);
    }
  }, []);

  const handleZoomOut = useCallback(() => {
    if (graphRef.current) {
      const currentZoom = graphRef.current.zoom();
      graphRef.current.zoom(currentZoom / 1.5, 300);
    }
  }, []);

  const handleFitView = useCallback(() => {
    graphRef.current?.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
  }, []);

  const handleReset = useCallback(() => {
    graphRef.current?.zoomToFit(400, GRAPH_DEFAULTS.FIT_PADDING);
    graphRef.current?.centerAt(0, 0, 300);
    setSelectedNodeId(null);
    setSelectedCluster(null);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) return;
    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  }, []);

  const nodeCount = graphData.nodes.length;
  const edgeCount = graphData.links.length;

  return (
    <div
      ref={containerRef}
      className={`flex flex-col ${isFullscreen ? 'fixed inset-0 z-50' : 'h-full'}`}
      style={isFullscreen ? { backgroundColor: colors.bg } : undefined}
      suppressHydrationWarning
    >
      {!isFullscreen && <Breadcrumb className="hidden md:flex" />}

      <div className="flex-1 flex gap-4 min-h-0 mt-0 md:mt-4">
        <div
          className="flex-1 relative md:rounded-xl md:border border-sc-fg-subtle/20 overflow-hidden"
          style={{ backgroundColor: colors.bg }}
          suppressHydrationWarning
        >
          <GraphToolbar
            onZoomIn={handleZoomIn}
            onZoomOut={handleZoomOut}
            onFitView={handleFitView}
            onReset={handleReset}
            isFullscreen={isFullscreen}
            onToggleFullscreen={toggleFullscreen}
            searchTerm={searchTerm}
            onSearchChange={setSearchTerm}
            selectedTypes={selectedTypes}
            onTypesChange={setSelectedTypes}
            matchCount={graphData.matchCount}
            nodeCount={nodeCount}
            edgeCount={edgeCount}
          />

          {/* Stats overlay - separate for detailed view */}
          {data && (
            <StatsOverlay
              totalNodes={data.total_nodes}
              totalEdges={data.total_edges}
              displayedNodes={data.displayed_nodes ?? graphData.nodes.length}
              displayedEdges={data.displayed_edges ?? graphData.links.length}
              clusterCount={data.clusters.length}
            />
          )}

          {/* Loading overlay */}
          {isLoading && (
            <div
              className="absolute inset-0 flex items-center justify-center z-20"
              style={{ backgroundColor: `${colors.bg}cc` }}
              suppressHydrationWarning
            >
              <div className="flex items-center gap-3 text-sc-fg-muted">
                <Loader2 width={20} height={20} className="animate-spin text-sc-purple" />
                <span>Detecting communities & building graph...</span>
              </div>
            </div>
          )}

          {/* Empty state */}
          {!isLoading && graphData.nodes.length === 0 && (
            <div
              className="flex items-center justify-center h-full"
              style={{ backgroundColor: colors.bg }}
              suppressHydrationWarning
            >
              <GraphEmptyState />
            </div>
          )}

          {/* Graph - key forces re-render when theme changes */}
          {!isLoading && graphData.nodes.length > 0 && (
            <ForceGraph2D
              key={`${theme}-${selectedProjects.join(',')}-${selectedCluster || 'all'}`}
              ref={graphRef as React.MutableRefObject<ForceGraphMethods | undefined>}
              graphData={graphData as { nodes: object[]; links: object[] }}
              nodeLabel={() => ''} // Disable default tooltip - we render labels on canvas
              nodeCanvasObject={
                paintNode as (
                  node: object,
                  ctx: CanvasRenderingContext2D,
                  globalScale: number
                ) => void
              }
              nodeCanvasObjectMode={() => 'replace'}
              linkCanvasObject={
                paintLink as (
                  link: object,
                  ctx: CanvasRenderingContext2D,
                  globalScale: number
                ) => void
              }
              linkCanvasObjectMode={() => 'replace'}
              onNodeClick={handleNodeClick as (node: object, event: MouseEvent) => void}
              onNodeHover={node => setHoveredNode((node as GraphNode)?.id || null)}
              cooldownTicks={GRAPH_DEFAULTS.COOLDOWN_TICKS}
              warmupTicks={GRAPH_DEFAULTS.WARMUP_TICKS}
              backgroundColor={colors.bg}
              enableZoomInteraction={true}
              enablePanInteraction={true}
              enableNodeDrag={true}
              minZoom={0.1}
              maxZoom={10}
              d3AlphaDecay={GRAPH_DEFAULTS.ALPHA_DECAY}
              d3VelocityDecay={GRAPH_DEFAULTS.VELOCITY_DECAY}
            />
          )}

          {/* Cluster legend - bottom left */}
          {data && data.clusters.length > 0 && (
            <div className="absolute bottom-4 left-4 z-10 hidden md:block">
              <ClusterLegend
                clusters={data.clusters}
                clusterColorMap={clusterColorMap}
                selectedCluster={selectedCluster}
                onClusterClick={setSelectedCluster}
                nodes={allNodesWithDegree}
              />
            </div>
          )}

          {/* Keyboard hints - desktop only */}
          <div className="absolute bottom-4 right-4 z-10 text-xs text-sc-fg-subtle/50 hidden md:block">
            <kbd className="px-1.5 py-0.5 rounded bg-sc-bg-highlight/50 border border-sc-fg-subtle/20">
              scroll
            </kbd>{' '}
            zoom ·{' '}
            <kbd className="px-1.5 py-0.5 rounded bg-sc-bg-highlight/50 border border-sc-fg-subtle/20">
              drag
            </kbd>{' '}
            pan ·{' '}
            <kbd className="px-1.5 py-0.5 rounded bg-sc-bg-highlight/50 border border-sc-fg-subtle/20">
              click
            </kbd>{' '}
            select
          </div>
        </div>

        {/* Entity detail panel - desktop sidebar */}
        {selectedNodeId && (
          <div className="hidden md:block">
            <EntityDetailPanel entityId={selectedNodeId} onClose={handleClosePanel} />
          </div>
        )}
      </div>

      {/* Entity detail panel - mobile bottom sheet */}
      {selectedNodeId && <MobileEntitySheet entityId={selectedNodeId} onClose={handleClosePanel} />}
    </div>
  );
}

export default function GraphPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <GraphPageContent />
    </Suspense>
  );
}
