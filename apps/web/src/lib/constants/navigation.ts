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

export interface RouteConfigItem {
  label: string;
  href: string;
  icon: IconComponent;
  navLabel?: string;
  showInNavigation?: boolean;
}

export const ROUTE_CONFIG: Record<string, RouteConfigItem> = {
  '': {
    label: 'Home',
    href: '/',
    icon: LayoutDashboard,
    navLabel: 'Dashboard',
    showInNavigation: true,
  },
  memory: { label: 'Memory', href: '/memory', icon: Database, showInNavigation: true },
  projects: { label: 'Projects', href: '/projects', icon: FolderKanban, showInNavigation: true },
  epics: { label: 'Epics', href: '/epics', icon: Layers, showInNavigation: true },
  tasks: { label: 'Tasks', href: '/tasks', icon: ListTodo, showInNavigation: true },
  sources: { label: 'Sources', href: '/sources', icon: BookOpen, showInNavigation: true },
  archive: { label: 'Memory Captures', href: '/archive', icon: FileText },
  graph: { label: 'Graph', href: '/graph', icon: Network, showInNavigation: true },
  entities: { label: 'Entities', href: '/entities', icon: Boxes, showInNavigation: true },
  search: { label: 'Search', href: '/search', icon: Search, showInNavigation: true },
  settings: { label: 'Settings', href: '/settings', icon: Settings },
};

export const NAVIGATION: NavigationItem[] = Object.values(ROUTE_CONFIG)
  .filter(route => route.showInNavigation)
  .map(route => ({
    name: route.navLabel ?? route.label,
    href: route.href,
    icon: route.icon,
  }));

export function withProjectsContext(href: string, projects: string | null): string {
  if (!projects) {
    return href;
  }

  const separator = href.includes('?') ? '&' : '?';
  return `${href}${separator}projects=${projects}`;
}
