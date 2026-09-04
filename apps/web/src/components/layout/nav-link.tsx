'use client';

import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { useMemo } from 'react';
import type { IconComponent } from '@/components/ui/icons';
import { Tooltip } from '@/components/ui/tooltip';
import { withProjectsContext } from '@/lib/constants/navigation';

interface NavLinkProps {
  href: string;
  icon: IconComponent;
  children: React.ReactNode;
  description?: string;
  isActive?: boolean;
  preserveProjectsContext?: boolean;
  /** Icon-only rail mode: label moves to a tooltip and screen-reader text. */
  collapsed?: boolean;
  onClick?: () => void;
}

export function NavLink({
  href,
  icon: Icon,
  children,
  description,
  isActive,
  preserveProjectsContext = true,
  collapsed = false,
  onClick,
}: NavLinkProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const active = isActive ?? pathname === href;

  // Preserve project context across navigation
  const hrefWithContext = useMemo(() => {
    if (!preserveProjectsContext) {
      return href;
    }
    return withProjectsContext(href, searchParams.get('projects'));
  }, [href, preserveProjectsContext, searchParams]);

  const link = (
    <Link
      href={hrefWithContext}
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      className={`
        flex items-center gap-3 rounded-lg py-2.5
        text-sm font-medium transition-all duration-200
        group relative
        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan
        focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
        ${collapsed ? 'justify-center px-0' : 'px-3'}
        ${
          active
            ? 'bg-sc-purple/10 text-sc-purple'
            : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50'
        }
      `}
    >
      {/* Active indicator glow */}
      {active && (
        <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 rounded-r-full bg-sc-purple shadow-[0_0_10px_color-mix(in_oklch,var(--sc-purple)_50%,transparent)]" />
      )}

      <Icon
        width={18}
        height={18}
        className={`shrink-0 transition-all duration-200 ${
          active
            ? 'text-sc-purple drop-shadow-[0_0_6px_color-mix(in_oklch,var(--sc-purple)_50%,transparent)]'
            : 'text-sc-cyan/70 group-hover:text-sc-cyan'
        }`}
      />

      {collapsed ? (
        <span className="sr-only">{children}</span>
      ) : description ? (
        <div className="flex-1 min-w-0">
          <span className="block text-sm font-medium whitespace-nowrap">{children}</span>
          <span className="block text-xs text-sc-fg-subtle truncate">{description}</span>
        </div>
      ) : (
        <span className="flex-1 whitespace-nowrap">{children}</span>
      )}
    </Link>
  );

  if (!collapsed) {
    return link;
  }

  return (
    <Tooltip content={children} side="right">
      {link}
    </Tooltip>
  );
}
