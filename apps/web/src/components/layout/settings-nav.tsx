'use client';

import { usePathname } from 'next/navigation';
import type { IconComponent } from '@/components/ui/icons';
import { Activity, Archive, Database, Flash, Settings, User, Users } from '@/components/ui/icons';
import { useMe } from '@/lib/hooks';
import { NavLink } from './nav-link';

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

const ADMIN_NAVIGATION: SettingsNavItem[] = [
  {
    name: 'AI Services',
    href: '/settings/admin/ai',
    icon: Flash,
    description: 'API keys and LLM settings',
  },
  {
    name: 'Backups',
    href: '/settings/admin/backups',
    icon: Archive,
    description: 'Backup management and archives',
  },
  {
    name: 'System',
    href: '/settings/admin/system',
    icon: Activity,
    description: 'Health and diagnostics',
  },
];

export function SettingsNav() {
  const pathname = usePathname();
  const { data: me } = useMe();

  // Check if user is admin or owner of current org
  const userRole = me?.org_role;
  const isAdmin = userRole === 'owner' || userRole === 'admin';

  return (
    <nav className="space-y-1">
      {SETTINGS_NAVIGATION.map(item => (
        <NavLink
          key={item.name}
          href={item.href}
          icon={item.icon}
          description={item.description}
          isActive={pathname === item.href || pathname.startsWith(`${item.href}/`)}
          preserveProjectsContext={false}
        >
          {item.name}
        </NavLink>
      ))}

      {/* Admin section - only visible to owners/admins */}
      {isAdmin && (
        <>
          <div className="pt-4 pb-2">
            <span className="px-3 text-[10px] font-semibold text-sc-fg-subtle uppercase tracking-wider">
              Administration
            </span>
          </div>
          {ADMIN_NAVIGATION.map(item => (
            <NavLink
              key={item.name}
              href={item.href}
              icon={item.icon}
              description={item.description}
              isActive={pathname === item.href || pathname.startsWith(`${item.href}/`)}
              preserveProjectsContext={false}
            >
              {item.name}
            </NavLink>
          ))}
        </>
      )}
    </nav>
  );
}
