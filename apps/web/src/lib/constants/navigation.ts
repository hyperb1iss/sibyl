import {
  Archive,
  BookOpen,
  Boxes,
  FolderKanban,
  type IconComponent,
  Layers,
  LayoutDashboard,
  ListTodo,
  Network,
  Search,
} from '@/components/ui/icons';

export interface NavigationItem {
  name: string;
  href: string;
  icon: IconComponent;
}

export const NAVIGATION: NavigationItem[] = [
  { name: 'Dashboard', href: '/', icon: LayoutDashboard },
  { name: 'Projects', href: '/projects', icon: FolderKanban },
  { name: 'Epics', href: '/epics', icon: Layers },
  { name: 'Tasks', href: '/tasks', icon: ListTodo },
  { name: 'Sources', href: '/sources', icon: BookOpen },
  { name: 'Archive', href: '/archive', icon: Archive },
  { name: 'Graph', href: '/graph', icon: Network },
  { name: 'Entities', href: '/entities', icon: Boxes },
  { name: 'Search', href: '/search', icon: Search },
];

export function withProjectsContext(href: string, projects: string | null): string {
  if (!projects) {
    return href;
  }

  const separator = href.includes('?') ? '&' : '?';
  return `${href}${separator}projects=${projects}`;
}
