'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import { Button, IconButton } from '@/components/ui/button';
import { Check, Edit, Plus, Trash, User, Users } from '@/components/ui/icons';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import {
  useCreateOrg,
  useDeleteOrg,
  useMe,
  useOrgMembers,
  useOrgs,
  useRemoveOrgMember,
  useSwitchOrg,
  useUpdateOrg,
  useUpdateOrgMemberRole,
} from '@/lib/hooks';

// Role options for member management
const ROLES = ['owner', 'admin', 'member'] as const;

function OrgSkeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      {[1, 2].map(i => (
        <div key={i} className="h-20 bg-sc-bg-highlight rounded-lg" />
      ))}
    </div>
  );
}

interface CreateOrgFormProps {
  onSuccess: () => void;
  onCancel: () => void;
}

function CreateOrgForm({ onSuccess, onCancel }: CreateOrgFormProps) {
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const createOrg = useCreateOrg();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    try {
      await createOrg.mutateAsync({ name: name.trim(), slug: slug.trim() || undefined });
      toast.success('Organization created');
      onSuccess();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to create organization');
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label
          htmlFor="org-name"
          className="block text-xs text-sc-fg-subtle uppercase tracking-wide mb-1"
        >
          Organization Name
        </label>
        <Input
          id="org-name"
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder="My Organization"
          autoFocus
        />
      </div>
      <div>
        <label
          htmlFor="org-slug"
          className="block text-xs text-sc-fg-subtle uppercase tracking-wide mb-1"
        >
          Slug (optional)
        </label>
        <Input
          id="org-slug"
          value={slug}
          onChange={e => setSlug(e.target.value)}
          placeholder="my-org"
        />
        <p className="text-xs text-sc-fg-subtle mt-1">
          URL-friendly identifier. Auto-generated from name if not provided.
        </p>
      </div>
      <div className="flex gap-2 justify-end">
        <Button variant="ghost" onClick={onCancel} type="button">
          Cancel
        </Button>
        <Button type="submit" loading={createOrg.isPending} disabled={!name.trim()}>
          Create Organization
        </Button>
      </div>
    </form>
  );
}

interface OrgMembersListProps {
  slug: string;
  currentUserId: string;
  userRole: string | null;
}

