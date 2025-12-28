'use client';

import { ArrowLeft } from 'lucide-react';
import { useRef } from 'react';
import { KnowledgeGraph, type KnowledgeGraphRef } from './knowledge-graph';

interface ClusterDetailProps {
  clusterId: string;
  data: {
    nodes: Array<{
      id: string;
      label?: string;
      type?: string;
      color?: string;
      size?: number;
    }>;
    edges: Array<{
      id: string;
      source: string;
      target: string;
      type?: string;
      weight?: number;
    }>;
    node_count: number;
    edge_count: number;
  } | null;
  isLoading: boolean;
  onBack: () => void;
}

export function ClusterDetail({
  clusterId: _clusterId,
  data,
  isLoading,
  onBack,
}: ClusterDetailProps) {
  const graphRef = useRef<KnowledgeGraphRef>(null);

  if (isLoading) {
    return (
      <div className="relative h-full bg-sc-bg-dark">
        {/* Back button */}
        <button
          type="button"
          onClick={onBack}
          className="absolute top-4 left-4 z-10 flex items-center gap-2 px-4 py-2 bg-sc-bg-elevated/90 backdrop-blur-sm border border-sc-purple/30 rounded-lg text-sc-fg-primary hover:bg-sc-purple/20 transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          <span>Back to clusters</span>
        </button>

        <div className="flex items-center justify-center h-full">
          <div className="flex flex-col items-center gap-3">
            <div className="w-8 h-8 border-2 border-sc-purple border-t-transparent rounded-full animate-spin" />
            <div className="text-sc-fg-muted text-sm">Loading cluster nodes...</div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full">
      {/* Back button */}
      <button
        type="button"
        onClick={onBack}
        className="absolute top-4 left-4 z-10 flex items-center gap-2 px-4 py-2 bg-sc-bg-elevated/90 backdrop-blur-sm border border-sc-purple/30 rounded-lg text-sc-fg-primary hover:bg-sc-purple/20 transition-colors"
      >
        <ArrowLeft className="w-4 h-4" />
        <span>Back to clusters</span>
      </button>

      {/* Cluster info overlay */}
      <div className="absolute top-4 right-4 z-10 bg-sc-bg-elevated/90 backdrop-blur-sm rounded-xl p-4 border border-sc-purple/20 min-w-[140px]">
        <div className="text-sm text-sc-fg-muted">Cluster Nodes</div>
        <div className="text-2xl font-bold text-sc-purple">{data?.node_count ?? 0}</div>
        <div className="text-sm text-sc-fg-muted mt-2">Edges</div>
        <div className="text-2xl font-bold text-sc-cyan">{data?.edge_count ?? 0}</div>
      </div>

      {/* Graph controls */}
      <div className="absolute bottom-4 right-4 z-10 flex gap-2">
        <button
          type="button"
          onClick={() => graphRef.current?.zoomIn()}
          className="w-8 h-8 flex items-center justify-center bg-sc-bg-elevated/90 backdrop-blur-sm border border-sc-purple/20 rounded-lg text-sc-fg-primary hover:bg-sc-purple/20 transition-colors"
          title="Zoom in"
        >
          +
        </button>
        <button
          type="button"
          onClick={() => graphRef.current?.zoomOut()}
          className="w-8 h-8 flex items-center justify-center bg-sc-bg-elevated/90 backdrop-blur-sm border border-sc-purple/20 rounded-lg text-sc-fg-primary hover:bg-sc-purple/20 transition-colors"
          title="Zoom out"
        >
          -
        </button>
        <button
          type="button"
          onClick={() => graphRef.current?.fitView()}
          className="px-3 h-8 flex items-center justify-center bg-sc-bg-elevated/90 backdrop-blur-sm border border-sc-purple/20 rounded-lg text-sc-fg-primary text-sm hover:bg-sc-purple/20 transition-colors"
          title="Fit view"
        >
          Fit
        </button>
      </div>

      {/* Use existing KnowledgeGraph for node rendering */}
      <KnowledgeGraph ref={graphRef} data={data} />
    </div>
  );
}
