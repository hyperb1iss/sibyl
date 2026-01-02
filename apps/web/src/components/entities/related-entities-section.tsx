'use client';

import { ArrowRight, GraphUp, List } from 'iconoir-react';
import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import { EntityBadge, RelationshipBadge } from '@/components/ui/badge';
import type { RelatedEntitySummary } from '@/lib/api';
import { ENTITY_COLORS, type EntityType } from '@/lib/constants';
import { useTheme } from '@/lib/theme';

// Dynamic import to avoid SSR issues with canvas
const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full bg-sc-bg-secondary/50 rounded-lg">
      <div className="text-sc-fg-muted text-xs">Loading...</div>
    </div>
  ),
});

interface RelatedEntitiesSectionProps {
  /** The central entity ID */
  entityId: string;
  /** The central entity name (for display) */
  entityName: string;
  /** The central entity type */
  entityType: string;
  /** Related entities from API */
  related: RelatedEntitySummary[];
  /** Optional: Maximum items to show in list */
  maxItems?: number;
  /** Optional: Title override */
  title?: string;
}

interface GraphNode {
  id: string;
  label: string;
  color: string;
  size: number;
  type?: string;
  isCenter?: boolean;
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
}

interface GraphLink {
  source: string | GraphNode;
  target: string | GraphNode;
  color: string;
}

const CANVAS_COLORS = {
  neon: { bg: '#0a0812', fgMuted: '#9b93b8', fgSubtle: '#5a5478' },
  dawn: { bg: '#f1ecff', fgMuted: '#8e84a8', fgSubtle: '#b8b0cc' },
};

/**
 * Mini force-directed graph visualization for related entities.
 * Uses react-force-graph-2d like the main graph page.
 */
