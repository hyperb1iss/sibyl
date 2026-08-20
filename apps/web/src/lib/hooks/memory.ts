'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type {
  MemoryAuditListResponse,
  MemoryCorrectionRequest,
  MemorySourceInspectResponse,
  MemorySpaceListResponse,
  RawCapture,
  RawCaptureListResponse,
  RawCaptureReviewState,
  SessionBundleResponse,
  SourceAdapterListResponse,
  SourceImportResumeRequest,
  SourceImportStartRequest,
  SourceImportStatusResponse,
  SynthesisDraftRequest,
  SynthesisRequest,
} from '../api/memory';
import {
  memoryApi,
  rawCapturesApi,
  sessionApi,
  sourceImportsApi,
  synthesisApi,
} from '../api/memory';
import { queryKeys } from './query-keys';
import { useWebSocketStatus } from './realtime';

export function useRawCaptures(
  params?: Parameters<typeof rawCapturesApi.list>[0],
  options?: { enabled?: boolean; initialData?: RawCaptureListResponse }
) {
  return useQuery({
    queryKey: queryKeys.rawCaptures.list(params),
    queryFn: () => rawCapturesApi.list(params),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
  });
}

export function useRawCapture(
  id: string,
  options?: { enabled?: boolean; initialData?: RawCapture }
) {
  return useQuery({
    queryKey: queryKeys.rawCaptures.detail(id),
    queryFn: () => rawCapturesApi.get(id),
    enabled: (options?.enabled ?? true) && !!id,
    initialData: options?.initialData,
  });
}

export function useSessionBundle(
  params?: Parameters<typeof sessionApi.bundle>[0],
  options?: { enabled?: boolean; initialData?: SessionBundleResponse }
) {
  return useQuery({
    queryKey: queryKeys.session.bundle(params),
    queryFn: () => sessionApi.bundle(params),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
  });
}

export function useUpdateRawCaptureReviewState() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, reviewState }: { id: string; reviewState: RawCaptureReviewState }) =>
      rawCapturesApi.updateReviewState(id, reviewState),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.all });
      queryClient.setQueryData(queryKeys.rawCaptures.detail(variables.id), data);
    },
  });
}

export function useMemoryAudit(
  params?: Parameters<typeof memoryApi.audit.list>[0],
  options?: { enabled?: boolean; initialData?: MemoryAuditListResponse }
) {
  return useQuery({
    queryKey: queryKeys.memory.audit(params),
    queryFn: () => memoryApi.audit.list(params),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
  });
}

export function useMemorySpaces(options?: {
  enabled?: boolean;
  initialData?: MemorySpaceListResponse;
}) {
  return useQuery({
    queryKey: queryKeys.memory.spaces,
    queryFn: () => memoryApi.spaces.list(),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
  });
}

export function useMemorySourceImport(
  importId: string,
  options?: { enabled?: boolean; initialData?: SourceImportStatusResponse }
) {
  const enabled = (options?.enabled ?? true) && !!importId;
  const wsStatus = useWebSocketStatus(enabled);

  return useQuery({
    queryKey: queryKeys.memory.sourceImport(importId),
    queryFn: () => memoryApi.sourceImportStatus(importId),
    enabled,
    initialData: options?.initialData,
    refetchInterval: query => {
      if (wsStatus === 'connected') {
        return false;
      }
      const status = query.state.data?.status;
      return status === 'pending' || status === 'running' ? 2500 : false;
    },
  });
}

export function useSourceImportAdapters(options?: {
  enabled?: boolean;
  initialData?: SourceAdapterListResponse;
}) {
  return useQuery({
    queryKey: queryKeys.memory.sourceAdapters,
    queryFn: () => sourceImportsApi.adapters(),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
  });
}

export function useStartSourceImport() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: SourceImportStartRequest) => sourceImportsApi.start(request),
    onSuccess: data => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.all });
      queryClient.setQueryData(queryKeys.memory.sourceImport(data.import_id), data);
    },
  });
}

export function useResumeSourceImport() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      importId,
      request,
    }: {
      importId: string;
      request?: SourceImportResumeRequest;
    }) => sourceImportsApi.resume(importId, request),
    onSuccess: data => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.all });
      queryClient.setQueryData(queryKeys.memory.sourceImport(data.import_id), data);
    },
  });
}

export function useCancelSourceImport() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (importId: string) => sourceImportsApi.cancel(importId),
    onSuccess: data => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
      queryClient.setQueryData(queryKeys.memory.sourceImport(data.import_id), data);
    },
  });
}

export function useMemorySourceInspect(
  sourceId: string,
  options?: { enabled?: boolean; initialData?: MemorySourceInspectResponse }
) {
  return useQuery({
    queryKey: queryKeys.memory.sourceInspect(sourceId),
    queryFn: () => memoryApi.inspect.get(sourceId),
    enabled: (options?.enabled ?? true) && !!sourceId,
    initialData: options?.initialData,
  });
}

export function useSynthesisPlan() {
  return useMutation({
    mutationFn: (request: SynthesisRequest) => synthesisApi.plan(request),
  });
}

export function useSynthesisDraft() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: SynthesisDraftRequest) => synthesisApi.draft(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.all });
    },
  });
}

export function usePreviewMemoryCorrection() {
  return useMutation({
    mutationFn: ({ sourceId, request }: { sourceId: string; request: MemoryCorrectionRequest }) =>
      memoryApi.inspect.previewCorrection(sourceId, request),
  });
}

export function useApplyMemoryCorrection() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ sourceId, request }: { sourceId: string; request: MemoryCorrectionRequest }) =>
      memoryApi.inspect.applyCorrection(sourceId, request),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.memory.sourceInspect(variables.sourceId),
      });
      queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.all });
    },
  });
}
