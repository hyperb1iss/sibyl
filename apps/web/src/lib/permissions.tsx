'use client';

/**
 * Permission hooks and components for role-based access control.
 *
 * Provides:
 * - useOrgRole: Get current user's org role
 * - useProjectRole: Get user's role in a specific project
 * - usePermission: Check if user can perform an action
 * - Can: Component that conditionally renders based on permissions
 */

import { createContext, type ReactNode, useContext, useMemo } from 'react';

import { useMe } from './hooks/auth';

// =============================================================================
// Types
// =============================================================================

export type OrgRole = 'owner' | 'admin' | 'member' | 'viewer';
export type ProjectRole =
  | 'project_owner'
  | 'project_maintainer'
  | 'project_contributor'
  | 'project_viewer';

// Role hierarchy (higher = more permissions)
const ORG_ROLE_LEVELS: Record<OrgRole, number> = {
  viewer: 10,
  member: 20,
  admin: 30,
  owner: 40,
};

const PROJECT_ROLE_LEVELS: Record<ProjectRole, number> = {
  project_viewer: 10,
  project_contributor: 20,
  project_maintainer: 30,
  project_owner: 40,
};

// Actions and their required roles
export type OrgAction =
  | 'org:view'
  | 'org:invite'
  | 'org:manage_members'
  | 'org:manage_settings'
  | 'org:delete';

export type ProjectAction =
  | 'project:view'
  | 'project:create_task'
  | 'project:edit_task'
  | 'project:delete_task'
  | 'project:manage_members'
  | 'project:manage_settings'
  | 'project:delete';

const ORG_ACTION_ROLES: Record<OrgAction, OrgRole> = {
  'org:view': 'viewer',
  'org:invite': 'admin',
  'org:manage_members': 'admin',
  'org:manage_settings': 'admin',
  'org:delete': 'owner',
};

const PROJECT_ACTION_ROLES: Record<ProjectAction, ProjectRole> = {
  'project:view': 'project_viewer',
  'project:create_task': 'project_contributor',
  'project:edit_task': 'project_contributor',
  'project:delete_task': 'project_maintainer',
  'project:manage_members': 'project_maintainer',
  'project:manage_settings': 'project_owner',
  'project:delete': 'project_owner',
};

// =============================================================================
// Context (for project-scoped permissions)
// =============================================================================

interface PermissionContextValue {
  projectId?: string;
  projectRole?: ProjectRole;
}

const PermissionContext = createContext<PermissionContextValue>({});

export function PermissionProvider({
  children,
  projectId,
  projectRole,
}: {
  children: ReactNode;
  projectId?: string;
  projectRole?: ProjectRole;
}) {
  const value = useMemo(() => ({ projectId, projectRole }), [projectId, projectRole]);
  return <PermissionContext.Provider value={value}>{children}</PermissionContext.Provider>;
}

// =============================================================================
// Hooks
// =============================================================================

/**
 * Get the current user's organization role.
 */
export function useOrgRole(): OrgRole | null {
  const { data } = useMe();
  return (data?.org_role as OrgRole) ?? null;
}

/**
 * Get the current user's role in a project (from context).
 */
export function useProjectRole(): ProjectRole | null {
  const { projectRole } = useContext(PermissionContext);
  return projectRole ?? null;
}

/**
 * Check if user has at least the required org role.
 */
export function useHasOrgRole(requiredRole: OrgRole): boolean {
  const currentRole = useOrgRole();
  if (!currentRole) return false;
  return ORG_ROLE_LEVELS[currentRole] >= ORG_ROLE_LEVELS[requiredRole];
}

/**
 * Check if user has at least the required project role.
 */
