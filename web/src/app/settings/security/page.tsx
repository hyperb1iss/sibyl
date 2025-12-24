'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import { Button, IconButton } from '@/components/ui/button';
import {
  Check,
  Clock,
  Command,
  Copy,
  Eye,
  Github,
  Plus,
  Settings,
  Trash,
  User,
  Xmark,
} from '@/components/ui/icons';
import { Input } from '@/components/ui/input';
import {
  useApiKeys,
  useChangePassword,
  useCreateApiKey,
  useOAuthConnections,
  useRemoveOAuthConnection,
  useRevokeAllSessions,
  useRevokeApiKey,
  useRevokeSession,
  useSessions,
} from '@/lib/hooks';

function SectionSkeleton() {
  return (
    <div className="space-y-3 animate-pulse">
      {[1, 2].map(i => (
        <div key={i} className="h-16 bg-sc-bg-highlight rounded-lg" />
      ))}
    </div>
  );
}

// =============================================================================
// Password Change Section
// =============================================================================

function PasswordSection() {
  const [isEditing, setIsEditing] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPasswords, setShowPasswords] = useState(false);
  const changePassword = useChangePassword();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (newPassword !== confirmPassword) {
      toast.error('Passwords do not match');
      return;
    }

    if (newPassword.length < 8) {
      toast.error('Password must be at least 8 characters');
      return;
    }

    try {
      await changePassword.mutateAsync({
        current_password: currentPassword,
        new_password: newPassword,
      });
      toast.success('Password changed successfully');
      setIsEditing(false);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to change password');
    }
  };

  const handleCancel = () => {
    setIsEditing(false);
    setCurrentPassword('');
    setNewPassword('');
    setConfirmPassword('');
  };

  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <Settings width={18} height={18} className="text-sc-purple" />
          <h3 className="font-semibold text-sc-fg-primary">Password</h3>
        </div>
        {!isEditing && (
          <Button variant="secondary" size="sm" onClick={() => setIsEditing(true)}>
            Change Password
          </Button>
        )}
      </div>

      {isEditing ? (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label
              htmlFor="current-password"
              className="block text-xs text-sc-fg-subtle uppercase tracking-wide mb-1"
            >
              Current Password
            </label>
            <div className="relative">
              <Input
                id="current-password"
                type={showPasswords ? 'text' : 'password'}
                value={currentPassword}
                onChange={e => setCurrentPassword(e.target.value)}
                placeholder="Enter current password"
                autoFocus
              />
            </div>
          </div>
          <div>
            <label
              htmlFor="new-password"
              className="block text-xs text-sc-fg-subtle uppercase tracking-wide mb-1"
            >
              New Password
            </label>
            <Input
              id="new-password"
              type={showPasswords ? 'text' : 'password'}
              value={newPassword}
              onChange={e => setNewPassword(e.target.value)}
              placeholder="Enter new password (min 8 characters)"
            />
          </div>
          <div>
            <label
              htmlFor="confirm-password"
              className="block text-xs text-sc-fg-subtle uppercase tracking-wide mb-1"
            >
              Confirm New Password
            </label>
            <Input
              id="confirm-password"
              type={showPasswords ? 'text' : 'password'}
              value={confirmPassword}
              onChange={e => setConfirmPassword(e.target.value)}
              placeholder="Confirm new password"
            />
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="show-passwords"
              checked={showPasswords}
              onChange={e => setShowPasswords(e.target.checked)}
              className="rounded border-sc-fg-subtle/30"
            />
            <label htmlFor="show-passwords" className="text-xs text-sc-fg-muted">
              Show passwords
            </label>
          </div>
          <div className="flex gap-2 justify-end">
            <Button variant="ghost" onClick={handleCancel} type="button">
              Cancel
            </Button>
            <Button
              type="submit"
              loading={changePassword.isPending}
              disabled={!currentPassword || !newPassword || !confirmPassword}
            >
              Update Password
            </Button>
          </div>
        </form>
      ) : (
        <p className="text-sc-fg-muted text-sm">Use a strong, unique password for your account.</p>
      )}
    </div>
  );
}

// =============================================================================
// Sessions Section
// =============================================================================

