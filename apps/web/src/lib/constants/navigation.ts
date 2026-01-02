// =============================================================================
// Navigation & Quick Actions
// =============================================================================

// Navigation items for sidebar (legacy - use COMMAND_NAV in command-palette.tsx)
export const NAVIGATION = [
  { name: 'Dashboard', href: '/', icon: '◆' },
  { name: 'Projects', href: '/projects', icon: '◇' },
  { name: 'Tasks', href: '/tasks', icon: '☰' },
  { name: 'Sources', href: '/sources', icon: '▤' },
  { name: 'Graph', href: '/graph', icon: '⬡' },
  { name: 'Entities', href: '/entities', icon: '▣' },
  { name: 'Search', href: '/search', icon: '⌕' },
] as const;

// Quick actions for dashboard
export const QUICK_ACTIONS = [
  { label: 'Explore Graph', href: '/graph', icon: '⬡', color: 'purple' as const },
  { label: 'Browse Entities', href: '/entities', icon: '▣', color: 'cyan' as const },
  { label: 'Search Knowledge', href: '/search', icon: '⌕', color: 'coral' as const },
  { label: 'Add Source', href: '/sources', icon: '▤', color: 'yellow' as const },
] as const;
