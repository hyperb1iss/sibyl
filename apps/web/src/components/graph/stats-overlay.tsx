interface StatsOverlayProps {
  totalNodes: number;
  totalEdges: number;
  displayedNodes: number;
  displayedEdges: number;
  clusterCount: number;
}

export function StatsOverlay({
  totalNodes,
  totalEdges,
  displayedNodes,
  displayedEdges,
  clusterCount,
}: StatsOverlayProps) {
  const showingAll = displayedNodes >= totalNodes;

  return (
    <div className="absolute top-4 right-4 z-10 bg-sc-bg-elevated rounded-lg px-3 py-2 border border-sc-fg-subtle/20 hidden md:flex items-center gap-4 text-xs shadow-card">
      <div className="flex items-center gap-1.5">
        <span className="text-sc-purple font-bold">{totalNodes.toLocaleString()}</span>
        <span className="text-sc-fg-subtle">nodes</span>
        {!showingAll && (
          <span className="text-sc-fg-subtle/60">({displayedNodes.toLocaleString()})</span>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        <span className="text-sc-cyan font-bold">{totalEdges.toLocaleString()}</span>
        <span className="text-sc-fg-subtle">edges</span>
        {!showingAll && displayedEdges < totalEdges && (
          <span className="text-sc-fg-subtle/60">({displayedEdges.toLocaleString()})</span>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        <span className="text-sc-coral font-bold">{clusterCount}</span>
        <span className="text-sc-fg-subtle">clusters</span>
      </div>
    </div>
  );
}
