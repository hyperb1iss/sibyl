'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import { SettingsPageHeader } from '@/components/settings/primitives';
import { Button, IconButton } from '@/components/ui/button';
import { Check, Copy, Eye, Send, Settings, Star, Trash, User, Users } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import {
  useCreateOrgInvitation,
  useDeleteOrgInvitation,
  useMe,
  useOrgInvitations,
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
const NON_OWNER_ROLES = ['admin', 'member'] as const;
const INVITE_ROLES = ['member', 'admin'] as const;

function inviteSignupUrl(acceptUrl: string | null): string {
  if (!acceptUrl) return '';
  const origin = typeof window === 'undefined' ? '' : window.location.origin;
  try {
    const url = new URL(acceptUrl, origin || undefined);
    const match = url.pathname.match(/\/invitations\/([^/]+)\/accept$/);
    if (!match) return acceptUrl;
    return `${origin || `${url.protocol}//${url.host}`}/login?invite=${encodeURIComponent(match[1])}`;
  } catch {
    return acceptUrl;
  }
}

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
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<(typeof INVITE_ROLES)[number]>('member');
  const [latestInviteUrl, setLatestInviteUrl] = useState('');
  const { data, isLoading } = useOrgMembers(org.slug, { enabled: expanded });
  const { data: invitationsData, isLoading: isLoadingInvites } = useOrgInvitations(org.slug, {
    enabled: expanded && (org.role === 'owner' || org.role === 'admin'),
  });
  const createInvitation = useCreateOrgInvitation();
  const deleteInvitation = useDeleteOrgInvitation();
  const updateRole = useUpdateOrgMemberRole();
  const removeMember = useRemoveOrgMember();
  const switchOrg = useSwitchOrg();

  const canManage = org.role === 'owner' || org.role === 'admin';
  const canManageOwnerRoles = org.role === 'owner';
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

  const handleInvite = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const email = inviteEmail.trim();
    if (!email) return;
    try {
      const result = await createInvitation.mutateAsync({
        slug: org.slug,
        email,
        role: inviteRole,
      });
      const inviteUrl = inviteSignupUrl(result.invitation.accept_url);
      setLatestInviteUrl(inviteUrl);
      setInviteEmail('');
      toast.success('Invitation created');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to create invitation');
    }
  };

  const handleCopyInvite = async (url: string) => {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      toast.success('Invite link copied');
    } catch {
      toast.error('Failed to copy invite link');
    }
  };

  const handleDeleteInvite = async (invitationId: string) => {
    try {
      await deleteInvitation.mutateAsync({ slug: org.slug, invitationId });
      toast.success('Invitation deleted');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to delete invitation');
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
      <div className="w-full p-4 flex items-center justify-between gap-3">
        <button
          type="button"
          className="flex items-center gap-3 flex-1 min-w-0 text-left"
          onClick={() => setExpanded(!expanded)}
        >
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
        </button>
        <div className="flex items-center gap-2">
          {!isCurrentOrg && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void handleSwitch()}
              loading={switchOrg.isPending}
            >
              Switch
            </Button>
          )}
          <button
            type="button"
            className="text-sc-fg-muted text-sm hover:text-sc-fg-primary transition-colors"
            onClick={() => setExpanded(!expanded)}
            aria-label={expanded ? 'Collapse members' : 'Expand members'}
          >
            {expanded ? '▲' : '▼'}
          </button>
        </div>
      </div>

      {/* Members List */}
      {expanded && (
        <div className="border-t border-sc-fg-subtle/10 p-4">
          {canManage && (
            <div className="mb-4 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/35 p-3">
              <form onSubmit={handleInvite} className="flex flex-col gap-2 sm:flex-row">
                <input
                  type="email"
                  value={inviteEmail}
                  onChange={event => setInviteEmail(event.target.value)}
                  placeholder="teammate@example.com"
                  className="min-w-0 flex-1 rounded border border-sc-fg-subtle/20 bg-sc-bg-base px-3 py-2 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle/50 focus:border-sc-purple/60 focus:outline-none focus:ring-2 focus:ring-sc-purple/20"
                />
                <select
                  value={inviteRole}
                  onChange={event =>
                    setInviteRole(event.target.value as (typeof INVITE_ROLES)[number])
                  }
                  className="rounded border border-sc-fg-subtle/20 bg-sc-bg-base px-3 py-2 text-sm text-sc-fg-secondary"
                >
                  {INVITE_ROLES.map(role => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
                <Button
                  type="submit"
                  size="sm"
                  icon={<Send width={14} height={14} />}
                  loading={createInvitation.isPending}
                  disabled={!inviteEmail.trim()}
                >
                  Invite
                </Button>
              </form>

              {latestInviteUrl && (
                <div className="mt-3 flex items-center gap-2">
                  <input
                    readOnly
                    value={latestInviteUrl}
                    className="min-w-0 flex-1 rounded border border-sc-cyan/20 bg-sc-bg-base px-3 py-2 text-xs text-sc-cyan"
                  />
                  <IconButton
                    icon={<Copy width={14} height={14} />}
                    label="Copy invite link"
                    size="sm"
                    onClick={() => void handleCopyInvite(latestInviteUrl)}
                  />
                </div>
              )}

              {isLoadingInvites ? (
                <div className="mt-3 flex justify-center">
                  <Spinner size="sm" />
                </div>
              ) : invitationsData?.invitations.length ? (
                <div className="mt-3 space-y-2 border-t border-sc-fg-subtle/10 pt-3">
                  {invitationsData.invitations.map(invitation => {
                    const inviteUrl = inviteSignupUrl(invitation.accept_url);
                    return (
                      <div
                        key={invitation.id}
                        className="flex items-center gap-2 rounded bg-sc-bg-base/70 px-2 py-2"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-xs font-medium text-sc-fg-secondary">
                            {invitation.email}
                          </p>
                          <p className="text-[11px] capitalize text-sc-fg-subtle">
                            {invitation.role}
                          </p>
                        </div>
                        {inviteUrl && (
                          <IconButton
                            icon={<Copy width={14} height={14} />}
                            label="Copy invite link"
                            size="sm"
                            variant="ghost"
                            onClick={() => void handleCopyInvite(inviteUrl)}
                          />
                        )}
                        <IconButton
                          icon={<Trash width={14} height={14} />}
                          label="Delete invitation"
                          size="sm"
                          variant="ghost"
                          onClick={() => void handleDeleteInvite(invitation.id)}
                          className="text-sc-red hover:text-sc-red"
                        />
                      </div>
                    );
                  })}
                </div>
              ) : null}
            </div>
          )}

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
                    {canManage && !isYou && (canManageOwnerRoles || member.role !== 'owner') ? (
                      <div className="flex items-center gap-2">
                        <select
                          value={member.role}
                          onChange={e => handleRoleChange(member.user.id, e.target.value)}
                          className="text-xs bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded px-2 py-1 text-sc-fg-secondary"
                        >
                          {(canManageOwnerRoles ? ROLES : NON_OWNER_ROLES).map(role => (
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
        <SettingsPageHeader
          icon={Users}
          title="Teams"
          description="Members and roles across your organizations."
        />
        <TeamsSkeleton />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <SettingsPageHeader
          icon={Users}
          title="Teams"
          description="Members and roles across your organizations."
        />
        <div className="rounded-lg border border-sc-red/20 bg-sc-red/5 p-4 text-sm text-sc-red">
          Failed to load teams. Please try again.
        </div>
      </div>
    );
  }

  const orgs = orgsData?.orgs || [];
  const currentOrgId = me?.organization?.id;
  const currentUserId = me?.user?.id || '';
  const currentOrg = orgs.find(o => o.id === currentOrgId);
  const otherOrgs = orgs.filter(o => o.id !== currentOrgId);

  return (
    <div className="space-y-6">
      <SettingsPageHeader
        icon={Users}
        title="Teams"
        description="Members and roles across your organizations. Expand to manage."
      />

      {orgs.length === 0 ? (
        <div className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base p-10 text-center">
          <Users width={32} height={32} className="mx-auto mb-3 text-sc-fg-muted" />
          <p className="text-sc-fg-muted">No organizations yet.</p>
          <p className="mt-1 text-sm text-sc-fg-subtle">
            Join or create an organization to collaborate with others.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {currentOrg && (
            <OrgMembersCard org={currentOrg} currentUserId={currentUserId} isCurrentOrg={true} />
          )}
          {otherOrgs.length > 0 && (
            <>
              {currentOrg && (
                <div className="px-1 pt-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-sc-fg-subtle">
                  Other organizations
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
  );
}
