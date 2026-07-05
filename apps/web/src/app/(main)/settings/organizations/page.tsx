'use client';

import Link from 'next/link';
import { useState } from 'react';
import { toast } from 'sonner';
import { SettingsPageHeader } from '@/components/settings/primitives';
import { Button, IconButton } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Check, Edit, Plus, Trash, Users } from '@/components/ui/icons';
import { Input } from '@/components/ui/input';
import {
  useCreateOrg,
  useDeleteOrg,
  useMe,
  useOrgs,
  useSwitchOrg,
  useUpdateOrg,
} from '@/lib/hooks';

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

interface OrgCardProps {
  org: {
    id: string;
    slug: string;
    name: string;
    is_personal: boolean;
    role: string | null;
  };
  isCurrent: boolean;
}

function OrgCard({ org, isCurrent }: OrgCardProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState(org.name);
  const [editSlug, setEditSlug] = useState(org.slug);
  const [confirmDelete, setConfirmDelete] = useState(false);

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

  const handleConfirmDelete = async () => {
    try {
      await deleteOrg.mutateAsync(org.slug);
      toast.success('Organization deleted');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to delete');
    } finally {
      setConfirmDelete(false);
    }
  };

  return (
    <div
      className={`bg-sc-bg-elevated shadow-card rounded-lg border p-4 transition-colors duration-200 ${
        isCurrent
          ? 'border-sc-purple/50 shadow-glow-purple'
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
              <Link
                href="/settings/teams"
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-sc-fg-muted transition-colors hover:bg-sc-bg-highlight hover:text-sc-fg-primary"
              >
                <Users width={14} height={14} />
                Members
              </Link>
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
                  onClick={() => setConfirmDelete(true)}
                  className="text-sc-red hover:text-sc-red"
                />
              )}
            </>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete "${org.name}"?`}
        description="This permanently removes the organization and its knowledge graph. This cannot be undone."
        confirmLabel="Delete Organization"
        variant="danger"
        loading={deleteOrg.isPending}
        onConfirm={handleConfirmDelete}
      />
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
        <SettingsPageHeader
          icon={Users}
          title="Organizations"
          description="Switch between organizations or create new ones."
        />
        <OrgSkeleton />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <SettingsPageHeader
          icon={Users}
          title="Organizations"
          description="Switch between organizations or create new ones."
        />
        <div className="rounded-lg border border-sc-red/20 bg-sc-red/5 p-4 text-sm text-sc-red">
          Failed to load organizations. Please try again.
        </div>
      </div>
    );
  }

  const orgs = orgsData?.orgs || [];
  const currentOrgId = me?.organization?.id;

  return (
    <div className="space-y-6">
      <SettingsPageHeader
        icon={Users}
        title="Organizations"
        description="Each organization has its own knowledge graph and team."
        actions={
          !showCreate && (
            <Button
              variant="secondary"
              size="sm"
              icon={<Plus width={14} height={14} />}
              onClick={() => setShowCreate(true)}
            >
              New Organization
            </Button>
          )
        }
      />

      {showCreate && (
        <div className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-elevated shadow-card p-5">
          <h3 className="mb-4 text-sm font-medium text-sc-fg-primary">Create new organization</h3>
          <CreateOrgForm
            onSuccess={() => setShowCreate(false)}
            onCancel={() => setShowCreate(false)}
          />
        </div>
      )}

      {orgs.length === 0 ? (
        <div className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-elevated shadow-card p-10 text-center">
          <Users width={32} height={32} className="mx-auto mb-3 text-sc-fg-muted" />
          <p className="text-sc-fg-muted">No organizations yet.</p>
          <p className="mt-1 text-sm text-sc-fg-subtle">
            Create your first organization to get started.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {orgs.map(org => (
            <OrgCard key={org.id} org={org} isCurrent={org.id === currentOrgId} />
          ))}
        </div>
      )}
    </div>
  );
}