export function useHasProjectRole(requiredRole: ProjectRole): boolean {
  const currentRole = useProjectRole();
  const orgRole = useOrgRole();

  // Org admins/owners always have full project access
  if (orgRole && ORG_ROLE_LEVELS[orgRole] >= ORG_ROLE_LEVELS.admin) {
    return true;
  }

  if (!currentRole) return false;
  return PROJECT_ROLE_LEVELS[currentRole] >= PROJECT_ROLE_LEVELS[requiredRole];
}

/**
 * Check if user can perform an action.
 */
export function usePermission(action: OrgAction | ProjectAction): boolean {
  const orgRole = useOrgRole();
  const projectRole = useProjectRole();

  return useMemo(() => {
    // Org-level actions
    if (action in ORG_ACTION_ROLES) {
      const requiredRole = ORG_ACTION_ROLES[action as OrgAction];
      if (!orgRole) return false;
      return ORG_ROLE_LEVELS[orgRole] >= ORG_ROLE_LEVELS[requiredRole];
    }

    // Project-level actions
    if (action in PROJECT_ACTION_ROLES) {
      const requiredRole = PROJECT_ACTION_ROLES[action as ProjectAction];

      // Org admins/owners bypass project role checks
      if (orgRole && ORG_ROLE_LEVELS[orgRole] >= ORG_ROLE_LEVELS.admin) {
        return true;
      }

      if (!projectRole) return false;
      return PROJECT_ROLE_LEVELS[projectRole] >= PROJECT_ROLE_LEVELS[requiredRole];
    }

    return false;
  }, [action, orgRole, projectRole]);
}

/**
 * Hook that returns permission check helpers.
 */
export function usePermissions() {
  const orgRole = useOrgRole();
  const projectRole = useProjectRole();

  return useMemo(
    () => ({
      orgRole,
      projectRole,
      isOrgAdmin: orgRole ? ORG_ROLE_LEVELS[orgRole] >= ORG_ROLE_LEVELS.admin : false,
      isOrgOwner: orgRole === 'owner',
      can: (action: OrgAction | ProjectAction) => {
        // Org-level actions
        if (action in ORG_ACTION_ROLES) {
          const requiredRole = ORG_ACTION_ROLES[action as OrgAction];
          if (!orgRole) return false;
          return ORG_ROLE_LEVELS[orgRole] >= ORG_ROLE_LEVELS[requiredRole];
        }

        // Project-level actions
        if (action in PROJECT_ACTION_ROLES) {
          const requiredRole = PROJECT_ACTION_ROLES[action as ProjectAction];
          if (orgRole && ORG_ROLE_LEVELS[orgRole] >= ORG_ROLE_LEVELS.admin) return true;
          if (!projectRole) return false;
          return PROJECT_ROLE_LEVELS[projectRole] >= PROJECT_ROLE_LEVELS[requiredRole];
        }

        return false;
      },
    }),
    [orgRole, projectRole]
  );
}

// =============================================================================
// Components
// =============================================================================

interface CanProps {
  /** The action to check permission for */
  action: OrgAction | ProjectAction;
  /** Content to render if permission granted */
  children: ReactNode;
  /** Content to render if permission denied (optional) */
  fallback?: ReactNode;
}

/**
 * Conditionally render children based on permissions.
 *
 * @example
 * <Can action="org:invite">
 *   <Button>Invite Member</Button>
 * </Can>
 *
 * @example
 * <Can action="project:delete_task" fallback={<span>Read only</span>}>
 *   <Button>Delete</Button>
 * </Can>
 */
export function Can({ action, children, fallback = null }: CanProps) {
  const hasPermission = usePermission(action);
  return hasPermission ? children : fallback;
}

/**
 * Show content only to org admins.
 */
export function OrgAdminOnly({
  children,
  fallback = null,
}: {
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const isAdmin = useHasOrgRole('admin');
  return isAdmin ? children : fallback;
}

/**
 * Show content only to org owners.
 */
export function OrgOwnerOnly({
  children,
  fallback = null,
}: {
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const orgRole = useOrgRole();
  return orgRole === 'owner' ? children : fallback;
}
