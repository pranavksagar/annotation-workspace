import type { AnnotationDocument, BrowserProgress, CompletionIssue, DrawingDetail, Lease, LibraryPage, MutationResult,
  OcrSuggestion, Operation, Polygon, Receipt, SealedEnvelope, Vocabulary } from './annotationTypes';

const BASE = '/api/annotation';

/** Typed API error preserving HTTP status, machine-readable code and details. status 0 = network failure. */
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string,
              public details: CompletionIssue[] = [], public currentVersion?: string,
              public retryAfterSeconds?: number) {
    super(message);
  }
  get isNetwork() { return this.status === 0; }
  /** The request may or may not have been applied, and repeating it is safe. */
  get isRetryable() { return this.status === 0 || this.status === 429 || this.status >= 500; }
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = await response.json();
    const e = body?.error;
    if (e && typeof e.code === 'string') {
      return new ApiError(response.status, e.code, e.message ?? response.statusText, e.details ?? [],
        e.current_annotation_version, e.retry_after_seconds);
    }
  } catch { /* not JSON */ }
  return new ApiError(response.status, response.status >= 500 ? 'SERVICE_UNAVAILABLE' : 'HTTP_ERROR',
    response.status >= 500 ? 'The server could not be reached.' : response.statusText);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(BASE + path, {
      ...init,
      headers: init?.body instanceof FormData ? init.headers : {'Content-Type': 'application/json', ...init?.headers},
    });
  } catch {
    throw new ApiError(0, 'NETWORK_ERROR', 'The server could not be reached.');
  }
  if (!response.ok) throw await parseError(response);
  return response.json() as Promise<T>;
}

const json = (method: string, body: unknown): RequestInit => ({method, body: JSON.stringify(body)});

export const assetUrl = (assetId: string) => `${BASE}/assets/${assetId}`;

export interface Identity { browser_id: string; tab_id: string }
export interface LeaseCredentials { lease_token: string; lease_generation: string }

export const api = {
  vocabulary: () => request<Vocabulary>('/vocabulary'),
  listDrawings: (params: {q?: string; status?: string; cursor?: string | null}) => {
    const query = new URLSearchParams();
    if (params.q) query.set('q', params.q);
    if (params.status) query.set('status', params.status);
    if (params.cursor) query.set('cursor', params.cursor);
    return request<LibraryPage>(`/drawings?${query}`);
  },
  upload: (file: File, onProgress: (fraction: number) => void) =>
    new Promise<{drawing: DrawingDetail['drawing']; duplicate: boolean; message?: string}>((resolve, reject) => {
      const form = new FormData();
      form.append('file', file);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${BASE}/drawings`);
      xhr.upload.onprogress = event => event.lengthComputable && onProgress(event.loaded / event.total);
      xhr.onerror = () => reject(new ApiError(0, 'NETWORK_ERROR', 'Upload failed: the server could not be reached.'));
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else reject(new ApiError(xhr.status, body?.error?.code ?? 'HTTP_ERROR', body?.error?.message ?? 'Upload failed.'));
        } catch {
          reject(new ApiError(xhr.status, 'HTTP_ERROR', `Upload failed (${xhr.status}).`));
        }
      };
      xhr.send(form);
    }),
  detail: (id: string) => request<DrawingDetail>(`/drawings/${id}`),
  document: (id: string) => request<AnnotationDocument>(`/drawings/${id}/document`),
  suggestOcr: (id: string, pageId: string, polygon: Polygon, signal?: AbortSignal) =>
    request<OcrSuggestion>(`/drawings/${id}/ocr-suggestions`, {...json('POST', {page_id: pageId, polygon}), signal}),
  retryPreparation: (id: string) => request<DrawingDetail>(`/drawings/${id}/retry-preparation`, {method: 'POST'}),
  acquireLease: (id: string, identity: Identity) => request<Lease>(`/drawings/${id}/edit-session`, json('POST', identity)),
  heartbeat: (id: string, lease: LeaseCredentials) =>
    request<{expires_at: string; annotation_version: string}>(`/drawings/${id}/edit-session`, json('PATCH', lease)),
  releaseLease: (id: string, lease: LeaseCredentials, keepalive = false) =>
    request<{released: boolean}>(`/drawings/${id}/edit-session`, {...json('DELETE', lease), keepalive}),
  mutate: (id: string, envelope: {mutation_id: string; expected_annotation_version: string; operations: Operation[]},
           identity: Identity, lease: LeaseCredentials) =>
    request<MutationResult>(`/drawings/${id}/mutations`, json('POST', {...envelope, ...identity, ...lease})),
  receipt: async (id: string, mutationId: string): Promise<Receipt | null> => {
    try {
      return await request<Receipt>(`/drawings/${id}/mutations/${mutationId}`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  complete: (id: string, envelope: SealedEnvelope, identity: Identity, lease: LeaseCredentials) =>
    request<MutationResult>(`/drawings/${id}/complete`, json('POST', {
      mutation_id: envelope.mutation_id, expected_annotation_version: envelope.expected_annotation_version,
      labeling_policy_version: envelope.labeling_policy_version, coverage: envelope.coverage, ...identity, ...lease})),
  reopen: (id: string, envelope: SealedEnvelope, identity: Identity) =>
    request<MutationResult>(`/drawings/${id}/reopen`, json('POST', {
      mutation_id: envelope.mutation_id, expected_annotation_version: envelope.expected_annotation_version, ...identity})),
  getProgress: (id: string, browserId: string) =>
    request<{progress: BrowserProgress | null; last_annotation_page_id: string | null}>(
      `/drawings/${id}/browser-progress?browser_id=${encodeURIComponent(browserId)}`),
  putProgress: (id: string, browserId: string, progress: BrowserProgress) =>
    request<{saved: boolean}>(`/drawings/${id}/browser-progress`, json('PUT', {browser_id: browserId, ...progress})),
};
