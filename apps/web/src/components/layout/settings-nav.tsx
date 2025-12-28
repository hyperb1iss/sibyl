'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { IconComponent } from '@/components/ui/icons';
import { Database, Settings, User, Users } from '@/components/ui/icons';

interface SettingsNavItem {
  name: string;
  href: string;
  icon: IconComponent;
  description: string;
}

const SETTINGS_NAVIGATION: SettingsNavItem[] = [
  {
    name: 'Profile',
    href: '/settings/profile',
    icon: User,
    description: 'Your personal information',
  },
  {
    name: 'Preferences',
    href: '/settings/preferences',
    icon: Settings,
    description: 'Display and behavior settings',
  },
  {
    name: 'Security',
    href: '/settings/security',
    icon: Settings,
    description: 'Password, sessions, and API keys',
  },
  {
    name: 'Organizations',
    href: '/settings/organizations',
    icon: Users,
    description: 'Manage your organizations',
  },
  {
    name: 'Teams',
    href: '/settings/teams',
    icon: Users,
    description: 'Team membership and settings',
  },
  {
    name: 'Data',
    href: '/settings/data',
    icon: Database,
    description: 'Backup and restore your graph',
  },
];

export function SettingsNav() {
  const pathname = usePathname();

  return (
    <nav className="space-y-1">
      {SETTINGS_NAVIGATION.map(item => {
        const isActive = pathname === item.href || pathname.startsWith(`${item.href}/`);
        const Icon = item.icon;

        return (
          <Link
            key={item.name}
            href={item.href}
            className={`
              flex items-center gap-3 px-3 py-2.5 rounded-lg
              transition-all duration-200 group relative
              ${
                isActive
                  ? 'bg-sc-purple/10 text-sc-purple'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50'
              }
            `}
          >
            {/* Active indicator */}
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

            <div className="flex-1 min-w-0">
              <span className="block text-sm font-medium">{item.name}</span>
              <span className="block text-xs text-sc-fg-subtle truncate">{item.description}</span>
            </div>
          </Link>
        );
      })}
    </nav>
  );
}
