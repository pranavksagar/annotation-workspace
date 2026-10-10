/** Wire types for the annotation API. Versions are decimal strings (BIGINT on the server). */
export type Version = string;
export type AnnotationStatus = 'TO_DO' | 'IN_PROGRESS' | 'COMPLETED';
export type PreparationState = 'QUEUED' | 'RUNNING' | 'READY' | 'FAILED';
export type RegionKind = 'TEXT' | 'SYMBOL' | 'IGNORE';
export type Legibility = 'READABLE' | 'UNREADABLE';
export type Disposition = 'UNCLASSIFIED' | 'NON_REQUIREMENT' | 'SUPPORTING_CONTEXT';
export type Role = 'PRIMARY' | 'NOMINAL' | 'UPPER_TOLERANCE' | 'LOWER_TOLERANCE' | 'UNIT' | 'DATUM' | 'SYMBOL' | 'CONTEXT';
export type RequirementType = 'DIMENSION' | 'GEOMETRIC_TOLERANCE' | 'SURFACE_FINISH' | 'THREAD' | 'MATERIAL'
  | 'HEAT_TREATMENT' | 'COATING' | 'GEAR_SPLINE' | 'MANUFACTURING_NOTE' | 'OTHER';
export type Polygon = number[][];

export interface Region {
  id: string; page_id: string; kind: RegionKind; polygon: Polygon; transcription: string;
  symbol_code: string | null; legibility: Legibility; reading_angle_degrees: number;
  semantic_disposition: Disposition; origin: 'MANUAL' | 'OCR_ACCEPTED' | 'LEGACY_IMPORT'; origin_suggestion_id: string | null;
}

export interface StructuredValue {
  nominal?: string | null; upper_tolerance?: string | null; lower_tolerance?: string | null;
  unit?: string | null; note?: string | null;
}

export interface Group {
  id: string; primary_page_id: string;
  /** Assigned by the server; null until the creating save is acknowledged. */
  display_number: number | null;
  requirement_type: RequirementType; description: string | null; structured_value: StructuredValue | null;
}

export interface Member { group_id: string; region_id: string; role: Role; order_index: number }

export interface DocState {
  regions: Record<string, Region>;
  groups: Record<string, Group>;
  members: Record<string, Member>;
}

export type RegionFields = Pick<Region, 'kind' | 'polygon' | 'transcription' | 'symbol_code' | 'legibility'
  | 'reading_angle_degrees' | 'semantic_disposition'>;

export type Operation =
  | {type: 'region.create'; region: {id: string; page_id: string} & RegionFields}
  | {type: 'region.update'; region_id: string; changes: Partial<RegionFields>}
  | {type: 'region.delete'; region_id: string}
  | {type: 'group.create'; group: {id: string; primary_page_id: string; requirement_type: RequirementType;
      description?: string | null; structured_value?: StructuredValue | null}}
  | {type: 'group.update'; group_id: string; changes: Partial<Pick<Group, 'primary_page_id' | 'requirement_type' | 'description' | 'structured_value'>>}
  | {type: 'group.delete'; group_id: string}
  | {type: 'member.add'; group_id: string; region_id: string; role: Role; order_index: number}
  | {type: 'member.update'; group_id: string; region_id: string; role: Role; changes: {order_index: number}}
  | {type: 'member.remove'; group_id: string; region_id: string; role: Role};

export interface Page {
  id: string; page_number: number; coordinate_unit: 'PT' | 'PX'; coordinate_space_version: number;
  width: number; height: number; render_asset_id: string; thumbnail_asset_id: string;
  render_width_px: number; render_height_px: number; canonical_to_render_matrix: number[][];
}

export interface Drawing {
  id: string; filename: string; display_title: string | null; source_format: 'PDF' | 'PNG' | 'JPEG';
  annotation_status: AnnotationStatus; preparation_state: PreparationState; annotation_version: Version;
  page_count: number | null; last_annotation_page_id: string | null; preparation_error_code: string | null;
  preparation_error_message: string | null; created_at: string; annotation_updated_at: string; completed_at: string | null;
}

export interface DrawingDetail {
  drawing: Drawing; pages: Page[];
  preparation: {state: PreparationState; progress: {pages_done?: number; page_count?: number | null};
    error_code: string | null; error_message: string | null; retryable: boolean};
  lease: {available: boolean; retry_after_seconds: number};
  ocr_enabled?: boolean;
}

export interface OcrSuggestion {text: string; confidence: number}

export interface AnnotationDocument {
  drawing_id: string; annotation_version: Version; annotation_status: AnnotationStatus;
  regions: Region[]; groups: Group[]; members: Member[];
}

export interface LibraryItem {
  id: string; filename: string; page_count: number | null; annotation_status: AnnotationStatus;
  annotation_version: Version; preparation_state: PreparationState; preparation_error_message: string | null;
  preparation_progress: {pages_done?: number; page_count?: number | null}; thumbnail_asset_id: string | null;
  being_edited: boolean; created_at: string; annotation_updated_at: string; completed_at: string | null;
}

export interface LibraryPage {
  items: LibraryItem[]; counts: Record<AnnotationStatus | 'ALL', number>; next_cursor: string | null;
}

export interface MutationResult {
  mutation_id: string; operation_kind: 'EDIT' | 'COMPLETE' | 'REOPEN';
  previous_annotation_version: Version; annotation_version: Version; annotation_status: AnnotationStatus;
  saved_at: string; replayed?: boolean;
  changed?: {regions: Region[]; groups: Group[]; members: Member[]};
  deleted?: {region_ids: string[]; group_ids: string[]; members: {group_id: string; region_id: string; role: Role}[]};
  snapshot_id?: string; snapshot_sequence?: number; payload_sha256?: string;
}

export interface Receipt {
  mutation_id: string; operation_kind: 'EDIT' | 'COMPLETE' | 'REOPEN'; resulting_version: Version;
  result: MutationResult; created_at: string;
}

export interface Lease { lease_token: string; lease_generation: string; expires_at: string; lease_seconds: number;
  heartbeat_seconds: number; annotation_version: Version }

export interface BrowserProgress {
  page_id: string; center_x: number; center_y: number; zoom_relative_to_fit: number;
  selected_region_id: string | null; selected_group_id: string | null;
}

export interface CompletionIssue {
  code: string; message: string; page_id?: string; region_id?: string; group_id?: string; field?: string;
}

export interface Vocabulary {
  vocabulary_version: string; labeling_policy_version: string; requirement_types: RequirementType[];
  member_roles: Role[]; text_characters: {character: string; label: string}[];
  symbol_codes: {code: string; label: string; unicode: string | null}[];
}

/** A sealed request whose outcome may be unknown; persisted before it is sent. */
export interface SealedEnvelope {
  kind: 'EDIT' | 'COMPLETE' | 'REOPEN';
  mutation_id: string;
  expected_annotation_version: Version;
  operations?: Operation[];
  coverage?: {page_id: string; status: 'ANNOTATED' | 'NO_LABELABLE_CONTENT'}[];
  labeling_policy_version?: string;
}
