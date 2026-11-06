'use client';

import { type Query, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type {
  ApiKeyCreateRequest,
  AuthMeResponse,
  AuthProvidersResponse,
  OnboardingChecklist,
  OrgCreateRequest,
  OrgListResponse,
  OrgUpdateRequest,
  PasswordChangeRequest,
  UserPreferences,
} from '../api/auth';
import { authApi, orgsApi, preferencesApi, securityApi } from '../api/auth';
import { TIMING } from '../constants/app';
import { queryKeys } from './query-keys';

export function useMe(options?: { enabled?: boolean; initialData?: AuthMeResponse }) {
  return useQuery({
    queryKey: queryKeys.auth.me,
    queryFn: () => authApi.me(),
    enabled: options?.enabled ?? true,
    retry: false,
    staleTime: TIMING.STALE_TIME,
    initialData: options?.initialData,
  });
}

export function useAuthProviders(options?: {
  enabled?: boolean;
  initialData?: AuthProvidersResponse;
}) {
  return useQuery({
    queryKey: queryKeys.auth.providers,
    queryFn: () => authApi.providers(),
    enabled: options?.enabled ?? true,
    retry: false,
    staleTime: TIMING.STALE_TIME,
    initialData: options?.initialData,
  });
}

export function useOrgs(options?: { enabled?: boolean; initialData?: OrgListResponse }) {
  return useQuery({
    queryKey: queryKeys.orgs.list,
    queryFn: () => orgsApi.list(),
    enabled: options?.enabled ?? true,
    retry: false,
    staleTime: TIMING.STALE_TIME,
    initialData: options?.initialData,
  });
}

/**
 * Queries that describe the person rather than the organization they are
 * in: their identity, sign-in providers, organizations, sessions, API keys
 * and preferences. Everything else is dropped on an organization switch.
 */
const USER_SCOPED_ROOTS = new Set(['auth', 'orgs', 'security', 'preferences']);

export function outlivesOrgSwitch(query: Query): boolean {
  const root = query.queryKey[0];
  return typeof root === 'string' && USER_SCOPED_ROOTS.has(root);
}

export function useSwitchOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (slug: string) => orgsApi.switch(slug),
    onSuccess: async () => {
      // Every other cached answer belongs to the organization just left, and
      // most query keys carry no organization, so a list here would always
      // miss one (Net and Team activity did). Cancel reads still in flight
      // for the old organization, then reset every such query: its data is
      // cleared, so nothing from the old organization can show, and a
      // mounted screen is told and refetches for the new one. Removing the
      // queries instead would leave a mounted screen holding the old data
      // until something else re-rendered it.
      const orgScoped = (query: Query) => !outlivesOrgSwitch(query);
      await queryClient.cancelQueries({ predicate: orgScoped });
      void queryClient.resetQueries({ predicate: orgScoped });
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.me });
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
    },
  });
}

export function useCreateOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: OrgCreateRequest) => orgsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.me });
    },
  });
}

export function useUpdateOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, data }: { slug: string; data: OrgUpdateRequest }) =>
      orgsApi.update(slug, data),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.detail(variables.slug) });
    },
  });
}

export function useDeleteOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (slug: string) => orgsApi.delete(slug),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.me });
    },
  });
}

export function useOrgMembers(slug: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.orgs.members(slug),
    queryFn: () => orgsApi.members.list(slug),
    enabled: options?.enabled ?? !!slug,
    retry: false,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useUpdateOrgMemberRole() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, userId, role }: { slug: string; userId: string; role: string }) =>
      orgsApi.members.updateRole(slug, userId, role),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.members(variables.slug) });
    },
  });
}

export function useRemoveOrgMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, userId }: { slug: string; userId: string }) =>
      orgsApi.members.remove(slug, userId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.members(variables.slug) });
    },
  });
}

export function useOrgInvitations(slug: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.orgs.invitations(slug),
    queryFn: () => orgsApi.invitations.list(slug),
    enabled: options?.enabled ?? !!slug,
    retry: false,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useCreateOrgInvitation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      slug,
      email,
      role,
      expiresDays,
    }: {
      slug: string;
      email: string;
      role: string;
      expiresDays?: number;
    }) =>
      orgsApi.invitations.create(slug, {
        email,
        role,
        expires_days: expiresDays,
      }),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.invitations(variables.slug) });
    },
  });
}

export function useDeleteOrgInvitation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, invitationId }: { slug: string; invitationId: string }) =>
      orgsApi.invitations.delete(slug, invitationId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.invitations(variables.slug) });
    },
  });
}

// =============================================================================
// Security Hooks (Sessions, API Keys, OAuth, Password)
// =============================================================================

export function useSessions() {
  return useQuery({
    queryKey: queryKeys.security.sessions,
    queryFn: () => securityApi.sessions.list(),
  });
}

export function useRevokeSession() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (sessionId: string) => securityApi.sessions.revoke(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.sessions });
    },
  });
}

export function useRevokeAllSessions() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => securityApi.sessions.revokeAll(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.sessions });
    },
  });
}

export function useApiKeys() {
  return useQuery({
    queryKey: queryKeys.security.apiKeys,
    queryFn: () => securityApi.apiKeys.list(),
  });
}

export function useCreateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: ApiKeyCreateRequest) => securityApi.apiKeys.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.apiKeys });
    },
  });
}

export function useRevokeApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (keyId: string) => securityApi.apiKeys.revoke(keyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.apiKeys });
    },
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (data: PasswordChangeRequest) => securityApi.changePassword(data),
  });
}

// =============================================================================
// Preferences Hooks
// =============================================================================

export function usePreferences() {
  return useQuery({
    queryKey: queryKeys.preferences,
    queryFn: () => preferencesApi.get(),
    staleTime: 5 * TIMING.STALE_TIME,
    refetchOnWindowFocus: false,
  });
}

export function useUpdatePreferences() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (preferences: Partial<UserPreferences>) => preferencesApi.update(preferences),
    onSuccess: data => {
      queryClient.setQueryData(queryKeys.preferences, data);
    },
  });
}

/**
 * Hook for tracking onboarding checklist progress.
 * Returns current state and methods to mark items complete.
 */
export function useOnboardingProgress() {
  const { data: prefsData, isLoading } = usePreferences();
  const updatePrefs = useUpdatePreferences();

  const checklist = prefsData?.preferences?.onboarding_checklist ?? {};

  const markComplete = (item: keyof OnboardingChecklist) => {
    if (checklist[item]) return; // Already complete
    updatePrefs.mutate({
      onboarding_checklist: {
        ...checklist,
        [item]: true,
      },
    });
  };

  const isAllComplete =
    checklist.connected_agent && checklist.added_source && checklist.tried_search;

  return {
    checklist,
    isLoading,
    isAllComplete,
    markComplete,
    markConnectedAgent: () => markComplete('connected_agent'),
    markAddedSource: () => markComplete('added_source'),
    markTriedSearch: () => markComplete('tried_search'),
  };
}
