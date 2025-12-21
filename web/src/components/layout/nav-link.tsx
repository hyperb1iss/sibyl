'use client';

import type { LucideIcon } from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

interface NavLinkProps {
  href: string;
  icon: LucideIcon;
  children: React.ReactNode;
}

export function NavLink({ href, icon: Icon, children }: NavLinkProps) {
  const pathname = usePathname();
  const isActive = pathname === href;

  return (
    <Link
      href={href}
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
        size={18}
        strokeWidth={isActive ? 2.5 : 2}
        className={`transition-all duration-200 ${
          isActive
            ? 'text-sc-purple drop-shadow-[0_0_6px_rgba(225,53,255,0.5)]'
            : 'text-sc-cyan/70 group-hover:text-sc-cyan'
        }`}
      />

      <span>{children}</span>
    </Link>
  );
}
