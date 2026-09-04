import {
  BookOpen,
  Boxes,
  Database,
  FileText,
  FolderKanban,
  type IconComponent,
  Layers,
  LayoutDashboard,
  ListTodo,
  Network,
  Search,
  Settings,
} from '@/components/ui/icons';

export interface NavigationItem {
  name: string;
  href: string;
  icon: IconComponent;
}

/**
 * Sidebar sections, in display order. `system` items (Settings) render in the
 * sidebar footer rather than the main list.
 */
export type NavigationSectionId = 'overview' | 'work' | 'memory' | 'explore' | 'system';

export interface NavigationSection {
  id: NavigationSectionId;
  label: string;
  items: NavigationItem[];
}

export interface RouteConfigItem {
  label: string;
  href: string;
  icon: IconComponent;
  navLabel?: string;
  showInNavigation?: boolean;
  section?: NavigationSectionId;
}

const SECTION_ORDER: { id: NavigationSectionId; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'work', label: 'Work' },
  { id: 'memory', label: 'Memory' },
  { id: 'explore', label: 'Explore' },
  { id: 'system', label: 'System' },
];

export const ROUTE_CONFIG: Record<string, RouteConfigItem> = {
  '': {
    label: 'Home',
    href: '/',
    icon: LayoutDashboard,
    navLabel: 'Dashboard',
    showInNavigation: true,
    section: 'overview',
  },
  projects: {
    label: 'Projects',
    href: '/projects',
    icon: FolderKanban,
    showInNavigation: true,
    section: 'work',
  },
  epics: { label: 'Epics', href: '/epics', icon: Layers, showInNavigation: true, section: 'work' },
  tasks: {
    label: 'Tasks',
    href: '/tasks',
    icon: ListTodo,
    showInNavigation: true,
    section: 'work',
  },
  memory: {
    label: 'Memory',
    href: '/memory',
    icon: Database,
    showInNavigation: true,
    section: 'memory',
  },
  sources: {
    label: 'Sources',
    href: '/sources',
    icon: BookOpen,
    showInNavigation: true,
    section: 'memory',
  },
  archive: { label: 'Memory Captures', href: '/archive', icon: FileText },
  entities: {
    label: 'Entities',
    href: '/entities',
    icon: Boxes,
    showInNavigation: true,
    section: 'memory',
  },
  graph: {
    label: 'Graph',
    href: '/graph',
    icon: Network,
    showInNavigation: true,
    section: 'memory',
  },
  search: {
    label: 'Search',
    href: '/search',
    icon: Search,
    showInNavigation: true,
    section: 'explore',
  },
  settings: {
    label: 'Settings',
    href: '/settings',
    icon: Settings,
    showInNavigation: true,
    section: 'system',
  },
};

const NAVIGATION_ROUTES = Object.values(ROUTE_CONFIG).filter(route => route.showInNavigation);

function toNavigationItem(route: RouteConfigItem): NavigationItem {
  return {
    name: route.navLabel ?? route.label,
    href: route.href,
    icon: route.icon,
  };
}

/** Flat navigation list (sidebar order). Includes Settings under `system`. */
export const NAVIGATION: NavigationItem[] = NAVIGATION_ROUTES.map(toNavigationItem);

/** Navigation grouped into sidebar sections; empty sections are dropped. */
export const NAVIGATION_SECTIONS: NavigationSection[] = SECTION_ORDER.map(section => ({
  ...section,
  items: NAVIGATION_ROUTES.filter(route => (route.section ?? 'overview') === section.id).map(
    toNavigationItem
  ),
})).filter(section => section.items.length > 0);

export function withProjectsContext(href: string, projects: string | null): string {
  if (!projects) {
    return href;
  }

  const separator = href.includes('?') ? '&' : '?';
  return `${href}${separator}projects=${projects}`;
}
