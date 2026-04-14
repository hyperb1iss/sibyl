'use client';

import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { useMemo } from 'react';
import type { IconComponent } from '@/components/ui/icons';
import { withProjectsContext } from '@/lib/constants';

interface NavLinkProps {
  href: string;
  icon: IconComponent;
  children: React.ReactNode;
  description?: string;
  isActive?: boolean;
  preserveProjectsContext?: boolean;
  onClick?: () => void;
}

export function NavLink({
  href,
  icon: Icon,
  children,
  description,
  isActive,
  preserveProjectsContext = true,
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

  return (
    <Link
      href={hrefWithContext}
      onClick={onClick}
      className={`
        flex items-center gap-3 px-3 py-2.5 rounded-lg
        text-sm font-medium transition-all duration-200
        group relative
        ${
          active
            ? 'bg-sc-purple/10 text-sc-purple'
            : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50'
        }
      `}
    >
      {/* Active indicator glow */}
      {active && (
        <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 rounded-r-full bg-sc-purple shadow-[0_0_10px_rgba(225,53,255,0.5)]" />
      )}

      <Icon
        width={18}
        height={18}
        className={`transition-all duration-200 ${
          active
            ? 'text-sc-purple drop-shadow-[0_0_6px_rgba(225,53,255,0.5)]'
            : 'text-sc-cyan/70 group-hover:text-sc-cyan'
        }`}
      />

      {description ? (
        <div className="flex-1 min-w-0">
          <span className="block text-sm font-medium">{children}</span>
          <span className="block text-xs text-sc-fg-subtle truncate">{description}</span>
        </div>
      ) : (
        <span className="flex-1">{children}</span>
      )}
    </Link>
  );
}
