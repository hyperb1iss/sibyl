import { fetchApi } from './transport';

export interface AuthProvider {
  name: string;
  label: string;
  login_url: string;
}

export interface AuthProvidersResponse {
  local_auth_enabled: boolean;
  break_glass_enabled: boolean;
  providers: AuthProvider[];
}

export interface AuthMeResponse {
  user: {
    id: string;
    github_id: number | null;
    email: string | null;
    name: string;
    avatar_url: string | null;
  };
  organization: { id: string; slug: string; name: string } | null;
  org_role: string | null;
}

export interface OrgSummary {
  id: string;
  slug: string;
  name: string;
  is_personal: boolean;
  role: string | null;
}

export interface OrgListResponse {
  orgs: OrgSummary[];
}

export interface OrgSwitchResponse {
  organization: { id: string; slug: string; name: string };
  access_token: string;
}

export interface OrgCreateRequest {
  name: string;
  slug?: string;
}

export interface OrgUpdateRequest {
  name?: string;
  slug?: string;
}

export interface OrgCreateResponse {
  organization: { id: string; slug: string; name: string };
  access_token: string;
}

export interface OrgGetResponse {
  organization: { id: string; slug: string; name: string };
  role: string;
}

export interface OrgMember {
  user: {
    id: string;
    github_id: number | null;
    email: string | null;
    name: string | null;
    avatar_url: string | null;
  };
  role: string;
  created_at: string;
}

export interface OrgMembersResponse {
  members: OrgMember[];
}

export interface OrgInvitation {
  id: string;
  email: string;
  role: string;
  created_at: string | null;
  expires_at: string | null;
  accept_url: string | null;
}

export interface OrgInvitationsResponse {
  invitations: OrgInvitation[];
}

export interface OrgInvitationCreateRequest {
  email: string;
  role: string;
  expires_days?: number;
}

export interface OrgInvitationCreateResponse {
  invitation: OrgInvitation;
}

export interface Session {
  id: string;
  user_agent: string | null;
  ip_address: string | null;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  is_current: boolean;
}

export interface SessionsResponse {
  sessions: Session[];
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  project_ids: string[];
  memory_space_ids: string[];
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string | null;
}

export interface ApiKeysResponse {
  api_keys: ApiKey[];
}

export interface ApiKeyCreateRequest {
  name: string;
  scopes?: string[];
  project_ids?: string[];
  memory_space_ids?: string[];
  expires_in_days?: number;
}

export interface ApiKeyCreateResponse {
  api_key: ApiKey;
  key: string; // Full key, only shown once
}

interface ApiKeyBackendRecord {
  id: string;
  name: string;
  prefix?: string;
  key_prefix?: string;
  scopes?: string[];
  project_ids?: string[];
  memory_space_ids?: string[];
  last_used_at?: string | null;
  expires_at?: string | null;
  created_at?: string | null;
}

interface ApiKeysBackendResponse {
  keys: ApiKeyBackendRecord[];
}

interface ApiKeyCreateBackendResponse extends ApiKeyBackendRecord {
  api_key: string;
}

function normalizeApiKey(record: ApiKeyBackendRecord): ApiKey {
  return {
    id: record.id,
    name: record.name,
    prefix: record.prefix ?? record.key_prefix ?? '',
    scopes: record.scopes ?? [],
    project_ids: record.project_ids ?? [],
    memory_space_ids: record.memory_space_ids ?? [],
    last_used_at: record.last_used_at ?? null,
    expires_at: record.expires_at ?? null,
    created_at: record.created_at ?? null,
  };
}

export interface PasswordChangeRequest {
  current_password: string;
  new_password: string;
}

export interface PasswordResetRequest {
  email: string;
}

export interface PasswordResetConfirmRequest {
  token: string;
  new_password: string;
}

// Onboarding checklist state
export interface OnboardingChecklist {
  connected_agent?: boolean;
  added_source?: boolean;
  tried_search?: boolean;
}

// User Preferences (flexible dict stored on user)
export interface UserPreferences {
  theme?: 'light' | 'dark' | 'system';
  locale?: string;
  timezone?: string;
  graphShowLabels?: boolean;
  graphDefaultZoom?: number;
  dashboardDefaultView?: 'grid' | 'list';
  notifyOnTaskAssigned?: boolean;
  notifyOnMention?: boolean;
  is_onboarded?: boolean; // Has user completed onboarding wizard
  onboarding_checklist?: OnboardingChecklist;
  [key: string]: unknown; // Allow additional preferences
}

export interface PreferencesResponse {
  preferences: UserPreferences;
}

// User Profile (editable account fields)
export interface UserProfile {
  id: string;
  email: string | null;
  name: string | null;
  bio: string | null;
  timezone: string | null;
  avatar_url: string | null;
  email_verified_at: string | null;
  created_at: string;
}

export const authApi = {
  providers: () => fetchApi<AuthProvidersResponse>('/auth/providers'),
  me: () => fetchApi<AuthMeResponse>('/auth/me'),
  logout: () =>
    fetchApi<void>('/auth/logout', {
      method: 'POST',
    }),
};

