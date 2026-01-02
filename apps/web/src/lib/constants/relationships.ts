// =============================================================================
// Relationship Type Styling
// =============================================================================

export const RELATIONSHIP_TYPES = [
  'APPLIES_TO',
  'REQUIRES',
  'CONFLICTS_WITH',
  'SUPERSEDES',
  'ENABLES',
  'BREAKS',
  'BELONGS_TO',
  'DEPENDS_ON',
  'BLOCKS',
  'ASSIGNED_TO',
  'REFERENCES',
  'MENTIONS',
  'ENCOUNTERED',
  'RELATED_TO',
] as const;

export type RelationshipType = (typeof RELATIONSHIP_TYPES)[number];

export const RELATIONSHIP_CONFIG: Record<string, { color: string; label: string; icon: string }> = {
  APPLIES_TO: { color: '#e135ff', label: 'Applies to', icon: '→' },
  REQUIRES: { color: '#80ffea', label: 'Requires', icon: '←' },
  CONFLICTS_WITH: { color: '#ff6363', label: 'Conflicts', icon: '⊗' },
  SUPERSEDES: { color: '#f1fa8c', label: 'Supersedes', icon: '↑' },
  ENABLES: { color: '#50fa7b', label: 'Enables', icon: '⚡' },
  BREAKS: { color: '#ff6363', label: 'Breaks', icon: '✕' },
  BELONGS_TO: { color: '#ff6ac1', label: 'Belongs to', icon: '⊂' },
  DEPENDS_ON: { color: '#80ffea', label: 'Depends on', icon: '⟵' },
  BLOCKS: { color: '#ff6363', label: 'Blocks', icon: '⊘' },
  ASSIGNED_TO: { color: '#e135ff', label: 'Assigned to', icon: '◎' },
  REFERENCES: { color: '#8b85a0', label: 'References', icon: '↗' },
  MENTIONS: { color: '#8b85a0', label: 'Mentions', icon: '↗' },
  ENCOUNTERED: { color: '#ffb86c', label: 'Encountered', icon: '◈' },
  RELATED_TO: { color: '#8b85a0', label: 'Related', icon: '↔' },
};

// Get relationship config with fallback
export function getRelationshipConfig(type: string) {
  return (
    RELATIONSHIP_CONFIG[type.toUpperCase()] ?? {
      color: '#8b85a0',
      label: type.replace(/_/g, ' ').toLowerCase(),
      icon: '↔',
    }
  );
}
