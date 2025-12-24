'use client';

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { EntityDetailPanel } from '@/components/graph/entity-detail-panel';
import { KnowledgeGraph, type KnowledgeGraphRef } from '@/components/graph/knowledge-graph';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { Card } from '@/components/ui/card';
import {
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
import { ENTITY_COLORS, ENTITY_ICONS, ENTITY_TYPES, GRAPH_DEFAULTS } from '@/lib/constants';
import { useGraphData, useStats } from '@/lib/hooks';

type EntityType = (typeof ENTITY_TYPES)[number];

interface GraphFilters {
  types: string[];
  search: string;
}

function TypeChip({
  type,
  active,
  count,
  onClick,
  compact = false,
}: {
  type: EntityType;
  active: boolean;
  count?: number;
  onClick: () => void;
  compact?: boolean;
}) {
  const color = ENTITY_COLORS[type];
  const icon = ENTITY_ICONS[type];

  return (
    <button
      type="button"
      onClick={onClick}
      className={`
        group flex items-center gap-1 sm:gap-1.5 px-2 sm:px-2.5 py-1 rounded-full text-[10px] sm:text-xs font-medium
        transition-all duration-150 border whitespace-nowrap
        ${
          active
            ? 'border-current bg-current/15 shadow-sm shadow-current/20'
            : 'border-sc-fg-subtle/20 bg-sc-bg-dark/50 text-sc-fg-subtle hover:border-sc-fg-subtle/40'
        }
      `}
      style={active ? { color, borderColor: color } : {}}
    >
      <span className="opacity-80">{icon}</span>
      {!compact && <span className="capitalize hidden xs:inline">{type.replace(/_/g, ' ')}</span>}
      {count !== undefined && count > 0 && (
        <span
          className={`text-[9px] sm:text-[10px] ${active ? 'opacity-70' : 'text-sc-fg-subtle'}`}
        >
          {count}
        </span>
      )}
    </button>
  );
}

// Mobile bottom sheet for entity details
function MobileEntitySheet({ entityId, onClose }: { entityId: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 md:hidden">
      {/* Backdrop */}
      <button
        type="button"
        className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default"
        onClick={onClose}
        onKeyDown={e => e.key === 'Escape' && onClose()}
        aria-label="Close panel"
      />
      {/* Sheet */}
      <div className="absolute bottom-0 left-0 right-0 max-h-[70vh] bg-sc-bg-base rounded-t-2xl overflow-hidden animate-slide-up">
        {/* Drag handle */}
        <div className="flex justify-center py-2">
          <div className="w-10 h-1 bg-sc-fg-subtle/30 rounded-full" />
        </div>
        <EntityDetailPanel entityId={entityId} onClose={onClose} variant="sheet" />
      </div>
    </div>
  );
}

function GraphToolbar({
  filters,
  onFilterChange,
  onZoomIn,
  onZoomOut,
  onFitView,
  onReset,
  isFullscreen,
  onToggleFullscreen,
  nodeCount,
  edgeCount,
}: {
  filters: GraphFilters;
  onFilterChange: (filters: GraphFilters) => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitView: () => void;
  onReset: () => void;
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
  nodeCount: number;
  edgeCount: number;
}) {
  const { data: stats } = useStats();
  const [showFilters, setShowFilters] = useState(false); // Collapsed by default on mobile
  const [showSearch, setShowSearch] = useState(false);

  // Expand filters on larger screens
  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth >= 768) {
        setShowFilters(true);
      }
    };
    handleResize();
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  const entityCounts = stats?.entity_counts || {};
  const availableTypes = ENTITY_TYPES.filter(type => (entityCounts[type] || 0) > 0);

  const toggleType = (type: string) => {
    const newTypes = filters.types.includes(type)
      ? filters.types.filter(t => t !== type)
      : [...filters.types, type];
    onFilterChange({ ...filters, types: newTypes });
  };

  const selectAll = () => {
    onFilterChange({ ...filters, types: [] });
  };

  const selectNone = () => {
    onFilterChange({ ...filters, types: ['__none__'] });
  };

  const activeFilterCount =
    filters.types.length > 0 && filters.types[0] !== '__none__' ? filters.types.length : 0;

  return (
    <>
      {/* Mobile compact toolbar */}
      <div className="absolute top-2 left-2 right-2 z-10 flex items-center gap-2 md:hidden">
        {/* Search toggle */}
        <button
          type="button"
          onClick={() => setShowSearch(!showSearch)}
          className={`p-2.5 rounded-lg transition-colors ${
            showSearch || filters.search
              ? 'bg-sc-purple/20 text-sc-purple'
              : 'bg-sc-bg-base/90 text-sc-fg-subtle hover:text-sc-fg-primary'
          } border border-sc-fg-subtle/20`}
        >
          <Search width={18} height={18} />
        </button>

        {/* Filter toggle */}
        <button
          type="button"
          onClick={() => setShowFilters(!showFilters)}
          className={`p-2.5 rounded-lg transition-colors relative ${
            showFilters || activeFilterCount > 0
              ? 'bg-sc-purple/20 text-sc-purple'
              : 'bg-sc-bg-base/90 text-sc-fg-subtle hover:text-sc-fg-primary'
          } border border-sc-fg-subtle/20`}
        >
          <Filter width={18} height={18} />
          {activeFilterCount > 0 && (
            <span className="absolute -top-1 -right-1 w-4 h-4 bg-sc-purple text-white text-[10px] rounded-full flex items-center justify-center">
              {activeFilterCount}
            </span>
          )}
        </button>

        {/* Stats pill */}
        <div className="flex-1 flex items-center justify-center gap-3 text-xs">
          <span>
            <span className="text-sc-purple font-medium">{nodeCount}</span>
            <span className="text-sc-fg-subtle ml-1">nodes</span>
          </span>
          <span>
            <span className="text-sc-cyan font-medium">{edgeCount}</span>
            <span className="text-sc-fg-subtle ml-1">edges</span>
          </span>
        </div>

        {/* Fullscreen */}
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

      {/* Mobile search bar (expandable) */}
      {showSearch && (
        <div className="absolute top-14 left-2 right-2 z-10 md:hidden animate-fade-in">
          <Card className="!p-0 overflow-hidden">
            <div className="flex items-center gap-2 px-3 py-2.5">
              <Search width={16} height={16} className="text-sc-fg-subtle flex-shrink-0" />
              <input
                type="text"
                placeholder="Search nodes..."
                value={filters.search}
                onChange={e => onFilterChange({ ...filters, search: e.target.value })}
                className="flex-1 bg-transparent text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:outline-none"
              />
              {filters.search && (
                <button
                  type="button"
                  onClick={() => onFilterChange({ ...filters, search: '' })}
                  className="text-sc-fg-subtle hover:text-sc-fg-primary p-1"
                >
                  <X width={16} height={16} />
                </button>
              )}
            </div>
          </Card>
        </div>
      )}

      {/* Mobile filter chips (expandable) */}
      {showFilters && (
        <div
          className={`absolute ${showSearch ? 'top-28' : 'top-14'} left-2 right-2 z-10 md:hidden animate-fade-in`}
        >
          <Card className="!p-2 overflow-hidden">
            <div className="flex items-center gap-2 mb-2 text-xs">
              <span className="text-sc-fg-muted">Filter:</span>
              <button
                type="button"
                onClick={selectAll}
                className="text-sc-fg-subtle hover:text-sc-cyan transition-colors"
              >
                All
              </button>
              <span className="text-sc-fg-subtle/30">|</span>
              <button
                type="button"
                onClick={selectNone}
                className="text-sc-fg-subtle hover:text-sc-coral transition-colors"
              >
                None
              </button>
            </div>
            <div className="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto">
              {availableTypes.map(type => (
                <TypeChip
                  key={type}
                  type={type}
                  active={filters.types.length === 0 || filters.types.includes(type)}
                  count={entityCounts[type]}
                  onClick={() => toggleType(type)}
                  compact
                />
              ))}
            </div>
          </Card>
        </div>
      )}

      {/* Desktop toolbar */}
      <div className="absolute top-4 left-4 right-4 z-10 hidden md:flex items-start gap-3">
        {/* Search + Stats */}
        <Card className="!p-0 flex-1 max-w-md overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-sc-fg-subtle/10">
            <Search width={14} height={14} className="text-sc-fg-subtle" />
            <input
              type="text"
              placeholder="Search nodes..."
              value={filters.search}
              onChange={e => onFilterChange({ ...filters, search: e.target.value })}
              className="flex-1 bg-transparent text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:outline-none"
            />
            {filters.search && (
              <button
                type="button"
                onClick={() => onFilterChange({ ...filters, search: '' })}
                className="text-sc-fg-subtle hover:text-sc-fg-primary"
              >
                <X width={14} height={14} />
              </button>
            )}
          </div>
          <div className="flex items-center justify-between px-3 py-1.5 text-xs text-sc-fg-subtle">
            <span>
              <span className="text-sc-purple font-medium">{nodeCount}</span> nodes
            </span>
            <span>
              <span className="text-sc-cyan font-medium">{edgeCount}</span> edges
            </span>
          </div>
        </Card>

        {/* Type Filters */}
        <Card className="!p-0 flex-1 overflow-hidden">
          <button
            type="button"
            onClick={() => setShowFilters(!showFilters)}
            className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
          >
            <span>Filter by Type</span>
            <div className="flex items-center gap-2">
              {activeFilterCount > 0 && (
                <span className="text-sc-purple">{activeFilterCount} selected</span>
              )}
              {showFilters ? (
                <ChevronUp width={14} height={14} />
              ) : (
                <ChevronDown width={14} height={14} />
              )}
            </div>
          </button>
          {showFilters && (
            <div className="px-3 pb-3 space-y-2">
              <div className="flex items-center gap-2 text-xs">
                <button
                  type="button"
                  onClick={selectAll}
                  className="text-sc-fg-subtle hover:text-sc-cyan transition-colors"
                >
                  All
                </button>
                <span className="text-sc-fg-subtle/30">|</span>
                <button
                  type="button"
                  onClick={selectNone}
                  className="text-sc-fg-subtle hover:text-sc-coral transition-colors"
                >
                  None
                </button>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {availableTypes.map(type => (
                  <TypeChip
                    key={type}
                    type={type}
                    active={filters.types.length === 0 || filters.types.includes(type)}
                    count={entityCounts[type]}
                    onClick={() => toggleType(type)}
                  />
                ))}
              </div>
            </div>
          )}
        </Card>

        {/* Zoom Controls */}
        <Card className="!p-1 flex items-center gap-1">
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
          <div className="w-px h-4 bg-sc-fg-subtle/20" />
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
          <div className="w-px h-4 bg-sc-fg-subtle/20" />
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
        </Card>
      </div>

      {/* Mobile zoom controls (bottom of screen for thumb reach) */}
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
  const graphRef = useRef<KnowledgeGraphRef>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [filters, setFilters] = useState<GraphFilters>({ types: [], search: '' });
  const [isFullscreen, setIsFullscreen] = useState(false);

  // Fetch graph data with type filters
  const queryParams = useMemo(() => {
    const params: { types?: string[]; max_nodes: number; max_edges: number } = {
      max_nodes: GRAPH_DEFAULTS.MAX_NODES,
      max_edges: GRAPH_DEFAULTS.MAX_EDGES,
    };
    if (filters.types.length > 0 && filters.types[0] !== '__none__') {
      params.types = filters.types;
    }
    return params;
  }, [filters.types]);

  const { data, isLoading } = useGraphData(queryParams);

  // Filter nodes by search
  const filteredData = useMemo(() => {
    if (!data) return null;
    if (!filters.search) return data;

    const searchLower = filters.search.toLowerCase();
    const matchingNodeIds = new Set(
      data.nodes.filter(n => n.label?.toLowerCase().includes(searchLower)).map(n => n.id)
    );

    return {
      nodes: data.nodes.filter(n => matchingNodeIds.has(n.id)),
      edges: data.edges.filter(e => matchingNodeIds.has(e.source) && matchingNodeIds.has(e.target)),
    };
  }, [data, filters.search]);

  // Get unique types present in the filtered data for the legend
  const visibleTypes = useMemo(() => {
    if (!filteredData?.nodes) return [];
    const types = new Set(filteredData.nodes.map(n => n.type).filter(Boolean));
    // Sort by ENTITY_TYPES order for consistency
    return ENTITY_TYPES.filter(t => types.has(t));
  }, [filteredData]);

  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedNodeId(prev => (prev === nodeId ? null : nodeId));
  }, []);

  const handleClosePanel = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  const handleZoomIn = useCallback(() => {
    graphRef.current?.zoomIn();
  }, []);

  const handleZoomOut = useCallback(() => {
    graphRef.current?.zoomOut();
  }, []);

  const handleFitView = useCallback(() => {
    graphRef.current?.fitView();
  }, []);

  const handleReset = useCallback(() => {
    graphRef.current?.resetView();
    setFilters({ types: [], search: '' });
    setSelectedNodeId(null);
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

  return (
    <div
      ref={containerRef}
      className={`flex flex-col ${isFullscreen ? 'fixed inset-0 z-50 bg-sc-bg-dark' : 'h-full'}`}
    >
      {!isFullscreen && <Breadcrumb className="hidden md:flex" />}

      {/* Main graph area */}
      <div className="flex-1 flex gap-4 min-h-0 mt-0 md:mt-4">
        <div className="flex-1 relative bg-sc-bg-dark md:rounded-xl md:border border-sc-fg-subtle/20 overflow-hidden">
          {/* Toolbar overlay */}
          <GraphToolbar
            filters={filters}
            onFilterChange={setFilters}
            onZoomIn={handleZoomIn}
            onZoomOut={handleZoomOut}
            onFitView={handleFitView}
            onReset={handleReset}
            isFullscreen={isFullscreen}
            onToggleFullscreen={toggleFullscreen}
            nodeCount={filteredData?.nodes.length || 0}
            edgeCount={filteredData?.edges.length || 0}
          />

          {/* Loading overlay */}
          {isLoading && (
            <div className="absolute inset-0 flex items-center justify-center bg-sc-bg-dark/80 z-20">
              <div className="flex items-center gap-3 text-sc-fg-muted">
                <Loader2 width={20} height={20} className="animate-spin text-sc-purple" />
                <span>Loading graph...</span>
              </div>
            </div>
          )}

          {/* Graph canvas */}
          <KnowledgeGraph
            ref={graphRef}
            data={filteredData}
            onNodeClick={handleNodeClick}
            selectedNodeId={selectedNodeId}
            searchTerm={filters.search}
          />

          {/* Legend - desktop only, shows types actually in the data */}
          {visibleTypes.length > 0 && (
            <div className="absolute bottom-4 left-4 z-10 hidden md:block">
              <Card className="!p-2 flex flex-wrap gap-x-3 gap-y-1 max-w-sm">
                {visibleTypes.map(type => (
                  <div key={type} className="flex items-center gap-1.5 text-xs">
                    <div
                      className="w-2 h-2 rounded-full"
                      style={{ backgroundColor: ENTITY_COLORS[type] }}
                    />
                    <span className="text-sc-fg-subtle capitalize">{type.replace(/_/g, ' ')}</span>
                  </div>
                ))}
              </Card>
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
            pan
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