export const securityApi = {
  // Sessions
  sessions: {
    list: async () => ({
      sessions: await fetchApi<Session[]>('/users/me/sessions'),
    }),
    revoke: (sessionId: string) =>
      fetchApi<void>(`/users/me/sessions/${sessionId}`, {
        method: 'DELETE',
      }).then(() => ({ success: true })),
    revokeAll: () =>
      fetchApi<{ revoked: number }>('/users/me/sessions', {
        method: 'DELETE',
      }),
  },

  // API Keys
  apiKeys: {
    list: async () => {
      const response = await fetchApi<ApiKeysBackendResponse>('/auth/api-keys');
      return { api_keys: response.keys.map(normalizeApiKey) };
    },
    create: async (data: ApiKeyCreateRequest) => {
      const response = await fetchApi<ApiKeyCreateBackendResponse>('/auth/api-keys', {
        method: 'POST',
        body: JSON.stringify({
          name: data.name,
          scopes: data.scopes,
          project_ids: data.project_ids,
          memory_space_ids: data.memory_space_ids,
          expires_days: data.expires_in_days,
        }),
      });
      return {
        api_key: normalizeApiKey(response),
        key: response.api_key,
      };
    },
    revoke: (keyId: string) =>
      fetchApi<{ success: boolean }>(`/auth/api-keys/${keyId}/revoke`, {
        method: 'POST',
      }),
  },

  // Password
  changePassword: (data: PasswordChangeRequest) =>
    fetchApi<void>('/users/me/password', {
      method: 'POST',
      body: JSON.stringify(data),
    }).then(() => ({ success: true })),
  requestPasswordReset: (data: PasswordResetRequest) =>
    fetchApi<{ message: string }>('/users/password/reset', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  confirmPasswordReset: (data: PasswordResetConfirmRequest) =>
    fetchApi<void>('/users/password/reset/confirm', {
      method: 'POST',
      body: JSON.stringify(data),
    }).then(() => ({ success: true })),
};

export const preferencesApi = {
  get: () => fetchApi<PreferencesResponse>('/users/me/preferences'),
  update: (preferences: Partial<UserPreferences>) =>
    fetchApi<PreferencesResponse>('/users/me/preferences', {
      method: 'PATCH',
      body: JSON.stringify({ preferences }),
    }),
};

export const profileApi = {
  get: () => fetchApi<UserProfile>('/users/me/profile'),
  update: (data: Partial<UserProfile>) =>
    fetchApi<UserProfile>('/users/me/profile', {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
};

export const orgsApi = {
  list: () => fetchApi<OrgListResponse>('/orgs'),
  get: (slug: string) => fetchApi<OrgGetResponse>(`/orgs/${encodeURIComponent(slug)}`),
  create: (data: OrgCreateRequest) =>
    fetchApi<OrgCreateResponse>('/orgs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (slug: string, data: OrgUpdateRequest) =>
    fetchApi<{ organization: { id: string; slug: string; name: string } }>(
      `/orgs/${encodeURIComponent(slug)}`,
      {
        method: 'PATCH',
        body: JSON.stringify(data),
      }
    ),
  delete: (slug: string) =>
    fetchApi<void>(`/orgs/${encodeURIComponent(slug)}`, {
      method: 'DELETE',
    }),
  switch: (slug: string) =>
    fetchApi<OrgSwitchResponse>(`/orgs/${encodeURIComponent(slug)}/switch`, {
      method: 'POST',
    }),
  members: {
    list: (slug: string) =>
      fetchApi<OrgMembersResponse>(`/orgs/${encodeURIComponent(slug)}/members`),
    add: (slug: string, userId: string, role: string) =>
      fetchApi<{ user_id: string; role: string }>(`/orgs/${encodeURIComponent(slug)}/members`, {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, role }),
      }),
    updateRole: (slug: string, userId: string, role: string) =>
      fetchApi<{ user_id: string; role: string }>(
        `/orgs/${encodeURIComponent(slug)}/members/${userId}`,
        {
          method: 'PATCH',
          body: JSON.stringify({ role }),
        }
      ),
    remove: (slug: string, userId: string) =>
      fetchApi<{ success: boolean }>(`/orgs/${encodeURIComponent(slug)}/members/${userId}`, {
        method: 'DELETE',
      }),
  },
  invitations: {
    list: (slug: string) =>
      fetchApi<OrgInvitationsResponse>(`/orgs/${encodeURIComponent(slug)}/invitations`),
    create: (slug: string, data: OrgInvitationCreateRequest) =>
      fetchApi<OrgInvitationCreateResponse>(`/orgs/${encodeURIComponent(slug)}/invitations`, {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    delete: (slug: string, invitationId: string) =>
      fetchApi<{ success: boolean }>(
        `/orgs/${encodeURIComponent(slug)}/invitations/${invitationId}`,
        {
          method: 'DELETE',
        }
      ),
  },
};