function SessionsSection() {
  const { data, isLoading, error } = useSessions();
  const revokeSession = useRevokeSession();
  const revokeAll = useRevokeAllSessions();

  const handleRevoke = async (sessionId: string) => {
    try {
      await revokeSession.mutateAsync(sessionId);
      toast.success('Session revoked');
    } catch {
      toast.error('Failed to revoke session');
    }
  };

  const handleRevokeAll = async () => {
    if (!confirm('Revoke all other sessions? You will remain logged in on this device.')) return;
    try {
      const result = await revokeAll.mutateAsync();
      toast.success(`Revoked ${result.revoked} session(s)`);
    } catch {
      toast.error('Failed to revoke sessions');
    }
  };

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <User width={18} height={18} className="text-sc-cyan" />
          <h3 className="font-semibold text-sc-fg-primary">Active Sessions</h3>
        </div>
        {data && data.sessions.length > 1 && (
          <Button
            variant="ghost"
            size="sm"
            onClick={handleRevokeAll}
            loading={revokeAll.isPending}
            className="text-sc-red hover:text-sc-red"
          >
            Revoke All Others
          </Button>
        )}
      </div>

      {isLoading && <SectionSkeleton />}

      {error && <p className="text-sc-red text-sm">Failed to load sessions. Please try again.</p>}

      {data && data.sessions.length === 0 && (
        <p className="text-sc-fg-muted text-sm">No active sessions found.</p>
      )}

      {data && data.sessions.length > 0 && (
        <div className="space-y-3">
          {data.sessions.map(session => (
            <div
              key={session.id}
              className={`flex items-center gap-3 p-3 rounded-lg border ${
                session.is_current
                  ? 'bg-sc-purple/10 border-sc-purple/30'
                  : 'bg-sc-bg-highlight border-sc-fg-subtle/10'
              }`}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-sc-fg-primary truncate">
                    {session.user_agent || 'Unknown Device'}
                  </p>
                  {session.is_current && (
                    <span className="flex items-center gap-1 text-xs text-sc-green">
                      <Check width={12} height={12} />
                      Current
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-xs text-sc-fg-muted mt-1">
                  {session.ip_address && <span>{session.ip_address}</span>}
                  <span className="flex items-center gap-1">
                    <Clock width={12} height={12} />
                    {session.last_used_at ? formatDate(session.last_used_at) : 'Never used'}
                  </span>
                </div>
              </div>
              {!session.is_current && (
                <IconButton
                  icon={<Xmark width={14} height={14} />}
                  label="Revoke session"
                  size="sm"
                  variant="ghost"
                  onClick={() => handleRevoke(session.id)}
                  className="text-sc-red hover:text-sc-red"
                />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// API Keys Section
// =============================================================================

function ApiKeysSection() {
  const [showCreate, setShowCreate] = useState(false);
  const [newKeyName, setNewKeyName] = useState('');
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const { data, isLoading, error } = useApiKeys();
  const createKey = useCreateApiKey();
  const revokeKey = useRevokeApiKey();

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newKeyName.trim()) return;

    try {
      const result = await createKey.mutateAsync({ name: newKeyName.trim() });
      setNewKey(result.key);
      setNewKeyName('');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to create API key');
    }
  };

  const handleCopyKey = async () => {
    if (!newKey) return;
    await navigator.clipboard.writeText(newKey);
    setCopied(true);
    toast.success('API key copied to clipboard');
    setTimeout(() => setCopied(false), 2000);
  };

  const handleDismissNewKey = () => {
    setNewKey(null);
    setShowCreate(false);
  };

  const handleRevoke = async (keyId: string, keyName: string) => {
    if (!confirm(`Revoke API key "${keyName}"? This cannot be undone.`)) return;
    try {
      await revokeKey.mutateAsync(keyId);
      toast.success('API key revoked');
    } catch {
      toast.error('Failed to revoke API key');
    }
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return 'Never';
    const date = new Date(dateStr);
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  };

  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <Command width={18} height={18} className="text-sc-coral" />
          <h3 className="font-semibold text-sc-fg-primary">API Keys</h3>
        </div>
        {!showCreate && !newKey && (
          <Button
            variant="secondary"
            size="sm"
            icon={<Plus width={14} height={14} />}
            onClick={() => setShowCreate(true)}
          >
            Create Key
          </Button>
        )}
      </div>

      <p className="text-sc-fg-muted text-sm mb-4">
        API keys allow programmatic access to the Sibyl API. Keep them secret.
      </p>

      {/* New Key Display */}
      {newKey && (
        <div className="mb-4 p-4 bg-sc-green/10 border border-sc-green/30 rounded-lg">
          <p className="text-sm font-medium text-sc-green mb-2">
            New API key created! Copy it now—it won't be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 px-3 py-2 bg-sc-bg-dark rounded font-mono text-sm text-sc-fg-primary break-all">
              {newKey}
            </code>
            <Button
              variant="secondary"
              size="sm"
              icon={copied ? <Check width={14} height={14} /> : <Copy width={14} height={14} />}
              onClick={handleCopyKey}
            >
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
          <div className="mt-3 flex justify-end">
            <Button variant="ghost" size="sm" onClick={handleDismissNewKey}>
              Done
            </Button>
          </div>
        </div>
      )}

      {/* Create Form */}
      {showCreate && !newKey && (
        <form
          onSubmit={handleCreate}
          className="mb-4 p-4 bg-sc-bg-highlight rounded-lg border border-sc-fg-subtle/10"
        >
          <label
            htmlFor="api-key-name"
            className="block text-xs text-sc-fg-subtle uppercase tracking-wide mb-1"
          >
            Key Name
          </label>
          <Input
            id="api-key-name"
            value={newKeyName}
            onChange={e => setNewKeyName(e.target.value)}
            placeholder="e.g., Production API, CI/CD Pipeline"
            autoFocus
          />
          <div className="flex gap-2 justify-end mt-3">
            <Button variant="ghost" onClick={() => setShowCreate(false)} type="button">
              Cancel
            </Button>
            <Button type="submit" loading={createKey.isPending} disabled={!newKeyName.trim()}>
              Create
            </Button>
          </div>
        </form>
      )}

      {isLoading && <SectionSkeleton />}

      {error && <p className="text-sc-red text-sm">Failed to load API keys. Please try again.</p>}

      {data && data.api_keys.length === 0 && !showCreate && !newKey && (
        <div className="text-center py-6">
          <Command width={28} height={28} className="mx-auto text-sc-fg-muted mb-2" />
          <p className="text-sc-fg-muted text-sm">No API keys yet.</p>
        </div>
      )}

      {data && data.api_keys.length > 0 && (
        <div className="space-y-3">
          {data.api_keys.map(key => (
            <div
              key={key.id}
              className="flex items-center gap-3 p-3 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/10"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-sc-fg-primary">{key.name}</p>
                <div className="flex items-center gap-3 text-xs text-sc-fg-muted mt-1">
                  <code className="text-sc-coral">{key.prefix}...</code>
                  <span>Created {formatDate(key.created_at)}</span>
                  {key.last_used_at && <span>Last used {formatDate(key.last_used_at)}</span>}
                  {key.expires_at && (
                    <span className="text-sc-yellow">Expires {formatDate(key.expires_at)}</span>
                  )}
                </div>
              </div>
              <IconButton
                icon={<Trash width={14} height={14} />}
                label="Revoke key"
                size="sm"
                variant="ghost"
                onClick={() => handleRevoke(key.id, key.name)}
                className="text-sc-red hover:text-sc-red"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// OAuth Connections Section
// =============================================================================

function OAuthConnectionsSection() {
  const { data, isLoading, error } = useOAuthConnections();
  const removeConnection = useRemoveOAuthConnection();

  const handleRemove = async (connectionId: string, provider: string) => {
    if (
      !confirm(`Disconnect ${provider}? You may need to re-authenticate to use this login method.`)
    )
      return;
    try {
      await removeConnection.mutateAsync(connectionId);
      toast.success(`${provider} disconnected`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to disconnect');
    }
  };

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const getProviderIcon = (provider: string) => {
    if (provider.toLowerCase() === 'github') {
      return <Github width={18} height={18} />;
    }
    return <User width={18} height={18} />;
  };

  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center gap-3 mb-4">
        <Eye width={18} height={18} className="text-sc-yellow" />
        <h3 className="font-semibold text-sc-fg-primary">Connected Accounts</h3>
      </div>

      <p className="text-sc-fg-muted text-sm mb-4">
        External accounts linked to your Sibyl account for authentication.
      </p>

      {isLoading && <SectionSkeleton />}

      {error && (
        <p className="text-sc-red text-sm">Failed to load connections. Please try again.</p>
      )}

      {data && data.connections.length === 0 && (
        <div className="text-center py-6">
          <User width={28} height={28} className="mx-auto text-sc-fg-muted mb-2" />
          <p className="text-sc-fg-muted text-sm">No connected accounts.</p>
        </div>
      )}

      {data && data.connections.length > 0 && (
        <div className="space-y-3">
          {data.connections.map(conn => (
            <div
              key={conn.id}
              className="flex items-center gap-3 p-3 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/10"
            >
              <div className="w-10 h-10 rounded-lg bg-sc-bg-dark flex items-center justify-center text-sc-fg-muted">
                {getProviderIcon(conn.provider)}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-sc-fg-primary capitalize">
                    {conn.provider}
                  </p>
                </div>
                <div className="flex items-center gap-3 text-xs text-sc-fg-muted mt-1">
                  {conn.email && <span>{conn.email}</span>}
                  {conn.name && <span>({conn.name})</span>}
                  <span>Connected {formatDate(conn.created_at)}</span>
                </div>
              </div>
              <IconButton
                icon={<Trash width={14} height={14} />}
                label="Disconnect"
                size="sm"
                variant="ghost"
                onClick={() => handleRemove(conn.id, conn.provider)}
                className="text-sc-red hover:text-sc-red"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Main Page
// =============================================================================

export default function SecurityPage() {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-center gap-3 mb-4">
          <Settings width={20} height={20} className="text-sc-purple" />
          <h2 className="text-lg font-semibold text-sc-fg-primary">Security</h2>
        </div>
        <p className="text-sc-fg-muted">
          Manage your password, active sessions, API keys, and connected accounts.
        </p>
      </div>

      <PasswordSection />
      <SessionsSection />
      <ApiKeysSection />
      <OAuthConnectionsSection />
    </div>
  );
}
