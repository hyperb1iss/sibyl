'use client';

import { useRouter } from 'next/navigation';
import { useMemo } from 'react';

import { EditableSelect } from '@/components/editable/editable-select';
import { Folder, Users } from '@/components/ui/icons';
import { useMe, useOrgs, useSwitchOrg } from '@/lib/hooks';

export function OrgSwitcher() {
  const router = useRouter();
  const { data: me } = useMe();
  const { data: orgs } = useOrgs();
  const switchOrg = useSwitchOrg();

  const currentSlug = me?.organization?.slug;

  const options = useMemo(() => {
    return (orgs?.orgs ?? []).map(o => ({
      value: o.slug,
      label: o.name,
      icon: o.is_personal ? <Users width={14} height={14} /> : <Folder width={14} height={14} />,
      color: o.is_personal ? 'text-sc-purple' : 'text-sc-cyan',
    }));
  }, [orgs?.orgs]);

  if (!currentSlug || options.length === 0) return null;

  return (
    <div className="hidden md:flex items-center gap-2 px-2 py-1 rounded-full bg-sc-bg-highlight/40 border border-sc-fg-subtle/10">
      <span className="text-[10px] font-medium tracking-wide uppercase text-sc-fg-subtle">Org</span>
      <EditableSelect
        value={currentSlug}
        options={options}
        disabled={switchOrg.isPending || options.length < 2}
        align="end"
        renderValue={option => (
          <span className="inline-flex items-center gap-1.5 text-xs text-sc-fg-primary">
            <span className={option?.color}>{option?.icon}</span>
            <span className="max-w-[160px] truncate">{option?.label || currentSlug}</span>
          </span>
        )}
        onSave={async slug => {
          await switchOrg.mutateAsync(slug);
          router.refresh();
        }}
      />
    </div>
  );
}
