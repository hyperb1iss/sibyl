'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import { Button, IconButton } from '@/components/ui/button';
import { Check, Eye, Settings, Star, Trash, User, Users } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import {
  useMe,
  useOrgMembers,
  useOrgs,
  useRemoveOrgMember,
  useSwitchOrg,
  useUpdateOrgMemberRole,
} from '@/lib/hooks';

const ROLE_CONFIG = {
  owner: { icon: Star, color: 'text-sc-yellow', label: 'Owner' },
  admin: { icon: Settings, color: 'text-sc-purple', label: 'Admin' },
  member: { icon: User, color: 'text-sc-cyan', label: 'Member' },
  viewer: { icon: Eye, color: 'text-sc-fg-muted', label: 'Viewer' },
} as const;

const ROLES = ['owner', 'admin', 'member'] as const;

interface OrgMembersCardProps {
  org: {
    id: string;
    slug: string;
    name: string;
    is_personal: boolean;
    role: string | null;
  };
  currentUserId: string;
  isCurrentOrg: boolean;
}

function OrgMembersCard({ org, currentUserId, isCurrentOrg }: OrgMembersCardProps) {
  const [expanded, setExpanded] = useState(isCurrentOrg);
  const { data, isLoading } = useOrgMembers(org.slug, { enabled: expanded });
  const updateRole = useUpdateOrgMemberRole();
  const removeMember = useRemoveOrgMember();
  const switchOrg = useSwitchOrg();

  const canManage = org.role === 'owner' || org.role === 'admin';
  const roleConfig = ROLE_CONFIG[org.role as keyof typeof ROLE_CONFIG] ?? ROLE_CONFIG.member;
  const RoleIcon = roleConfig.icon;

  const handleRoleChange = async (userId: string, newRole: string) => {
    try {
      await updateRole.mutateAsync({ slug: org.slug, userId, role: newRole });
      toast.success('Role updated');
    } catch {
      toast.error('Failed to update role');
    }
  };

  const handleRemove = async (userId: string, userName: string | null) => {
    if (!confirm(`Remove ${userName || 'this member'} from ${org.name}?`)) return;
    try {
      await removeMember.mutateAsync({ slug: org.slug, userId });
      toast.success('Member removed');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to remove member');
    }
  };

  const handleSwitch = async () => {
    try {
      await switchOrg.mutateAsync(org.slug);
      toast.success(`Switched to ${org.name}`);
    } catch {
      toast.error('Failed to switch organization');
    }
  };

  return (
    <div
      className={`bg-sc-bg-base rounded-lg border transition-all ${
        isCurrentOrg
          ? 'border-sc-purple/50 shadow-lg shadow-sc-purple/10'
          : 'border-sc-fg-subtle/10 hover:border-sc-fg-subtle/30'
      }`}
    >
      {/* Header */}
      <button
        type="button"
        className="w-full p-4 flex items-center justify-between gap-3 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <div
            className={`w-10 h-10 rounded-lg flex items-center justify-center ${
              isCurrentOrg ? 'bg-sc-purple/20' : 'bg-sc-bg-highlight'
            }`}
          >
            <Users
              width={20}
              height={20}
              className={isCurrentOrg ? 'text-sc-purple' : 'text-sc-fg-muted'}
            />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="font-semibold text-sc-fg-primary truncate">{org.name}</h3>
              {isCurrentOrg && (
                <span className="flex items-center gap-1 text-xs text-sc-green">
                  <Check width={12} height={12} />
                  Current
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 mt-0.5">
              <span className={`flex items-center gap-1 text-xs ${roleConfig.color}`}>
                <RoleIcon width={12} height={12} />
                {roleConfig.label}
              </span>
              {org.is_personal && <span className="text-xs text-sc-fg-subtle">(personal)</span>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {!isCurrentOrg && (
            <Button
              variant="secondary"
              size="sm"
              onClick={e => {
                e.stopPropagation();
                void handleSwitch();
              }}
              loading={switchOrg.isPending}
            >
              Switch
            </Button>
          )}
          <span className="text-sc-fg-muted text-sm">{expanded ? '▲' : '▼'}</span>
        </div>
      </button>

      {/* Members List */}
      {expanded && (
        <div className="border-t border-sc-fg-subtle/10 p-4">
          {isLoading ? (
            <div className="flex items-center justify-center py-4">
              <Spinner size="sm" />
            </div>
          ) : !data?.members.length ? (
            <p className="text-sc-fg-muted text-sm text-center py-4">No members found.</p>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between mb-3">
                <span className="text-xs text-sc-fg-subtle uppercase tracking-wide">
                  {data.members.length} member{data.members.length !== 1 ? 's' : ''}
                </span>
              </div>
              {data.members.map(member => {
                const memberRoleConfig =
                  ROLE_CONFIG[member.role as keyof typeof ROLE_CONFIG] ?? ROLE_CONFIG.member;
                const MemberRoleIcon = memberRoleConfig.icon;
                const isYou = member.user.id === currentUserId;

                return (
                  <div
                    key={member.user.id}
                    className="flex items-center gap-3 p-2 rounded-lg hover:bg-sc-bg-highlight/50 transition-colors"
                  >
                    {member.user.avatar_url ? (
                      <img
                        src={member.user.avatar_url}
                        alt=""
                        className="w-8 h-8 rounded-full border border-sc-fg-subtle/20"
                      />
                    ) : (
                      <div className="w-8 h-8 rounded-full bg-sc-bg-highlight flex items-center justify-center">
                        <User width={14} height={14} className="text-sc-fg-muted" />
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-sc-fg-primary truncate">
                        {member.user.name || member.user.email || 'Unknown'}
                        {isYou && <span className="ml-2 text-xs text-sc-purple">(you)</span>}
                      </p>
                      <p className="text-xs text-sc-fg-muted truncate">{member.user.email}</p>
                    </div>
                    {canManage && !isYou ? (
                      <div className="flex items-center gap-2">
                        <select
                          value={member.role}
                          onChange={e => handleRoleChange(member.user.id, e.target.value)}
                          className="text-xs bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded px-2 py-1 text-sc-fg-secondary"
                        >
                          {ROLES.map(role => (
                            <option key={role} value={role}>
                              {role}
                            </option>
                          ))}
                        </select>
                        <IconButton
                          icon={<Trash width={14} height={14} />}
                          label="Remove member"
                          size="sm"
                          variant="ghost"
                          onClick={() => handleRemove(member.user.id, member.user.name)}
                          className="text-sc-red hover:text-sc-red"
                        />
                      </div>
                    ) : (
                      <span className={`flex items-center gap-1 text-xs ${memberRoleConfig.color}`}>
                        <MemberRoleIcon width={12} height={12} />
                        {memberRoleConfig.label}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TeamsSkeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      {[1, 2].map(i => (
        <div key={i} className="h-20 bg-sc-bg-highlight rounded-lg" />
      ))}
    </div>
  );
}

export default function TeamsPage() {
  const { data: orgsData, isLoading, error } = useOrgs();
  const { data: me } = useMe();

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
          <div className="flex items-center gap-3 mb-4">
            <Users width={20} height={20} className="text-sc-purple" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">Teams</h2>
          </div>
          <TeamsSkeleton />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-sc-bg-base rounded-lg border border-sc-red/20 p-6">
        <p className="text-sc-red">Failed to load teams. Please try again.</p>
      </div>
    );
  }

  const orgs = orgsData?.orgs || [];
  const currentOrgId = me?.organization?.id;
  const currentUserId = me?.user?.id || '';

  // Separate current org from others for better UX
  const currentOrg = orgs.find(o => o.id === currentOrgId);
  const otherOrgs = orgs.filter(o => o.id !== currentOrgId);

  return (
    <div className="space-y-6">
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-center gap-3 mb-4">
          <Users width={20} height={20} className="text-sc-purple" />
          <h2 className="text-lg font-semibold text-sc-fg-primary">Teams</h2>
        </div>
        <p className="text-sc-fg-muted mb-6">
          View and manage team members across your organizations. Expand each organization to see
          members and manage roles.
        </p>

        {orgs.length === 0 ? (
          <div className="text-center py-8">
            <Users width={32} height={32} className="mx-auto text-sc-fg-muted mb-3" />
            <p className="text-sc-fg-muted">No organizations yet.</p>
            <p className="text-sm text-sc-fg-subtle mt-1">
              Join or create an organization to collaborate with others.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Current org first */}
            {currentOrg && (
              <OrgMembersCard org={currentOrg} currentUserId={currentUserId} isCurrentOrg={true} />
            )}

            {/* Other orgs */}
            {otherOrgs.length > 0 && (
              <>
                {currentOrg && (
                  <div className="text-xs text-sc-fg-subtle uppercase tracking-wide pt-2">
                    Other Organizations
                  </div>
                )}
                {otherOrgs.map(org => (
                  <OrgMembersCard
                    key={org.id}
                    org={org}
                    currentUserId={currentUserId}
                    isCurrentOrg={false}
                  />
                ))}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