function MiniGraph({
  entityId,
  entityName,
  entityType,
  related,
}: {
  entityId: string;
  entityName: string;
  entityType: string;
  related: RelatedEntitySummary[];
}) {
  const { theme } = useTheme();
  const colors = CANVAS_COLORS[theme];
  const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 300, height: 180 });

  // Measure container with ResizeObserver for responsive sizing
  useEffect(() => {
    if (!containerRef.current) return;

    const updateDimensions = (entry: ResizeObserverEntry) => {
      const { width } = entry.contentRect;
      if (width > 0) {
        setDimensions({ width, height: Math.min(width * 0.5, 180) });
      }
    };

    const observer = new ResizeObserver(entries => {
      const entry = entries[0];
      if (entry) updateDimensions(entry);
    });

    observer.observe(containerRef.current);

    // Initial measurement
    const rect = containerRef.current.getBoundingClientRect();
    if (rect.width > 0) {
      setDimensions({ width: rect.width, height: Math.min(rect.width * 0.5, 180) });
    }

    return () => observer.disconnect();
  }, []);

  // Build graph data
  const graphData = useMemo(() => {
    const centerColor = ENTITY_COLORS[entityType as EntityType] ?? '#8b85a0';

    const nodes: GraphNode[] = [
      {
        id: entityId,
        label: entityName.length > 15 ? `${entityName.slice(0, 15)}…` : entityName,
        color: centerColor,
        size: 10,
        type: entityType,
        isCenter: true,
      },
      ...related.map(r => ({
        id: r.id,
        label: r.name.length > 12 ? `${r.name.slice(0, 12)}…` : r.name,
        color: ENTITY_COLORS[r.entity_type as EntityType] ?? '#8b85a0',
        size: 6,
        type: r.entity_type,
        isCenter: false,
      })),
    ];

    const links: GraphLink[] = related.map(r => ({
      source: r.direction === 'outgoing' ? entityId : r.id,
      target: r.direction === 'outgoing' ? r.id : entityId,
      color: '#4a4560',
    }));

    return { nodes, links };
  }, [entityId, entityName, entityType, related]);

  // Custom node rendering
  const paintNode = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D) => {
      const size = node.size || 6;
      const x = node.x || 0;
      const y = node.y || 0;

      // Glow for center node
      if (node.isCenter) {
        ctx.beginPath();
        ctx.arc(x, y, size + 3, 0, 2 * Math.PI);
        ctx.fillStyle = 'rgba(225, 53, 255, 0.3)';
        ctx.fill();
      }

      // Main node
      ctx.beginPath();
      ctx.arc(x, y, size, 0, 2 * Math.PI);
      ctx.fillStyle = node.color;
      ctx.fill();

      // Border
      ctx.strokeStyle = node.isCenter ? '#e135ff' : colors.fgSubtle;
      ctx.lineWidth = node.isCenter ? 1.5 : 0.5;
      ctx.stroke();

      // Label
      const fontSize = node.isCenter ? 8 : 6;
      ctx.font = `${fontSize}px "Space Grotesk", sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = colors.fgMuted;
      ctx.fillText(node.label, x, y + size + 2);
    },
    [colors]
  );

  // Custom link rendering
  const paintLink = useCallback((link: GraphLink, ctx: CanvasRenderingContext2D) => {
    const source = link.source as GraphNode;
    const target = link.target as GraphNode;
    if (!source.x || !source.y || !target.x || !target.y) return;

    ctx.beginPath();
    ctx.moveTo(source.x, source.y);
    ctx.lineTo(target.x, target.y);
    ctx.strokeStyle = link.color;
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.5;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }, []);

  // Click handler for navigation
  const handleNodeClick = useCallback((node: GraphNode) => {
    if (!node.isCenter && node.id) {
      const type = node.type;
      if (type === 'task') {
        window.location.href = `/tasks/${node.id}`;
      } else if (type === 'project') {
        window.location.href = `/projects/${node.id}`;
      } else if (type === 'epic') {
        window.location.href = `/epics/${node.id}`;
      } else {
        window.location.href = `/entities/${node.id}`;
      }
    }
  }, []);

  // Fit view once simulation settles
  const handleEngineStop = useCallback(() => {
    graphRef.current?.zoomToFit(200, 30);
  }, []);

  if (related.length === 0) return null;

  return (
    <div
      ref={containerRef}
      className="relative rounded-lg overflow-hidden"
      style={{ height: dimensions.height, backgroundColor: colors.bg }}
    >
      <ForceGraph2D
        key={theme}
        ref={graphRef as React.MutableRefObject<ForceGraphMethods | undefined>}
        width={dimensions.width}
        height={dimensions.height}
        graphData={graphData as { nodes: object[]; links: object[] }}
        nodeCanvasObject={
          paintNode as (node: object, ctx: CanvasRenderingContext2D, globalScale: number) => void
        }
        nodeCanvasObjectMode={() => 'replace'}
        linkCanvasObject={
          paintLink as (link: object, ctx: CanvasRenderingContext2D, globalScale: number) => void
        }
        linkCanvasObjectMode={() => 'replace'}
        onNodeClick={handleNodeClick as (node: object, event: MouseEvent) => void}
        onEngineStop={handleEngineStop}
        backgroundColor={colors.bg}
        cooldownTicks={50}
        warmupTicks={20}
        enableZoomInteraction={false}
        enablePanInteraction={false}
        enableNodeDrag={false}
        d3AlphaDecay={0.05}
        d3VelocityDecay={0.3}
      />
    </div>
  );
}

/**
 * Related entities section with list view and mini-graph visualization.
 * Designed to be reused across entity detail pages.
 */
export function RelatedEntitiesSection({
  entityId,
  entityName,
  entityType,
  related,
  maxItems = 5,
  title = 'Related',
}: RelatedEntitiesSectionProps) {
  const [view, setView] = useState<'list' | 'graph'>('list');

  const getEntityUrl = useCallback((id: string, type: string) => {
    if (type === 'task') return `/tasks/${id}`;
    if (type === 'project') return `/projects/${id}`;
    if (type === 'epic') return `/epics/${id}`;
    if (type === 'source') return `/sources/${id}`;
    return `/entities/${id}`;
  }, []);

  if (!related || related.length === 0) {
    return null;
  }

  const displayedItems = related.slice(0, maxItems);
  const hasMore = related.length > maxItems;

  return (
    <div className="space-y-3">
      {/* Header with view toggle */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-sc-fg-muted">{title}</h3>
        <div className="flex items-center gap-1 rounded-lg bg-sc-bg-secondary p-0.5">
          <button
            type="button"
            onClick={() => setView('list')}
            className={`p-1.5 rounded transition-colors ${
              view === 'list'
                ? 'bg-sc-bg-tertiary text-sc-fg-default'
                : 'text-sc-fg-muted hover:text-sc-fg-default'
            }`}
            title="List view"
          >
            <List className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={() => setView('graph')}
            className={`p-1.5 rounded transition-colors ${
              view === 'graph'
                ? 'bg-sc-bg-tertiary text-sc-fg-default'
                : 'text-sc-fg-muted hover:text-sc-fg-default'
            }`}
            title="Graph view"
          >
            <GraphUp className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Content */}
      {view === 'list' ? (
        <div className="space-y-2">
          {displayedItems.map(item => (
            <Link
              key={item.id}
              href={getEntityUrl(item.id, item.entity_type)}
              className="flex items-center gap-2 p-2 rounded-lg bg-sc-bg-secondary/50 hover:bg-sc-bg-secondary transition-colors group"
            >
              <RelationshipBadge type={item.relationship} direction={item.direction} size="xs" />
              <EntityBadge type={item.entity_type} size="sm" showIcon />
              <span className="flex-1 text-sm text-sc-fg-default truncate">{item.name}</span>
              <ArrowRight className="w-4 h-4 text-sc-fg-muted opacity-0 group-hover:opacity-100 transition-opacity" />
            </Link>
          ))}
          {hasMore && (
            <Link
              href={`/graph?selected=${entityId}`}
              className="block text-center text-sm text-sc-cyan hover:text-sc-cyan/80 py-2"
            >
              View all {related.length} connections in graph
            </Link>
          )}
        </div>
      ) : (
        <div className="relative">
          <MiniGraph
            entityId={entityId}
            entityName={entityName}
            entityType={entityType}
            related={displayedItems}
          />
          <Link
            href={`/graph?selected=${entityId}`}
            className="absolute bottom-2 right-2 text-xs text-sc-cyan hover:text-sc-cyan/80 bg-sc-bg-secondary/90 px-2 py-1 rounded"
          >
            Open in Graph
          </Link>
        </div>
      )}
    </div>
  );
}
