'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type { Entity, EntityCreate, EntityListResponse, EntityUpdate } from '../api/graph';
import { entitiesApi, graphApi } from '../api/graph';
import { TIMING } from '../constants/app';
import { queryKeys } from './query-keys';
import { invalidateByEntityType } from './shared';

export function useEntities(
  params?: Parameters<typeof entitiesApi.list>[0],
  initialData?: EntityListResponse
) {
  return useQuery({
    queryKey: queryKeys.entities.list(params),
    queryFn: () => entitiesApi.list(params),
    initialData,
    staleTime: TIMING.STALE_TIME,
    placeholderData: previousData => previousData,
  });
}

export function useEntity(
  id: string,
  initialData?: Entity,
  params?: Parameters<typeof entitiesApi.get>[1]
) {
  return useQuery({
    queryKey: queryKeys.entities.detail(id, params),
    queryFn: () => entitiesApi.get(id, params),
    enabled: !!id,
    initialData,
  });
}

export function useCreateEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (entity: EntityCreate) => entitiesApi.create(entity),
    onSuccess: (data, variables) => {
      // Use entity type from response (most accurate) or input
      const entityType = data.entity_type || variables.entity_type;
      invalidateByEntityType(queryClient, entityType, data.id, { includeStats: true });
    },
  });
}

export function useUpdateEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, updates }: { id: string; updates: EntityUpdate }) =>
      entitiesApi.update(id, updates),
    onSuccess: (data, { id }) => {
      // Use entity type from response
      invalidateByEntityType(queryClient, data.entity_type, id);
    },
  });
}

export function useDeleteEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => entitiesApi.delete(id),
    onSuccess: (_data, id) => {
      // Check cache for entity type before it's removed
      const cachedEntity = queryClient.getQueryData(queryKeys.entities.detail(id)) as
        | { entity_type?: string }
        | undefined;
      const entityType = cachedEntity?.entity_type;
      invalidateByEntityType(queryClient, entityType, id, { includeStats: true });
    },
  });
}

export function useHierarchicalGraph(params?: {
  max_nodes?: number;
  max_edges?: number;
  projects?: string[];
  types?: string[];
  refresh?: boolean;
  resolution?: 'overview' | 'detail';
  cluster_id?: string;
}) {
  return useQuery({
    queryKey: queryKeys.graph.hierarchical(params),
    queryFn: () => graphApi.hierarchical(params),
    staleTime: 5 * 60 * 1000,
    gcTime: 10 * 60 * 1000,
    placeholderData: previousData => previousData,
  });
}
