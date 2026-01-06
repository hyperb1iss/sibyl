'use client';

import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { useMemo } from 'react';
import type { IconComponent } from '@/components/ui/icons';

interface NavLinkProps {
  href: string;
  icon: IconComponent;
  children: React.ReactNode;
  onClick?: () => void;
  badge?: number;
}

export function NavLink({ href, icon: Icon, children, onClick, badge }: NavLinkProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const isActive = pathname === href;

  // Preserve project context across navigation
  const hrefWithContext = useMemo(() => {
    const projects = searchParams.get('projects');
    if (!projects) return href;
    const separator = href.includes('?') ? '&' : '?';
    return `${href}${separator}projects=${projects}`;
  }, [href, searchParams]);

  return (
    <Link
      href={hrefWithContext}
      onClick={onClick}
      className={`
        flex items-center gap-3 px-3 py-2.5 rounded-lg
        text-sm font-medium transition-all duration-200
        group relative
        ${
          isActive
            ? 'bg-sc-purple/10 text-sc-purple'
            : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50'
        }
      `}
    >
      {/* Active indicator glow */}
      {isActive && (
        <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 rounded-r-full bg-sc-purple shadow-[0_0_10px_rgba(225,53,255,0.5)]" />
      )}

      <Icon
        width={18}
        height={18}
        className={`transition-all duration-200 ${
          isActive
            ? 'text-sc-purple drop-shadow-[0_0_6px_rgba(225,53,255,0.5)]'
            : 'text-sc-cyan/70 group-hover:text-sc-cyan'
        }`}
      />

      <span className="flex-1">{children}</span>

      {/* Badge for counts */}
      {badge !== undefined && badge > 0 && (
        <span className="flex items-center justify-center min-w-[18px] h-[18px] px-1 text-[10px] font-bold rounded-full bg-sc-purple/20 text-sc-purple">
          {badge > 99 ? '99+' : badge}
        </span>
      )}
    </Link>
  );
}
