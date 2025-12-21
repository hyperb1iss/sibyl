'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Fragment, useMemo } from 'react';
import {
  BookOpen,
  Boxes,
  ChevronRight,
  FileText,
  FolderKanban,
  type IconComponent,
  LayoutDashboard,
  ListTodo,
  Network,
  RefreshCw,
  Search,
} from '@/components/ui/icons';

interface BreadcrumbItem {
  label: string;
  href?: string;
  icon?: IconComponent;
}

interface BreadcrumbProps {
  items?: BreadcrumbItem[];
  /** Override automatic path-based breadcrumbs */
  custom?: boolean;
  /** Additional class names */
  className?: string;
}

// Route name mappings for automatic breadcrumbs
const ROUTE_NAMES: Record<string, { label: string; icon: IconComponent }> = {
  '': { label: 'Dashboard', icon: LayoutDashboard },
  projects: { label: 'Projects', icon: FolderKanban },
  tasks: { label: 'Tasks', icon: ListTodo },
  sources: { label: 'Sources', icon: BookOpen },
  documents: { label: 'Documents', icon: FileText },
  graph: { label: 'Graph', icon: Network },
  entities: { label: 'Entities', icon: Boxes },
  search: { label: 'Search', icon: Search },
  ingest: { label: 'Ingest', icon: RefreshCw },
};

export function Breadcrumb({ items, custom, className = '' }: BreadcrumbProps) {
  const pathname = usePathname();

  // Auto-generate breadcrumbs from pathname if not custom
  const breadcrumbs = useMemo(() => {
    if (custom && items) return items;

    const segments = pathname.split('/').filter(Boolean);
    const crumbs: BreadcrumbItem[] = [{ label: 'Dashboard', href: '/', icon: LayoutDashboard }];

    let currentPath = '';
    for (const segment of segments) {
      currentPath += `/${segment}`;
      const route = ROUTE_NAMES[segment];

      if (route) {
        crumbs.push({
          label: route.label,
          href: currentPath,
          icon: route.icon,
        });
      } else {
        // Dynamic segment (ID) - don't add href, it's current page
        crumbs.push({
          label: segment.length > 20 ? `${segment.slice(0, 8)}...` : segment,
        });
      }
    }

    return crumbs;
  }, [pathname, items, custom]);

  // Don't render if only home
  if (breadcrumbs.length <= 1) return null;

  return (
    <nav
      aria-label="Breadcrumb"
      className={`flex items-center gap-1.5 text-sm text-sc-fg-muted h-6 overflow-hidden ${className}`}
      style={{ viewTransitionName: 'breadcrumb' }}
    >
      {breadcrumbs.map((crumb, index) => {
        const Icon = crumb.icon;
        const isLast = index === breadcrumbs.length - 1;
        return (
          <Fragment key={crumb.href ?? crumb.label}>
            {index > 0 && (
              <ChevronRight width={14} height={14} className="text-sc-fg-subtle/50 shrink-0" aria-hidden="true" />
            )}
            {crumb.href && !isLast ? (
              <Link
                href={crumb.href}
                className="flex items-center gap-1.5 hover:text-sc-purple transition-colors shrink-0"
              >
                {Icon && <Icon width={14} height={14} />}
                <span className="hidden xs:inline">{crumb.label}</span>
              </Link>
            ) : (
              <span className={`flex items-center gap-1.5 text-sc-fg-primary font-medium ${isLast ? 'min-w-0 truncate' : 'shrink-0'}`}>
                {Icon && <Icon width={14} height={14} className="shrink-0" />}
                <span className={isLast ? 'truncate' : ''}>{crumb.label}</span>
              </span>
            )}
          </Fragment>
        );
      })}
    </nav>
  );
}

// Context-aware breadcrumb for entity detail pages
interface EntityBreadcrumbProps {
  entityType: 'project' | 'task' | 'entity' | 'source';
  entityName: string;
  parentProject?: { id: string; name: string };
}

export function EntityBreadcrumb({ entityType, entityName, parentProject }: EntityBreadcrumbProps) {
  const items: BreadcrumbItem[] = [{ label: 'Dashboard', href: '/', icon: LayoutDashboard }];

  // Add parent context based on entity type
  if (entityType === 'task') {
    items.push({ label: 'Tasks', href: '/tasks', icon: ListTodo });
    if (parentProject) {
      items.push({
        label: parentProject.name,
        href: `/tasks?project=${parentProject.id}`,
        icon: FolderKanban,
      });
    }
  } else if (entityType === 'project') {
    items.push({ label: 'Projects', href: '/projects', icon: FolderKanban });
  } else if (entityType === 'entity') {
    items.push({ label: 'Entities', href: '/entities', icon: Boxes });
  } else if (entityType === 'source') {
    items.push({ label: 'Sources', href: '/sources', icon: BookOpen });
  }

  items.push({ label: entityName });

  return <Breadcrumb items={items} custom />;
}