function OrgMembersList({ slug, currentUserId, userRole }: OrgMembersListProps) {
  const { data, isLoading } = useOrgMembers(slug);
  const updateRole = useUpdateOrgMemberRole();
  const removeMember = useRemoveOrgMember();
  const canManage = userRole === 'owner' || userRole === 'admin';

  if (isLoading) {
    return (
      <div className="flex items-center justify-center p-4">
        <Spinner size="sm" />
      </div>
    );
  }

  if (!data?.members.length) {
    return <p className="text-sc-fg-muted text-sm p-4">No members found.</p>;
  }

  const handleRoleChange = async (userId: string, newRole: string) => {
    try {
      await updateRole.mutateAsync({ slug, userId, role: newRole });
      toast.success('Role updated');
    } catch {
      toast.error('Failed to update role');
    }
  };

  const handleRemove = async (userId: string, userName: string | null) => {
    if (!confirm(`Remove ${userName || 'this member'} from the organization?`)) return;
    try {
      await removeMember.mutateAsync({ slug, userId });
      toast.success('Member removed');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to remove member');
    }
  };

  return (
    <div className="divide-y divide-sc-fg-subtle/10">
      {data.members.map(member => (
        <div key={member.user.id} className="flex items-center gap-3 py-3 px-1">
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
              {member.user.id === currentUserId && (
                <span className="ml-2 text-xs text-sc-purple">(you)</span>
              )}
            </p>
            <p className="text-xs text-sc-fg-muted truncate">{member.user.email}</p>
          </div>
          {canManage && member.user.id !== currentUserId ? (
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
            <span className="text-xs text-sc-fg-muted capitalize px-2 py-1 bg-sc-bg-highlight rounded">
              {member.role}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

interface OrgCardProps {
  org: {
    id: string;
    slug: string;
    name: string;
    is_personal: boolean;
    role: string | null;
  };
  isCurrent: boolean;
  currentUserId: string;
}

function OrgCard({ org, isCurrent, currentUserId }: OrgCardProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [showMembers, setShowMembers] = useState(false);
  const [editName, setEditName] = useState(org.name);
  const [editSlug, setEditSlug] = useState(org.slug);

  const switchOrg = useSwitchOrg();
  const updateOrg = useUpdateOrg();
  const deleteOrg = useDeleteOrg();

  const canEdit = org.role === 'owner' || org.role === 'admin';
  const canDelete = org.role === 'owner' && !org.is_personal;

  const handleSwitch = async () => {
    if (isCurrent) return;
    try {
      await switchOrg.mutateAsync(org.slug);
      toast.success(`Switched to ${org.name}`);
    } catch {
      toast.error('Failed to switch organization');
    }
  };

  const handleSaveEdit = async () => {
    try {
      await updateOrg.mutateAsync({
        slug: org.slug,
        data: { name: editName, slug: editSlug !== org.slug ? editSlug : undefined },
      });
      toast.success('Organization updated');
      setIsEditing(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to update');
    }
  };

  const handleDelete = async () => {
    if (!confirm(`Delete "${org.name}"? This cannot be undone.`)) return;
    try {
      await deleteOrg.mutateAsync(org.slug);
      toast.success('Organization deleted');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to delete');
    }
  };

  return (
    <div
      className={`bg-sc-bg-base rounded-lg border p-4 transition-all ${
        isCurrent
          ? 'border-sc-purple/50 shadow-lg shadow-sc-purple/10'
          : 'border-sc-fg-subtle/10 hover:border-sc-fg-subtle/30'
      }`}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <div
            className={`w-10 h-10 rounded-lg flex items-center justify-center ${
              isCurrent ? 'bg-sc-purple/20' : 'bg-sc-bg-highlight'
            }`}
          >
            <Users
              width={20}
              height={20}
              className={isCurrent ? 'text-sc-purple' : 'text-sc-fg-muted'}
            />
          </div>
          <div className="min-w-0">
            {isEditing ? (
              <div className="space-y-2">
                <Input
                  value={editName}
                  onChange={e => setEditName(e.target.value)}
                  placeholder="Organization name"
                  className="text-sm"
                />
                <Input
                  value={editSlug}
                  onChange={e => setEditSlug(e.target.value)}
                  placeholder="slug"
                  className="text-xs"
                />
              </div>
            ) : (
              <>
                <h3 className="font-semibold text-sc-fg-primary truncate">{org.name}</h3>
                <p className="text-xs text-sc-fg-muted">
                  {org.slug}
                  {org.is_personal && <span className="ml-2 text-sc-cyan">(personal)</span>}
                </p>
              </>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          {isCurrent && (
            <span className="flex items-center gap-1 text-xs text-sc-green">
              <Check width={12} height={12} />
              Current
            </span>
          )}
          {!isCurrent && (
            <Button
              variant="secondary"
              size="sm"
              onClick={handleSwitch}
              loading={switchOrg.isPending}
            >
              Switch
            </Button>
          )}
        </div>
      </div>

      {/* Role and edit controls */}
      <div className="mt-3 pt-3 border-t border-sc-fg-subtle/10 flex items-center justify-between">
        <span className="text-xs text-sc-fg-subtle capitalize">
          Role: <span className="text-sc-coral">{org.role}</span>
        </span>
        <div className="flex items-center gap-2">
          {isEditing ? (
            <>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setIsEditing(false);
                  setEditName(org.name);
                  setEditSlug(org.slug);
                }}
              >
                Cancel
              </Button>
              <Button size="sm" onClick={handleSaveEdit} loading={updateOrg.isPending}>
                Save
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" size="sm" onClick={() => setShowMembers(!showMembers)}>
                <Users width={14} height={14} />
                Members
              </Button>
              {canEdit && (
                <IconButton
                  icon={<Edit width={14} height={14} />}
                  label="Edit organization"
                  size="sm"
                  variant="ghost"
                  onClick={() => setIsEditing(true)}
                />
              )}
              {canDelete && (
                <IconButton
                  icon={<Trash width={14} height={14} />}
                  label="Delete organization"
                  size="sm"
                  variant="ghost"
                  onClick={handleDelete}
                  className="text-sc-red hover:text-sc-red"
                />
              )}
            </>
          )}
        </div>
      </div>

      {/* Members panel */}
      {showMembers && (
        <div className="mt-3 pt-3 border-t border-sc-fg-subtle/10">
          <OrgMembersList slug={org.slug} currentUserId={currentUserId} userRole={org.role} />
        </div>
      )}
    </div>
  );
}

export default function OrganizationsPage() {
  const [showCreate, setShowCreate] = useState(false);
  const { data: orgsData, isLoading, error } = useOrgs();
  const { data: me } = useMe();

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
          <div className="flex items-center gap-3 mb-4">
            <Users width={20} height={20} className="text-sc-purple" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">Organizations</h2>
          </div>
          <OrgSkeleton />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-sc-bg-base rounded-lg border border-sc-red/20 p-6">
        <p className="text-sc-red">Failed to load organizations. Please try again.</p>
      </div>
    );
  }

  const orgs = orgsData?.orgs || [];
  const currentOrgId = me?.organization?.id;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <Users width={20} height={20} className="text-sc-purple" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">Organizations</h2>
          </div>
          {!showCreate && (
            <Button
              variant="secondary"
              size="sm"
              icon={<Plus width={14} height={14} />}
              onClick={() => setShowCreate(true)}
            >
              New Organization
            </Button>
          )}
        </div>

        <p className="text-sc-fg-muted mb-6">
          Manage your organizations and switch between them. Each organization has its own knowledge
          graph and team members.
        </p>

        {/* Create form */}
        {showCreate && (
          <div className="mb-6 p-4 bg-sc-bg-highlight rounded-lg border border-sc-fg-subtle/10">
            <h3 className="text-sm font-medium text-sc-fg-primary mb-4">Create New Organization</h3>
            <CreateOrgForm
              onSuccess={() => setShowCreate(false)}
              onCancel={() => setShowCreate(false)}
            />
          </div>
        )}

        {/* Org list */}
        {orgs.length === 0 ? (
          <div className="text-center py-8">
            <Users width={32} height={32} className="mx-auto text-sc-fg-muted mb-3" />
            <p className="text-sc-fg-muted">No organizations yet.</p>
            <p className="text-sm text-sc-fg-subtle mt-1">
              Create your first organization to get started.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {orgs.map(org => (
              <OrgCard
                key={org.id}
                org={org}
                isCurrent={org.id === currentOrgId}
                currentUserId={me?.user?.id || ''}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
