import type { HierarchicalEdge, HierarchicalNode } from '@/lib/api';

export interface GraphNode extends HierarchicalNode {
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
  clusterColor?: string;
  entityColor?: string;
  degree?: number;
  isProject?: boolean;
  isNeighbor?: boolean;
  isSearchMatch?: boolean;
  zIndex?: number;
  __highlightTime?: number;
}

// d3-force mutates source/target from string IDs to node objects at runtime.
export interface GraphLink extends Omit<HierarchicalEdge, 'source' | 'target'> {
  source: string | number | GraphNode;
  target: string | number | GraphNode;
  sourceNode?: GraphNode;
  targetNode?: GraphNode;
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
  maxDegree: number;
  matchCount: number;
}

export interface CanvasColors {
  bg: string;
  fgPrimary: string;
  fgMuted: string;
}

export interface KnowledgeGraphRef {
  zoomIn: () => void;
  zoomOut: () => void;
  fitView: () => void;
  resetView: () => void;
}
