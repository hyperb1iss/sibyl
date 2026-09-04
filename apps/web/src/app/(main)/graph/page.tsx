'use client';

import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { ClusterLegend } from '@/components/graph/cluster-legend';
import { GraphCanvas } from '@/components/graph/graph-canvas';
import { GraphEntityDetails } from '@/components/graph/graph-entity-details';
import { GraphToolbar } from '@/components/graph/graph-toolbar';
import type { KnowledgeGraphRef } from '@/components/graph/graph-types';
import { StatsOverlay } from '@/components/graph/stats-overlay';
import { useGraphPageState } from '@/components/graph/use-graph-page-state';
import { LoadingState } from '@/components/ui/spinner';
import { useMediaQuery } from '@/lib/hooks';
import { useTheme } from '@/lib/theme';

export type { KnowledgeGraphRef } from '@/components/graph/graph-types';

const CANVAS_COLORS = {
  neon: { bg: '#0a0812', fgPrimary: '#fafaf5', fgMuted: '#9b93b8' },
  dawn: { bg: '#f1ecff', fgPrimary: '#2b2540', fgMuted: '#8e84a8' },
};

function GraphPageContent() {
  const { theme } = useTheme();
  const colors = CANVAS_COLORS[theme];
  const isMobile = useMediaQuery('(max-width: 767px)');
  const graphRef = useRef<KnowledgeGraphRef>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const graph = useGraphPageState(theme);

  useEffect(() => {
    function handleFullscreenChange() {
      setIsFullscreen(Boolean(document.fullscreenElement));
    }
    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, []);

  const handleZoomIn = useCallback(() => graphRef.current?.zoomIn(), []);
  const handleZoomOut = useCallback(() => graphRef.current?.zoomOut(), []);
  const handleFitView = useCallback(() => graphRef.current?.fitView(), []);
  const handleJumpToLevel = useCallback(
    (level: 'domains' | 'entities') => {
      // Jumping to the domain map means the whole map: pins the reader left
      // open would otherwise hold their clusters open underneath it.
      if (level === 'domains') graph.clearCluster();
      graphRef.current?.zoomToLevel(level);
    },
    [graph.clearCluster]
  );

  const handleReset = useCallback(() => {
    graphRef.current?.resetView();
    graph.resetState();
  }, [graph.resetState]);

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) return;
    if (!document.fullscreenElement) {
      void containerRef.current.requestFullscreen();
    } else {
      void document.exitFullscreen();
    }
  }, []);

  const nodeCount = graph.graphData.nodes.length;
  const edgeCount = graph.graphData.links.length;

  return (
    <div
      ref={containerRef}
      className={`flex flex-col ${isFullscreen ? 'fixed inset-0 z-50' : 'h-full'}`}
      style={isFullscreen ? { backgroundColor: colors.bg } : undefined}
      suppressHydrationWarning
    >
      <div className="flex-1 flex gap-4 min-h-0 mt-0 md:mt-4">
        <div
          className="flex-1 relative md:rounded-xl md:border border-sc-fg-subtle/20 overflow-hidden"
          style={{ backgroundColor: colors.bg }}
          suppressHydrationWarning
        >
          <GraphToolbar
            zoomLevel={graph.zoomLevel}
            onJumpToLevel={handleJumpToLevel}
            selectedClusterLabel={graph.selectedClusterLabel}
            onClearCluster={graph.clearCluster}
            onZoomIn={handleZoomIn}
            onZoomOut={handleZoomOut}
            onFitView={handleFitView}
            onReset={handleReset}
            isFullscreen={isFullscreen}
            onToggleFullscreen={toggleFullscreen}
            searchTerm={graph.searchTerm}
            onSearchChange={graph.setSearchTerm}
            selectedTypes={graph.selectedTypes}
            onTypesChange={graph.setSelectedTypes}
            matchCount={graph.graphData.matchCount}
            nodeCount={nodeCount}
            edgeCount={edgeCount}
            includeShared={graph.includeShared}
            onIncludeSharedChange={graph.setIncludeShared}
            sharedLabel={graph.sharedProjectLabel}
            sharedAvailable={graph.canToggleShared}
            focusProjects={graph.focusProjects}
            onFocusProjectsChange={graph.setFocusProjects}
            focusedProjectCount={graph.focusedProjectCount}
            focusAvailable={graph.canToggleFocus}
          />

          {graph.data && (
            <StatsOverlay
              totalNodes={graph.data.total_nodes}
              totalEdges={graph.data.total_edges}
              displayedNodes={nodeCount}
              displayedEdges={edgeCount}
              clusterCount={graph.data.clusters.length}
            />
          )}

          <GraphCanvas
            ref={graphRef}
            graphData={graph.graphData}
            graphRenderKey={graph.graphRenderKey}
            filterKey={graph.filterKey}
            expandedClusterLabels={graph.expandedClusterLabels}
            zoomBounds={graph.zoomBounds}
            onViewportChange={graph.handleViewportChange}
            selectedNodeId={graph.selectedNodeId}
            colors={colors}
            theme={theme}
            isLoading={graph.isLoading}
            graphError={graph.graphError}
            onNodeClick={graph.handleNodeClick}
          />

          {graph.data && graph.data.clusters.length > 0 && (
            <div className="absolute bottom-4 left-4 z-10 hidden md:block">
              <ClusterLegend
                clusters={graph.data.clusters}
                clusterColorMap={graph.clusterColorMap}
                selectedCluster={graph.selectedCluster}
                onClusterClick={graph.handleClusterClick}
                nodes={graph.allNodesWithDegree}
              />
            </div>
          )}

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

        {graph.selectedNodeId && (
          <GraphEntityDetails
            entityId={graph.selectedNodeId}
            isMobile={isMobile}
            onClose={graph.closeNodeDetails}
            relatedEntities={graph.selectedNodeRelated}
          />
        )}
      </div>
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
