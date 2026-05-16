'use client';

import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { memo, useCallback, useMemo } from 'react';
import { ChevronRight } from '@/components/ui/icons';
import { ROUTE_CONFIG, withProjectsContext } from '@/lib/constants/navigation';
import { type BreadcrumbItem, useBreadcrumbOverride, useSetBreadcrumb } from './breadcrumb-context';

export { ROUTE_CONFIG } from '@/lib/constants/navigation';
export type { BreadcrumbItem } from './breadcrumb-context';
export { useSetBreadcrumb } from './breadcrumb-context';

interface BreadcrumbProps {
  /** Custom breadcrumb items - if provided, overrides auto-generation. */
  items?: BreadcrumbItem[];
  /** Additional class names */
  className?: string;
}

/** Title-case an unknown path segment so breadcrumb labels read cleanly. */
function formatSegmentLabel(segment: string): string {
  if (segment.length > 20) return `${segment.slice(0, 8)}...`;
  return segment
    .split(/[-_]/)
    .map(word => (word ? word.charAt(0).toUpperCase() + word.slice(1) : word))
    .join(' ');
}

function useAutoCrumbs(pathname: string): BreadcrumbItem[] {
  return useMemo(() => {
    const segments = pathname.split('/').filter(Boolean);
    const crumbs: BreadcrumbItem[] = [
      { label: ROUTE_CONFIG[''].label, href: ROUTE_CONFIG[''].href, icon: ROUTE_CONFIG[''].icon },
    ];

    let currentPath = '';
    for (const segment of segments) {
      currentPath += `/${segment}`;
      const route = ROUTE_CONFIG[segment];

      if (route) {
        crumbs.push({ label: route.label, href: currentPath, icon: route.icon });
      } else {
        crumbs.push({ label: formatSegmentLabel(segment) });
      }
    }

    return crumbs;
  }, [pathname]);
}

function BreadcrumbInner({ items, className = '' }: BreadcrumbProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const override = useBreadcrumbOverride();

  const withContext = useCallback(
    (href: string) => withProjectsContext(href, searchParams.get('projects')),
    [searchParams]
  );

  const auto = useAutoCrumbs(pathname);

  // Precedence: explicit prop > context override > auto-derived from path.
  const breadcrumbs = items ?? override ?? auto;

  // On the home route the trail is just Home. Keep its href so the animation
  // key stays `0:/` like every other route — without it the crumb re-keys and
  // AnimatePresence redraws it instead of morphing on dashboard navigation.
  const renderable: BreadcrumbItem[] =
    breadcrumbs.length <= 1
      ? [
          {
            label: ROUTE_CONFIG[''].label,
            href: ROUTE_CONFIG[''].href,
            icon: ROUTE_CONFIG[''].icon,
          },
        ]
      : breadcrumbs;

  return (
    <nav
      aria-label="Breadcrumb"
      className={`flex h-6 items-center gap-1.5 overflow-hidden text-sm text-sc-fg-muted ${className}`}
      style={{ viewTransitionName: 'breadcrumb' }}
    >
      <AnimatePresence initial={false} mode="popLayout">
        {renderable.map((crumb, index) => {
          const Icon = crumb.icon;
          const isLast = index === renderable.length - 1;
          // Stable key per logical position + identity so React preserves the
          // DOM node across navigations whenever a crumb stays the same.
          const key = `${index}:${crumb.href ?? crumb.label}`;
          return (
            <motion.span
              key={key}
              layout
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 6 }}
              transition={{ duration: 0.18, ease: 'easeOut' }}
              className="flex items-center gap-1.5"
            >
              {index > 0 && (
                <ChevronRight
                  width={14}
                  height={14}
                  className="text-sc-fg-subtle/50 shrink-0"
                  aria-hidden="true"
                />
              )}
              {crumb.href && !isLast ? (
                <Link
                  href={withContext(crumb.href)}
                  className="flex items-center gap-1.5 shrink-0 transition-colors hover:text-sc-purple"
                >
                  {Icon && <Icon width={14} height={14} />}
                  <span className="hidden xs:inline">{crumb.label}</span>
                </Link>
              ) : (
                <span
                  className={`flex items-center gap-1.5 font-medium text-sc-fg-primary ${
                    isLast ? 'min-w-0 truncate' : 'shrink-0'
                  }`}
                >
                  {Icon && <Icon width={14} height={14} className="shrink-0" />}
                  <span className={isLast ? 'truncate' : ''}>{crumb.label}</span>
                </span>
              )}
            </motion.span>
          );
        })}
      </AnimatePresence>
    </nav>
  );
}

export const Breadcrumb = memo(BreadcrumbInner);

/**
 * Context-aware breadcrumb for entity detail pages.
 *
 * Pages call this in render — it pushes the custom trail into the persistent
 * layout breadcrumb via context and renders nothing. The breadcrumb in the
 * layout smoothly morphs into the new trail instead of being torn down.
 */
interface EntityBreadcrumbProps {
  entityType: 'project' | 'epic' | 'task' | 'entity' | 'source';
  entityName: string;
  parentProject?: { id: string; name: string };
}

export function EntityBreadcrumb({ entityType, entityName, parentProject }: EntityBreadcrumbProps) {
  const items = useMemo<BreadcrumbItem[]>(() => {
    const result: BreadcrumbItem[] = [
      { label: ROUTE_CONFIG[''].label, href: ROUTE_CONFIG[''].href, icon: ROUTE_CONFIG[''].icon },
    ];

    if (entityType === 'task') {
      result.push({
        label: ROUTE_CONFIG.tasks.label,
        href: ROUTE_CONFIG.tasks.href,
        icon: ROUTE_CONFIG.tasks.icon,
      });
      if (parentProject) {
        result.push({
          label: parentProject.name,
          href: `/tasks?project=${parentProject.id}`,
          icon: ROUTE_CONFIG.projects.icon,
        });
      }
    } else if (entityType === 'epic') {
      result.push({
        label: ROUTE_CONFIG.epics.label,
        href: ROUTE_CONFIG.epics.href,
        icon: ROUTE_CONFIG.epics.icon,
      });
      if (parentProject) {
        result.push({
          label: parentProject.name,
          href: `/epics?project=${parentProject.id}`,
          icon: ROUTE_CONFIG.projects.icon,
        });
      }
    } else if (entityType === 'project') {
      result.push({
        label: ROUTE_CONFIG.projects.label,
        href: ROUTE_CONFIG.projects.href,
        icon: ROUTE_CONFIG.projects.icon,
      });
    } else if (entityType === 'entity') {
      result.push({
        label: ROUTE_CONFIG.entities.label,
        href: ROUTE_CONFIG.entities.href,
        icon: ROUTE_CONFIG.entities.icon,
      });
    } else if (entityType === 'source') {
      result.push({
        label: ROUTE_CONFIG.sources.label,
        href: ROUTE_CONFIG.sources.href,
        icon: ROUTE_CONFIG.sources.icon,
      });
    }

    result.push({ label: entityName });
    return result;
  }, [entityType, entityName, parentProject]);

  useSetBreadcrumb(items);
  return null;
}
