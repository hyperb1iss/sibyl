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
  /**
   * Where the member's domain bubble sat when it opened. A weak pull toward
   * it keeps an opened cluster inside its own territory instead of flinging
   * members across the map.
   */
  clusterAnchor?: { x: number; y: number; radius: number };
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
  /** Fly to the zoom that produces a whole level, for the toolbar shortcuts. */
  zoomToLevel: (level: 'domains' | 'entities') => void;
}
