/**
 * Тонкий fetch-обёртка для бэкенд-API. Все запросы относительные
 * (`/api/...`) — в dev next.config.ts проксирует на :8765, в проде FastAPI
 * сам отдаёт фронт + API из одного origin.
 */

import type {
  ArtifactDTO,
  ExcelHeroCharacter,
  FrameDTO,
  MontageBoardDTO,
  MontageBoardMeta,
  GenerationConfigPreset,
  GenerationConfigPresetSettings,
  HITLDTO,
  ProjectDetail,
  ProjectSummary,
  SidebarLayout,
  PromptDTO,
  WorkflowDetail,
  WorkflowNode,
  WorkflowEdge,
  WorkflowRunDetail,
  WorkflowSummary,
  NodeGroupSummary,
  NodeGroupDetail,
} from "./types";
import type { BlockSelection } from "./prompt-styles";
import type { ShotMenuDTO } from "./shot-menu";

export interface StepTemplateBlock {
  number: number;
  title: string;
  body: string;
}

// ---- KIE Create типы (каталог моделей kie.ai) ----
export interface KieField {
  name: string;
  label: string;
  kind:
    | "text"
    | "textarea"
    | "select"
    | "toggle"
    | "number"
    | "images"
    | "videos"
    | "audios"
    | "dialogue";
  required?: boolean;
  default?: unknown;
  options?: string[];
  min?: number;
  max?: number;
  step?: number;
  max_items?: number;
  desc?: string;
  show_if?: Record<string, unknown>;
}

export interface KiePricingRule {
  when: Record<string, unknown>;
  credits: number;
}

export interface KieModelSpec {
  id: string;
  label: string;
  category: string;
  /** В какой тип Create-пикера попадает: image | video | audio */
  media?: string;
  desc: string;
  /** Понятное объяснение «как работает» (для утилит/звуков) */
  hint?: string;
  is_top?: boolean;
  isTop?: boolean;
  badge?: string;
  result: "video" | "image" | "audio" | "text";
  fields: KieField[];
  pricing: {
    unit: "gen" | "sec" | "1k_chars";
    rules: KiePricingRule[];
    default: number;
    note?: string;
  };
}

export interface KieCatalog {
  credit_usd: number;
  categories: { id: string; label: string }[];
  models: KieModelSpec[];
  configured: boolean;
}

export interface LibraryItemDTO {
  id: number;
  kind: string;
  key: string;
  title: string;
  file_path: string;
  active_version: number;
  content_hash: string;
  meta: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface LibraryItemDetailDTO extends LibraryItemDTO {
  content: string;
}

export interface LibraryVersionDTO {
  id: number;
  item_id: number;
  version: number;
  content_hash: string;
  message?: string | null;
  author?: string | null;
  source?: string | null;
  file_path: string;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface BlockActivityEntry {
  id: number;
  event_type: string;
  category?: string | null;
  block_id?: string | null;
  path?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface LibraryConfigDTO {
  id: number;
  name: string;
  project_id?: number | null;
  snapshot: Record<string, unknown>;
  content_hash: string;
  meta: Record<string, unknown>;
  created_at: string;
}

async function http<T>(
  path: string,
  options: RequestInit = {},
  timeoutMs = 30_000,
): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const external = options.signal;
  const onExtAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", onExtAbort);
  }
  try {
    // FormData: не ставить Content-Type вручную — браузер сам добавит
    // multipart boundary. Иначе FastAPI не видит UploadFile.
    const isFormData =
      typeof FormData !== "undefined" && options.body instanceof FormData;
    const { signal: _ignoredSignal, ...rest } = options;
    const res = await fetch(path, {
      ...rest,
      signal: controller.signal,
      headers: {
        ...(isFormData ? {} : { "Content-Type": "application/json" }),
        ...(options.headers || {}),
      },
    });
    if (!res.ok) {
      let detail: string | object = await res.text();
      try {
        detail = JSON.parse(detail as string);
      } catch {
        // оставляем как text
      }
      throw new ApiError(res.status, detail);
    }
    if (res.status === 204) return undefined as T;
    return res.json() as Promise<T>;
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      if (external?.aborted) {
        throw new ApiError(0, "Отменено");
      }
      const sec = Math.max(1, Math.round(timeoutMs / 1000));
      const isGptLong = timeoutMs >= 120_000;
      const looksOperator =
        typeof path === "string" &&
        (path.includes("/gpt-operator/") || path.includes("/excel-gpt/"));
      throw new ApiError(
        0,
        isGptLong
          ? `GPT не ответил за ${sec} с (vision/длинный ответ). Смотри окно BACKEND / лог; можно повторить.`
          : looksOperator
            ? `Пульт не сохранился за ${sec} с: БД занята воркером (hero/img/GPT). Подождите конец шага или повторите — бэкенд на :8765 обычно жив.`
            : `Сервер не ответил за ${sec} с — проверьте окно BACKEND (Uvicorn на :8765)`,
      );
    }
    throw e;
  } finally {
    window.clearTimeout(timer);
    external?.removeEventListener("abort", onExtAbort);
  }
}

export interface MontagePendingOp {
  type:
    | "image_regen"
    | "image_regen_prompt"
    | "image_regen_correction"
    | "image_ai_change"
    | "video_regen"
    | "video_regen_prompt"
    | "video_ai_change";
  frame_number: number;
  shot: 1 | 2;
  prompt?: string;
  correction?: string;
}

export interface XlsxPreview {
  path: string;
  sheets: string[];
  active_sheet: string;
  headers: string[];
  rows: string[][];
  row?: number;
  cells?: string[];
  start_row?: number;
  col_letters?: string[];
  truncated_rows?: boolean;
  truncated_cols?: boolean;
  sheet_max_row?: number;
  sheet_max_col?: number;
  node_key?: string | null;
  xlsx_snapshot?: string | null;
}

export interface ProjectAsset {
  source: string;
  id: string;
  kind: string;
  path: string | null;
  preview_url: string | null;
  label?: string;
  frame_id?: number | null;
  meta?: Record<string, unknown>;
  voiceover?: string;
  description?: string | null;
  uuid?: string;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string | object) {
    super(formatApiError(detail, status));
    this.name = "ApiError";
  }
}

export function formatApiError(
  detail: string | object,
  status?: number,
): string {
  if (status === 405) {
    return "API устарел (Method not allowed) — закройте Studio и запустите RUN-STUDIO.ps1 после git pull / UPDATE";
  }
  if (typeof detail === "string") {
    try {
      const parsed = JSON.parse(detail) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      return detail;
    }
    return detail;
  }
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    if (Array.isArray(d.errors) && d.errors.length > 0) {
      return d.errors.map(String).join("; ");
    }
    if (typeof d.error === "string" && d.error.trim()) return d.error;
    if (typeof d.message === "string" && d.message.trim()) return d.message;
    if ("detail" in d) {
      const inner = d.detail;
      if (typeof inner === "string") return inner;
      if (Array.isArray(inner)) return inner.map(String).join("; ");
      if (inner && typeof inner === "object") {
        const nested = inner as Record<string, unknown>;
        if (Array.isArray(nested.errors) && nested.errors.length > 0) {
          return nested.errors.map(String).join("; ");
        }
        if (typeof nested.error === "string") return nested.error;
      }
    }
  }
  return "Ошибка операции";
}

// ── База (DB v2) типы ─────────────────────────────────────────────
export interface DbProjectOverview {
  id: number;
  slug: string;
  title: string | null;
  topic: string | null;
  status: string | null;
  frames: number;
  scenes: number;
  entities: number;
  edges: number;
}

export interface DbOverview {
  projects: DbProjectOverview[];
}

export interface DbFrameText {
  id: number;
  kind: string;
  text: string;
  sort_key: number;
}

export interface DbPromptVersion {
  id: number;
  kind: string;
  version: number;
  is_active: boolean;
  text: string;
}

export interface DbEdge {
  id: number;
  to_frame_id: number;
  type: string;
}

export interface DbFrame {
  id: number;
  uuid: string | null;
  number: number;
  sort_key: number | null;
  scene_id: number | null;
  status: string | null;
  duration_seconds: number | null;
  voiceover_text: string;
  meaning: string | null;
  image_prompt?: string | null;
  animation_prompt?: string | null;
  /** Превью картинки кадра (scenes/frame_NNN_*.png) — для фильм-стрипа. */
  image_url?: string | null;
  attrs: Record<string, unknown>;
  texts: DbFrameText[];
  prompts: DbPromptVersion[];
  edges: DbEdge[];
}

export interface DbScene {
  id: number;
  sort_key: number;
  title: string | null;
  place: string | null;
  meaning: string | null;
  scene_type: string | null;
  frame_ids: number[];
}

export interface DbEntity {
  id: number;
  type: string;
  code: string | null;
  name: string | null;
  sort_key: number;
  attrs: Record<string, unknown>;
}

export interface DbExcelRow {
  column: number;
  r15_timecode: string | null;
  r45_image_prompt: string | null;
  r46_image_prompt_2: string | null;
  r48_video_prompt: string | null;
  r64_video_prompt_2: string | null;
  r49_voiceover: string | null;
  r50_duration: string | null;
  persons: string | null;
  items: string | null;
}

export interface DbHarnessSummary {
  outcome: string | null;
  updated_at: string | null;
  total: number;
  failed: string[];
  next_action: string | null;
}

export interface DbSceneRegistryEntry {
  id_scene?: string | null;
  start_words?: string | null;
  end_words?: string | null;
  structure?: string | null;
  edit_type?: string | null;
  transition?: string | null;
  [key: string]: unknown;
}

export interface DbGraph {
  project: { id: number; slug: string; title: string | null; topic: string | null; status: string | null };
  scenes: DbScene[];
  frames: DbFrame[];
  entities: DbEntity[];
  excel_rows?: Record<string, DbExcelRow>;
  /** Сцены по словам (meta.scene_registry) — SoT для scene grammar. */
  scene_registry?: DbSceneRegistryEntry[];
  harness?: DbHarnessSummary;
}

export const api = {
  // ── База (DB v2 browser) ─────────────────────────────────────────
  dbOverview: () => http<DbOverview>(`/api/db/overview`),
  dbGraph: (projectId: number) => http<DbGraph>(`/api/db/projects/${projectId}/graph`),
  shotMenu: (projectId: number) =>
    http<ShotMenuDTO>(`/api/db/projects/${projectId}/shot-menu`),
  shotMenuEditCell: (projectId: number, parentUuid: string, voiceover: string) =>
    http<{ ok: boolean }>(`/api/db/projects/${projectId}/shot-menu/cell`, {
      method: "PATCH",
      body: JSON.stringify({ parent_uuid: parentUuid, voiceover }),
    }),
  shotMenuAddCell: (projectId: number, beforeIndex: number | null, voiceover = "") =>
    http<{ ok: boolean; uuid: string; number: number }>(
      `/api/db/projects/${projectId}/shot-menu/cell`,
      {
        method: "POST",
        body: JSON.stringify({ before_index: beforeIndex, voiceover }),
      },
    ),
  shotMenuEditField: (
    projectId: number,
    frameUuid: string,
    field: string,
    value: string,
  ) =>
    http<{ ok: boolean }>(`/api/db/projects/${projectId}/shot-menu/shot-field`, {
      method: "PATCH",
      body: JSON.stringify({ frame_uuid: frameUuid, field, value }),
    }),
  dbPatchFrame: (frameId: number, body: Record<string, unknown>) =>
    http<{ ok: boolean }>(`/api/db/frames/${frameId}`, { method: "PATCH", body: JSON.stringify(body) }),
  dbInsertFrame: (projectId: number, afterFrameId: number | null, sceneId?: number | null) =>
    http<{ id: number; uuid: string; sort_key: number }>(`/api/db/projects/${projectId}/frames/insert`, {
      method: "POST",
      body: JSON.stringify({ after_frame_id: afterFrameId, scene_id: sceneId ?? null }),
    }),
  dbAddText: (frameId: number, kind: string, text: string) =>
    http<{ id: number }>(`/api/db/frames/${frameId}/texts`, { method: "POST", body: JSON.stringify({ kind, text }) }),
  dbDeleteText: (textId: number) =>
    http<{ ok: boolean }>(`/api/db/texts/${textId}`, { method: "DELETE" }),
  dbAddPrompt: (frameId: number, kind: string, text: string, setActive = true) =>
    http<{ id: number; version: number }>(`/api/db/frames/${frameId}/prompts`, {
      method: "POST",
      body: JSON.stringify({ kind, text, set_active: setActive }),
    }),
  dbActivatePrompt: (promptId: number) =>
    http<{ ok: boolean }>(`/api/db/prompts/${promptId}/activate`, { method: "POST" }),
  dbAddEntity: (projectId: number, body: { type: string; code?: string | null; name?: string | null; attrs?: Record<string, unknown> }) =>
    http<{ id: number }>(`/api/db/projects/${projectId}/entities`, { method: "POST", body: JSON.stringify(body) }),
  dbPatchEntity: (entityId: number, body: { type: string; code?: string | null; name?: string | null; attrs?: Record<string, unknown> }) =>
    http<{ ok: boolean }>(`/api/db/entities/${entityId}`, { method: "PATCH", body: JSON.stringify(body) }),
  dbDeleteEntity: (entityId: number) =>
    http<{ ok: boolean }>(`/api/db/entities/${entityId}`, { method: "DELETE" }),
  dbAddEdge: (frameId: number, toFrameId: number, type = "next") =>
    http<{ id: number }>(`/api/db/frames/${frameId}/edges`, { method: "POST", body: JSON.stringify({ to_frame_id: toFrameId, type }) }),
  dbDeleteEdge: (edgeId: number) =>
    http<{ ok: boolean }>(`/api/db/edges/${edgeId}`, { method: "DELETE" }),
  dbAddScene: (projectId: number, body: { title?: string; after_scene_id?: number | null }) =>
    http<{ id: number; sort_key: number }>(`/api/db/projects/${projectId}/scenes`, { method: "POST", body: JSON.stringify(body) }),
  dbExportXlsx: (projectId: number) =>
    http<{ frames: number; cells: number; backup: string | null; path: string }>(
      `/api/db/projects/${projectId}/export-xlsx`,
      { method: "POST" },
    ),
  dbPatchSheetCell: (
    projectId: number,
    body: { sheet: string; row: number; col: number; value: string },
  ) =>
    http<{
      ok: boolean;
      sheet: string;
      row: number;
      col: number;
      value: string;
      synced: string | null;
      backup: string | null;
    }>(`/api/db/projects/${projectId}/sheet-cell`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  dbApplyOps: (
    projectId: number,
    ops: { frame_uuid: string; fields: Record<string, string | number | null> }[],
    exportXlsx = false,
  ) =>
    http<{ ok: boolean; updated: number; exported: { frames: number; cells: number } | null }>(
      `/api/db/projects/${projectId}/apply-ops`,
      { method: "POST", body: JSON.stringify({ ops, export_xlsx: exportXlsx }) },
    ),
  dbOrchestratorChat: (
    projectId: number,
    message: string,
    history: { role: string; content: string }[],
    opts?: { signal?: AbortSignal; fixBugs?: boolean },
  ) =>
    http<{
      reply: string;
      applied: { updated: number; exported: { frames: number; cells: number } | null } | null;
      actions_run: {
        run_step?: string;
        status?: string;
        stop_step?: boolean;
        set_option?: string;
        set_prompt?: string;
        set_text_llm?: string;
        run_harness?: string;
        read_file?: string;
        edit_files?: string;
        run_tests?: string;
        git_commit_push?: string;
        delete_projects?: string;
        create_project?: string;
        create_child?: string;
        add_node?: string;
        connect_edges?: string;
        rename_node?: string;
        repair_graph?: string;
        hitl_decision?: string;
        set_topic?: string;
      }[];
      ui_actions: { kind: string; step?: string; node_type?: string; node_key?: string; hitl_id?: number; project_id?: number }[];
      pending_confirm: {
        kind: string;
        node_key?: string | null;
        node_type?: string | null;
        only?: string | null;
        count: number;
        nodes: string[];
        message?: string;
        files?: string[];
      }[];
      error: string | null;
    }>(
      `/api/db/projects/${projectId}/orchestrator/chat`,
      {
        method: "POST",
        body: JSON.stringify({
          message,
          history,
          fix_bugs: Boolean(opts?.fixBugs),
        }),
        signal: opts?.signal,
      },
      // GPT-вызов длинный (контекст графа + ответ): 10 минут, как ask_fresh.
      600_000,
    ),
  dbOrchestratorConfirmRemove: (projectId: number, body: { node_key?: string | null; node_type?: string | null; only?: string | null }) =>
    http<{ remove_node: string }>(`/api/db/projects/${projectId}/orchestrator/confirm-remove`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  dbOrchestratorConfirmDeleteProjects: (projectId: number, body: { ids: number[] }) =>
    http<{ delete_projects: string; ids: number[] }>(
      `/api/db/projects/${projectId}/orchestrator/confirm-delete-projects`,
      { method: "POST", body: JSON.stringify(body) },
      120_000,
    ),
  dbOrchestratorConfirmGitPush: (
    projectId: number,
    body: { message: string; files?: string[] },
  ) =>
    http<{ git_commit_push: string; sha: string; branch?: string; files: string[] }>(
      `/api/db/projects/${projectId}/orchestrator/confirm-git-push`,
      { method: "POST", body: JSON.stringify(body) },
      180_000,
    ),
  runHarnessVerify: (projectId: number) =>
    http<{ ok: boolean; checks: { name: string; ok: boolean; detail: string }[] }>(
      `/api/projects/${projectId}/harness/verify?include_http=false`,
      { method: "POST" },
      120_000,
    ),

  // ── Workflows ────────────────────────────────────────────────────
  listWorkflows: () => http<WorkflowSummary[]>(`/api/workflows`),
  getWorkflow: (id: number) => http<WorkflowDetail>(`/api/workflows/${id}`),
  saveWorkflow: (id: number, body: { name?: string; description?: string; nodes: WorkflowNode[]; edges: WorkflowEdge[] }) =>
    http<WorkflowDetail>(`/api/workflows/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  validateWorkflow: (body: { nodes: WorkflowNode[]; edges: WorkflowEdge[] }) =>
    http<{
      valid: boolean;
      errors: string[];
      warnings: string[];
      /** Только реально изменённые kind (Связь→Не ок). Не затирает остальные. */
      edge_patches?: { id: string; kind: string }[];
      edges?: WorkflowEdge[];
    }>(`/api/workflows/validate`, {
      method: "POST",
      body: JSON.stringify({ nodes: body.nodes, edges: body.edges }),
    }),
  createWorkflow: (body: { name?: string; description?: string; nodes: WorkflowNode[]; edges: WorkflowEdge[] }) =>
    http<WorkflowDetail>(`/api/workflows`, { method: "POST", body: JSON.stringify(body) }),
  duplicateWorkflow: (id: number) =>
    http<WorkflowDetail>(`/api/workflows/${id}/duplicate`, { method: "POST" }),
  deleteWorkflow: (id: number) =>
    http<void>(`/api/workflows/${id}`, { method: "DELETE" }),
  resetDefaultWorkflow: () =>
    http<WorkflowDetail>(`/api/workflows/default/reset`, { method: "POST" }),

  // ── Группы нод (пресеты канваса) ──────────────────────────────────
  listNodeGroups: () => http<NodeGroupSummary[]>(`/api/node-groups`),
  getNodeGroup: (groupId: string) =>
    http<NodeGroupDetail>(`/api/node-groups/${encodeURIComponent(groupId)}`),
  insertNodeGroup: (projectId: number, groupId: string, after?: string) =>
    http<{
      group: string;
      after: string;
      nodes: string[];
      edges_added: number;
      prompt_variants: Record<string, string>;
      project_meta: Record<string, unknown>;
    }>(`/api/projects/${projectId}/canvas/groups/${groupId}`, {
      method: "POST",
      body: JSON.stringify(after ? { after } : {}),
    }),
  createNodeGroupFromSelection: (
    projectId: number,
    body: {
      node_ids: string[];
      title: string;
      description?: string;
      category?: string;
    },
  ) =>
    http<NodeGroupSummary>(`/api/projects/${projectId}/node-groups/from-selection`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateNodeGroup: (
    groupId: string,
    patch: { title?: string; description?: string; category?: string },
  ) =>
    http<NodeGroupSummary>(`/api/node-groups/${encodeURIComponent(groupId)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  deleteNodeGroup: (groupId: string) =>
    http<{ deleted: string }>(`/api/node-groups/${encodeURIComponent(groupId)}`, {
      method: "DELETE",
    }),

  // ── Projects ─────────────────────────────────────────────────────
  listProjects: () => http<ProjectSummary[]>(`/api/projects`),
  getProject: (id: number) => http<ProjectDetail>(`/api/projects/${id}`),
  createProject: (body: {
    title: string;
    topic?: string;
    hero_mode?: string;
    auto_mode?: boolean;
    sidebar_folder_id?: string | null;
  }) =>
    http<ProjectDetail>(`/api/projects`, { method: "POST", body: JSON.stringify(body) }),
  patchProject: (id: number, body: Partial<ProjectDetail>) =>
    http<ProjectDetail>(
      `/api/projects/${id}`,
      { method: "PATCH", body: JSON.stringify(body) },
      90_000,
    ),
  deleteProject: (id: number) =>
    http<void>(`/api/projects/${id}`, { method: "DELETE" }),
  createChildProject: (parentId: number) =>
    http<ProjectDetail>(
      `/api/projects/${parentId}/child`,
      { method: "POST" },
      120_000,
    ),

  // ── Sidebar layout ───────────────────────────────────────────────
  getSidebarLayout: () => http<SidebarLayout>(`/api/sidebar-layout`),
  getRuntimeStreams: () =>
    http<{
      worker_max_parallel: number;
      default_outsee_streams: number;
      default_check_streams: number;
      worker_busy: number;
      create_max_parallel_outsee: number;
      limits: Record<string, [number, number]>;
    }>(`/api/runtime-streams`),
  patchRuntimeStreams: (body: {
    worker_max_parallel?: number;
    default_outsee_streams?: number;
    default_check_streams?: number;
  }) =>
    http<{
      worker_max_parallel: number;
      default_outsee_streams: number;
      default_check_streams: number;
      worker_busy: number;
      create_max_parallel_outsee: number;
      limits: Record<string, [number, number]>;
    }>(`/api/runtime-streams`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  updateSidebarLayout: (body: Partial<SidebarLayout>) =>
    http<SidebarLayout>(`/api/sidebar-layout`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  createSidebarFolder: (name: string) =>
    http<{ id: string; name: string; order: number }>(`/api/sidebar-layout/folders`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  renameSidebarFolder: (folderId: string, name: string) =>
    http<{ id: string; name: string }>(`/api/sidebar-layout/folders/${folderId}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  deleteSidebarFolder: (folderId: string) =>
    http<{ ok: boolean }>(`/api/sidebar-layout/folders/${folderId}`, { method: "DELETE" }),
  toggleGenQueue: (projectId: number) =>
    http<{ gen_queue: number[]; gen_queue_positions: Record<number, number>; position: number | null }>(
      `/api/sidebar-layout/gen-queue/toggle`,
      { method: "POST", body: JSON.stringify({ project_id: projectId }) },
    ),
  enqueueGenQueue: (body: {
    project_id: number;
    mode: "full" | "until_node";
    target_node_key?: string;
    target_node_type?: string;
  }) =>
    http<{
      gen_queue: number[];
      gen_queue_positions: Record<number, number>;
      position: number | null;
      gen_queue_run?: Record<string, unknown> | null;
    }>(`/api/sidebar-layout/gen-queue/enqueue`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listStepCatalog: () =>
    http<{ code: string; label: string; running_status: string; ready_status: string }[]>(
      `/api/projects/steps/catalog`
    ),
  runProjectStep: (
    projectId: number,
    stepCode: string,
    opts?: { dryRun?: boolean; nodeKey?: string; mode?: "full" | "resume"; forceWipe?: boolean },
  ) => {
    const params = new URLSearchParams();
    if (opts?.dryRun) params.set("dry_run", "true");
    if (opts?.nodeKey) params.set("node_key", opts.nodeKey);
    if (opts?.mode) params.set("mode", opts.mode);
    if (opts?.forceWipe != null) params.set("force_wipe", String(opts.forceWipe));
    const q = params.toString() ? `?${params.toString()}` : "";
    return http<ProjectDetail>(`/api/projects/${projectId}/steps/${stepCode}/run${q}`, {
      method: "POST",
    });
  },
  patchExcelGptConfig: (
    projectId: number,
    nodeKey: string,
    patch: {
      label?: string;
      inputSource?: string;
      uploadedFileName?: string;
      uploadedFileNames?: string[];
      slotIndex?: number;
      workMode?: string;
      role?: string;
      outputMode?: string;
      useSnapshot?: boolean;
      transport?: string;
    },
  ) =>
    http<{ ok: boolean; config: Record<string, unknown>; resolve?: Record<string, unknown> }>(
      `/api/projects/${projectId}/excel-gpt/${encodeURIComponent(nodeKey)}`,
      { method: "PATCH", body: JSON.stringify(patch) },
    ),
  uploadExcelGptFile: (projectId: number, nodeKey: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return http<{
      ok: boolean;
      fileName: string;
      path: string;
      isImage?: boolean;
      preview_url?: string | null;
      uploadedFileNames?: string[];
      usedAsCheckAgent?: boolean;
      replacedXlsx?: boolean;
      takeFromEdges?: boolean;
      inputSource?: string;
      chars?: number;
      resolve?: import("@/lib/gpt-operator").OperatorResolve;
    }>(
      `/api/projects/${projectId}/excel-gpt/${encodeURIComponent(nodeKey)}/upload`,
      { method: "POST", body: fd },
    );
  },
  remapExcelGptNodes: (projectId: number, mapping: Record<string, string>) =>
    http<{ ok: boolean; remapped: string[] }>(
      `/api/projects/${projectId}/excel-gpt/remap-keys`,
      { method: "POST", body: JSON.stringify({ mapping }) },
    ),
  resolveGptOperator: (projectId: number, nodeKey: string) =>
    http<import("@/lib/gpt-operator").OperatorResolve>(
      `/api/projects/${projectId}/gpt-operator/${encodeURIComponent(nodeKey)}/resolve`,
      {},
      90_000,
    ),
  patchGptOperator: (
    projectId: number,
    nodeKey: string,
    patch: Record<string, unknown>,
  ) =>
    http<{ ok: boolean; resolve: import("@/lib/gpt-operator").OperatorResolve }>(
      `/api/projects/${projectId}/gpt-operator/${encodeURIComponent(nodeKey)}`,
      { method: "PATCH", body: JSON.stringify(patch) },
      90_000,
    ),
  uploadCheckAgentFile: (projectId: number, nodeKey: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return http<{
      ok: boolean;
      fileName: string;
      path: string;
      chars: number;
      resolve: import("@/lib/gpt-operator").OperatorResolve;
    }>(
      `/api/projects/${projectId}/gpt-operator/${encodeURIComponent(nodeKey)}/check-agent`,
      { method: "POST", body: fd },
    );
  },
  clearCheckAgentFile: (projectId: number, nodeKey: string) =>
    http<{ ok: boolean; resolve: import("@/lib/gpt-operator").OperatorResolve }>(
      `/api/projects/${projectId}/gpt-operator/${encodeURIComponent(nodeKey)}/check-agent`,
      { method: "DELETE" },
    ),
  getCheckAgentFile: (projectId: number, nodeKey: string) =>
    http<{
      fileName: string;
      chars: number;
      text: string;
      source?: "upload" | "builtin";
      step?: string;
    }>(
      `/api/projects/${projectId}/gpt-operator/${encodeURIComponent(nodeKey)}/check-agent`,
      {},
      90_000,
    ),
  getGptOperatorSourcePrompt: (
    projectId: number,
    nodeKey: string,
    sourceKey: string,
  ) =>
    http<{ nodeKey: string; variant: string | null; chars: number; text: string }>(
      `/api/projects/${projectId}/gpt-operator/${encodeURIComponent(nodeKey)}/source-prompt?source=${encodeURIComponent(sourceKey)}`,
      {},
      90_000,
    ),
  getCheckPromptPreview: (projectId: number, nodeKey: string) =>
    http<{
      text: string;
      chars: number;
      mode: string;
      checkPromptSource: string;
      source: string;
      dbSot: boolean;
    }>(
      `/api/projects/${projectId}/gpt-operator/${encodeURIComponent(nodeKey)}/check-prompt-preview`,
      {},
      90_000,
    ),
  patchCanvasEdgeKind: (
    projectId: number,
    edgeId: string,
    kind: string,
  ) =>
    http<{ ok: boolean; edge: Record<string, unknown> }>(
      `/api/projects/${projectId}/canvas-edges/${encodeURIComponent(edgeId)}`,
      { method: "PATCH", body: JSON.stringify({ kind }) },
    ),

  previewBugReport: (minutes: number) =>
    http<{
      minutes: number;
      files: { name: string; chars: number; preview: string }[];
    }>(`/api/bug-reports/preview?minutes=${minutes}`),
  createBugReport: (body: {
    description: string;
    minutes: number;
    projectId?: number;
    projectSlug?: string;
    studioVersion?: string;
  }) =>
    http<{
      ok: boolean;
      path: string;
      rel: string;
      filename: string;
      minutes: number;
      logFiles: string[];
      logChars?: number;
      content?: string;
      clipboardPrompt: string;
    }>(`/api/bug-reports`, { method: "POST", body: JSON.stringify(body) }),

  // ── Excel-Hero (читает лист «Персонажи» из project.xlsx) ─────────
  getExcelHero: (projectId: number) =>
    http<{ loaded: boolean; characters: ExcelHeroCharacter[] }>(
      `/api/projects/${projectId}/excel-hero`
    ),
  loadExcelHero: (projectId: number) =>
    http<{ loaded: boolean; count: number; characters: ExcelHeroCharacter[] }>(
      `/api/projects/${projectId}/excel-hero/load`,
      { method: "POST" }
    ),
  clearExcelHero: (projectId: number) =>
    http<void>(`/api/projects/${projectId}/excel-hero`, { method: "DELETE" }),

  // ── Storage node (хранилище файлов по node_key) ─────────────────
  resolveStorage: (projectId: number, nodeKey: string) =>
    http<{
      nodeKey: string;
      label: string;
      formats: string[];
      autoSync?: boolean;
      files: {
        name: string;
        path: string;
        size: number;
        kind: string;
        ok: boolean;
        fromNode?: string | null;
        fromLabel?: string | null;
        savedAt?: string | null;
        originalName?: string | null;
        preview_url?: string | null;
        download_url?: string | null;
      }[];
      okFileCount: number;
      incomingSources: string[];
      storageDir: string;
      lastSyncAt?: string;
    }>(`/api/projects/${projectId}/storage/${encodeURIComponent(nodeKey)}/resolve`),
  storageDownloadZipUrl: (projectId: number, nodeKey: string) =>
    `/api/projects/${projectId}/storage/${encodeURIComponent(nodeKey)}/download.zip`,
  patchStorage: (
    projectId: number,
    nodeKey: string,
    patch: { formats?: string[]; label?: string },
  ) =>
    http<{ ok: boolean; config: Record<string, unknown>; resolve: Record<string, unknown> }>(
      `/api/projects/${projectId}/storage/${encodeURIComponent(nodeKey)}`,
      { method: "PATCH", body: JSON.stringify(patch) },
    ),
  syncStorage: (projectId: number, nodeKey: string) =>
    http<{
      ok: boolean;
      copied: string[];
      skipped: string[];
      errors: string[];
      files: { name: string; path: string }[];
      okFileCount: number;
    }>(`/api/projects/${projectId}/storage/${encodeURIComponent(nodeKey)}/sync`, {
      method: "POST",
    }),
  uploadStorageFile: (projectId: number, nodeKey: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return http<{ ok: boolean; fileName: string; path: string }>(
      `/api/projects/${projectId}/storage/${encodeURIComponent(nodeKey)}/upload`,
      { method: "POST", body: fd },
    );
  },
  clearStorageFiles: (projectId: number, nodeKey: string) =>
    http<{ ok: boolean; removed: number }>(
      `/api/projects/${projectId}/storage/${encodeURIComponent(nodeKey)}/files`,
      { method: "DELETE" },
    ),

  // ── Frames ───────────────────────────────────────────────────────
  listFrames: (projectId: number) =>
    http<FrameDTO[]>(`/api/projects/${projectId}/frames`),
  patchFrame: (projectId: number, frameId: number, body: Partial<FrameDTO>) =>
    http<FrameDTO>(`/api/projects/${projectId}/frames/${frameId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  getMontageBoard: (projectId: number) =>
    http<MontageBoardDTO>(
      `/api/projects/${projectId}/montage-board`,
      {},
      120_000,
    ),

  saveMontageQueue: (
    projectId: number,
    body: {
      pending_ops: MontagePendingOp[];
      video_trims?: Record<string, { start: number; end: number }>;
    },
  ) =>
    http<{
      ok: boolean;
      pending_ops: MontagePendingOp[];
      meta?: MontageBoardMeta;
    }>(`/api/projects/${projectId}/montage-board/queue`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  applyMontageBoard: (
    projectId: number,
    body: {
      video_trims: Record<string, { start: number; end: number }>;
      pending_ops: MontagePendingOp[];
    },
  ) =>
    http<{
      ok: boolean;
      started?: boolean;
      already_running?: boolean;
      message?: string;
      meta?: MontageBoardMeta;
      errors?: string[];
      job?: { status?: string; total_ops?: number; error?: string | null };
    }>(`/api/projects/${projectId}/montage-board/apply`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getMontageApplyStatus: (projectId: number) =>
    http<{
      job: {
        status?: string;
        error?: string | null;
        total_ops?: number;
        done_ops?: number;
        last_path?: string;
        last_frame_number?: number;
        last_shot?: number;
        last_highlight?: string;
        results?: Array<{
          ok?: boolean;
          error?: string;
          highlight?: string;
          op?: {
            type?: string;
            frame_number?: number;
            shot?: number;
          };
        }>;
      };
    }>(`/api/projects/${projectId}/montage-board/apply-status`),

  runMontageBoard: (projectId: number) =>
    http<{ started: boolean; already_running?: boolean; job?: Record<string, unknown> }>(
      `/api/projects/${projectId}/montage-board/montage`,
      { method: "POST" },
    ),

  recoverMontageFromOutsee: (projectId: number) =>
    http<{
      started?: boolean;
      already_running?: boolean;
      ok: boolean;
      message?: string;
      saved?: Array<{ frame_number: number; shot: number; path?: string }>;
      saved_count?: number;
      errors?: string[];
      hits_scanned?: number;
      meta?: MontageBoardMeta;
      job?: {
        status?: string;
        error?: string | null;
        saved_count?: number;
        hits_scanned?: number;
      };
    }>(`/api/projects/${projectId}/montage-board/recover-outsee`, {
      method: "POST",
    }),

  getMontageRecoverOutseeStatus: (projectId: number) =>
    http<{
      job: {
        status?: string;
        error?: string | null;
        saved_count?: number;
        hits_scanned?: number;
        saved?: Array<{ frame_number: number; shot: number }>;
      };
    }>(`/api/projects/${projectId}/montage-board/recover-outsee-status`),

  getMontageBoardStatus: (projectId: number) =>
    http<{ job: { status?: string; error?: string | null } }>(
      `/api/projects/${projectId}/montage-board/montage-status`,
    ),

  listAudioAlignMethods: () =>
    http<{ methods: Array<{ id: string; title: string; summary: string }> }>(
      `/api/projects/audio-align/methods`,
    ),

  runAudioAlign: (
    projectId: number,
    opts: { method: string; force_asr?: boolean; run_assemble?: boolean },
  ) => {
    const q = new URLSearchParams({
      method: opts.method,
      force_asr: String(Boolean(opts.force_asr)),
      run_assemble: String(opts.run_assemble !== false),
    });
    return http<{
      started: boolean;
      already_running?: boolean;
      job?: Record<string, unknown>;
    }>(`/api/projects/${projectId}/audio-align?${q}`, { method: "POST" });
  },

  getAudioAlignStatus: (projectId: number) =>
    http<{
      job: {
        status?: string;
        error?: string | null;
        method?: string;
        result?: {
          crumbs?: number;
          words_source?: string;
          r15_written?: number;
          final_video?: string | null;
          db_frames_error?: string;
          engine?: string;
        };
      };
      last?: Record<string, unknown> | null;
    }>(`/api/projects/${projectId}/audio-align/status`),

  swapMontageShots: (
    projectId: number,
    frameNumber: number,
    kind: "image" | "video" | "both" = "both",
  ) =>
    http<{
      ok: boolean;
      frame_number: number;
      kind: string;
      images_swapped?: boolean;
      videos_swapped?: boolean;
      prompts_swapped?: boolean;
    }>(
      `/api/projects/${projectId}/montage-board/swap-shots?frame_number=${frameNumber}&kind=${kind}`,
      { method: "POST" },
    ),

  /** Обмен двух слотов (картинка↔картинка или видео↔видео) из любых кадров. */
  swapMontageSlots: (
    projectId: number,
    kind: "image" | "video",
    a: { frameNumber: number; shot: 1 | 2 },
    b: { frameNumber: number; shot: 1 | 2 },
  ) =>
    http<{
      ok: boolean;
      mode: "move" | "swap";
      kind: string;
      from_frame: number;
      from_shot: number;
      to_frame: number;
      to_shot: number;
    }>(
      `/api/projects/${projectId}/montage-board/swap-slots` +
        `?kind=${kind}` +
        `&a_frame=${a.frameNumber}&a_shot=${a.shot}` +
        `&b_frame=${b.frameNumber}&b_shot=${b.shot}`,
      { method: "POST" },
    ),

  moveMontageImage: (
    projectId: number,
    fromFrame: number,
    fromShot: 1 | 2,
    toFrame: number,
    toShot: 1 | 2,
  ) =>
    http<{
      ok: boolean;
      mode: "move" | "swap";
      from_frame: number;
      from_shot: number;
      to_frame: number;
      to_shot: number;
    }>(
      `/api/projects/${projectId}/montage-board/move-image` +
        `?from_frame=${fromFrame}&from_shot=${fromShot}` +
        `&to_frame=${toFrame}&to_shot=${toShot}`,
      { method: "POST" },
    ),

  deleteMontageImage: (projectId: number, frameNumber: number, shot: 1 | 2) =>
    http<{ ok: boolean }>(
      `/api/projects/${projectId}/montage-board/delete-image?frame_number=${frameNumber}&shot=${shot}`,
      { method: "POST" },
    ),

  deleteMontageVideo: (projectId: number, frameNumber: number, shot: 1 | 2) =>
    http<{ ok: boolean }>(
      `/api/projects/${projectId}/montage-board/delete-video?frame_number=${frameNumber}&shot=${shot}`,
      { method: "POST" },
    ),

  uploadMontageImage: async (projectId: number, frameNumber: number, shot: 1 | 2, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(
      `/api/projects/${projectId}/montage-board/upload-image?frame_number=${frameNumber}&shot=${shot}`,
      { method: "POST", body: fd },
    );
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<{ ok: boolean; preview_url: string }>;
  },

  uploadMontageVideo: async (projectId: number, frameNumber: number, shot: 1 | 2, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(
      `/api/projects/${projectId}/montage-board/upload-video?frame_number=${frameNumber}&shot=${shot}`,
      { method: "POST", body: fd },
    );
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<{ ok: boolean; preview_url: string }>;
  },

  uploadMontageVoice: async (projectId: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/projects/${projectId}/montage-board/upload-voice`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<{ ok: boolean; path: string }>;
  },

  uploadMontageMusic: async (projectId: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/projects/${projectId}/montage-board/upload-music`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<{ ok: boolean; path: string }>;
  },

  // ── Runs ─────────────────────────────────────────────────────────
  listRuns: () => http<WorkflowRunDetail[]>(`/api/runs`),
  getRun: (id: number) => http<WorkflowRunDetail>(`/api/runs/${id}`),
  startRunFromWorkflow: (workflowId: number, body: { project_id?: number; topic?: string; hero_mode?: string }) =>
    http<WorkflowRunDetail>(`/api/runs/from-workflow/${workflowId}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  cancelRun: (id: number) =>
    http<WorkflowRunDetail>(`/api/runs/${id}/cancel`, { method: "POST" }),

  // ── HITL ─────────────────────────────────────────────────────────
  listPendingHitl: () => http<HITLDTO[]>(`/api/hitl/pending`),
  listProjectHitl: (projectId: number) =>
    http<HITLDTO[]>(`/api/hitl/project/${projectId}`),
  submitHitlDecision: (
    hitlId: number,
    body: { decision: string; edited_prompt?: string }
  ) =>
    http<HITLDTO>(`/api/hitl/${hitlId}/decision`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── Prompts ──────────────────────────────────────────────────────
  listPrompts: () => http<PromptDTO[]>(`/api/prompts`),
  patchPrompt: (id: number, body: { text?: string; active?: boolean }) =>
    http<PromptDTO>(`/api/prompts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  // ── Artifacts ────────────────────────────────────────────────────
  listArtifacts: (params: { project_id?: number; frame_id?: number; kind?: string }) => {
    const q = new URLSearchParams();
    if (params.project_id != null) q.set("project_id", String(params.project_id));
    if (params.frame_id != null) q.set("frame_id", String(params.frame_id));
    if (params.kind) q.set("kind", params.kind);
    return http<ArtifactDTO[]>(`/api/artifacts?${q.toString()}`);
  },
  artifactFileUrl: (uuid: string) => `/api/artifacts/${uuid}/file`,

  // ── Prompt studio (blocks v2) ────────────────────────────────────
  promptStudioCatalog: () =>
    http<{
      block_categories: Record<string, string[]>;
      blocks?: {
        category: string;
        id: string;
        label: string;
        preview: string;
        body: string;
      }[];
      steps: string[];
      step_block_categories: Record<string, string[]>;
      node_type_to_step: Record<string, string>;
      style_presets: { id: string; label: string; description?: string }[];
    }>(`/api/prompt-studio/catalog`),
  promptStudioStepMeta: (stepId: string) =>
    http<{ step_id: string; block_categories: string[]; vars: string[] }>(
      `/api/prompt-studio/steps/${stepId}/meta`,
    ),
  promptStudioStepPresets: (stepCode: string) =>
    http<Record<string, unknown>>(
      `/api/prompt-studio/step-presets/${encodeURIComponent(stepCode)}`,
    ),
  patchStepPreset: (
    stepCode: string,
    presetId: string,
    body: { label?: string; description?: string; blocks?: Record<string, string | null> },
  ) =>
    http<Record<string, unknown>>(
      `/api/prompt-studio/step-presets/${encodeURIComponent(stepCode)}/presets/${encodeURIComponent(presetId)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  createStepPreset: (
    stepCode: string,
    presetId: string,
    body: { label?: string; description?: string; blocks?: Record<string, string | null> },
  ) =>
    http<Record<string, unknown>>(
      `/api/prompt-studio/step-presets/${encodeURIComponent(stepCode)}/presets/${encodeURIComponent(presetId)}`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  deleteStepPreset: (stepCode: string, presetId: string) =>
    http<{ step_code: string; id: string; deleted: boolean }>(
      `/api/prompt-studio/step-presets/${encodeURIComponent(stepCode)}/presets/${encodeURIComponent(presetId)}`,
      { method: "DELETE" },
    ),
  composePrompt: (body: {
    node_type?: string;
    step_id?: string;
    project_id?: number;
    blocks?: Record<string, BlockSelection>;
    vars?: Record<string, string | number>;
    style_preset?: string;
  }) =>
    http<{ text: string; blocks: Record<string, BlockSelection>; vars: Record<string, string> }>(
      `/api/prompt-studio/compose`,
      { method: "POST", body: JSON.stringify(body) }
    ),
  patchProjectPromptConfig: (
    projectId: number,
    body: {
      style_profile?: string;
      blocks?: Record<string, BlockSelection>;
      vars?: Record<string, string | number>;
      use_blocks_v2?: boolean;
      legacy?: Record<string, string>;
    }
  ) =>
    http<{ prompt_overrides: Record<string, unknown> }>(
      `/api/prompt-studio/projects/${projectId}/prompt-config`,
      { method: "PATCH", body: JSON.stringify(body) }
    ),
  // Блочный редактор шаблона шага (steps/<id>/template.md, карточки 1..N).
  getStepTemplate: (stepId: string) =>
    http<{ step_id: string; blocks: StepTemplateBlock[] }>(
      `/api/prompt-studio/step-template/${stepId}`
    ),
  saveStepTemplate: (stepId: string, blocks: StepTemplateBlock[]) =>
    http<{ step_id: string; blocks: StepTemplateBlock[] }>(
      `/api/prompt-studio/step-template/${stepId}`,
      { method: "PUT", body: JSON.stringify({ blocks }) }
    ),
  getProjectGptText: (projectId: number, stepCode: string) =>
    http<{
      step_code: string;
      text: string;
      supported: boolean;
      is_override: boolean;
      human_name?: string;
    }>(`/api/prompt-studio/projects/${projectId}/gpt-text/${stepCode}`),
  saveProjectGptText: (projectId: number, stepCode: string, text: string) =>
    http<{ step_code: string; text: string; supported: boolean; is_override: boolean }>(
      `/api/prompt-studio/projects/${projectId}/gpt-text/${stepCode}`,
      { method: "PUT", body: JSON.stringify({ text }) }
    ),
  resetProjectGptText: (projectId: number, stepCode: string) =>
    http<{ step_code: string; text: string; supported: boolean; is_override: boolean }>(
      `/api/prompt-studio/projects/${projectId}/gpt-text/${stepCode}`,
      { method: "DELETE" }
    ),
  promptStudioSyncBlocks: () =>
    http<{
      categories: number;
      blocks_total: number;
      discovered: { category: string; block_id: string }[];
      discovered_count: number;
    }>(`/api/prompt-studio/blocks/sync`, { method: "POST" }),
  promptStudioBlockActivity: (params?: { limit?: number; category?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.category) q.set("category", params.category);
    const qs = q.toString();
    return http<BlockActivityEntry[]>(
      `/api/prompt-studio/block-activity${qs ? `?${qs}` : ""}`,
    );
  },
  promptStudioLogBlockActivity: (body: {
    event_type: "block_selected" | "block_viewed";
    category: string;
    block_id: string;
    project_id?: number;
    step_id?: string;
    step_code?: string;
    prompt_variant?: string | null;
  }) =>
    http<{ ok: boolean }>(`/api/prompt-studio/block-activity`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPromptBlock: (category: string, blockId: string) =>
    http<{ category: string; id: string; body: string }>(
      `/api/prompt-studio/blocks/${encodeURIComponent(category)}/${encodeURIComponent(blockId)}`,
    ),
  savePromptBlock: (
    category: string,
    blockId: string,
    body: { content: string; message?: string },
  ) =>
    http<{ category: string; id: string; label: string; version: number; library_item_id: number }>(
      `/api/prompt-studio/blocks/${encodeURIComponent(category)}/${encodeURIComponent(blockId)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  createPromptBlock: (
    category: string,
    body: { block_id: string; content?: string; message?: string },
  ) =>
    http<{ category: string; id: string; label: string; version: number; library_item_id: number }>(
      `/api/prompt-studio/blocks/${encodeURIComponent(category)}`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  deletePromptBlock: (category: string, blockId: string) =>
    http<{ category: string; id: string; deleted: boolean }>(
      `/api/prompt-studio/blocks/${encodeURIComponent(category)}/${encodeURIComponent(blockId)}`,
      { method: "DELETE" },
    ),
  renamePromptBlock: (
    category: string,
    blockId: string,
    body: { new_block_id: string; message?: string },
  ) =>
    http<{ category: string; id: string; renamed_from?: string; label: string }>(
      `/api/prompt-studio/blocks/${encodeURIComponent(category)}/${encodeURIComponent(blockId)}/rename`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  saveGptTextAsTemplate: (
    projectId: number,
    stepCode: string,
    body: { name: string; text?: string },
  ) =>
    http<{
      step_code: string;
      name: string;
      filename: string;
      path: string;
      size: number;
    }>(`/api/prompt-studio/projects/${projectId}/gpt-text/${stepCode}/save-template`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getGptVerdictContext: (projectId: number, stepCode: string, template = "default") =>
    http<{
      step_code: string;
      supported: boolean;
      template: string;
      templates: string[];
      prompt: string;
      artifact_preview: string;
      attachments: string[];
    }>(
      `/api/prompt-studio/projects/${projectId}/gpt-verdict/${stepCode}?template=${encodeURIComponent(template)}`,
    ),
  listGptVerdictTemplates: (stepCode: string) =>
    http<{ step_code: string; templates: string[] }>(
      `/api/prompt-studio/verdict-templates/${stepCode}`,
    ),
  saveGptVerdictTemplate: (
    projectId: number,
    stepCode: string,
    body: { name: string; content: string },
  ) =>
    http<{ ok: boolean; name: string; path: string }>(
      `/api/prompt-studio/projects/${projectId}/gpt-verdict/${stepCode}/save-template`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
  deleteGptVerdictTemplate: (projectId: number, stepCode: string, name: string) =>
    http<{ ok: boolean; name: string; removed: boolean }>(
      `/api/prompt-studio/projects/${projectId}/gpt-verdict/${stepCode}/templates/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  getStepAttachments: (projectId: number, stepCode: string, nodeKey?: string) => {
    const q = nodeKey ? `?node_key=${encodeURIComponent(nodeKey)}` : "";
    return http<{ step_code: string; node_key?: string; files: string[] }>(
      `/api/prompt-studio/projects/${projectId}/step-attachments/${stepCode}${q}`,
    );
  },
  runGptVerdict: (projectId: number, stepCode: string, prompt: string) =>
    http<{
      approved: boolean;
      fix_applied: boolean;
      fix_path: string;
      advanced: boolean;
      status: string;
      rounds: number;
      fix_text: string;
      last_raw: string;
      history: string[];
    }>(`/api/prompt-studio/projects/${projectId}/gpt-verdict/${stepCode}/run`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  pauseProject: (projectId: number) =>
    http<ProjectDetail>(`/api/projects/${projectId}/pause`, { method: "POST" }),
  resumeProject: (projectId: number) =>
    http<ProjectDetail>(`/api/projects/${projectId}/resume`, { method: "POST" }),
  continueProject: (projectId: number) =>
    http<{
      project: ProjectDetail;
      action: string;
      status: string;
      advanced: boolean;
      cleared?: string[];
    }>(`/api/projects/${projectId}/continue`, { method: "POST" }),
  stopProject: (projectId: number) =>
    http<{
      project: ProjectDetail;
      message: string;
      generation_still_active: boolean;
      xlsx_stopped: string[];
    }>(`/api/projects/${projectId}/stop`, {
      method: "POST",
    }),
  finishMissingImages: (projectId: number) =>
    http<{
      ok: boolean;
      kind: string;
      missing: number[];
      queued: number;
      already_running: boolean;
      message: string;
      project: ProjectDetail;
    }>(`/api/projects/${projectId}/finish/images`, { method: "POST" }),
  finishMissingVideos: (projectId: number) =>
    http<{
      ok: boolean;
      kind: string;
      missing: number[];
      queued: number;
      already_running: boolean;
      message: string;
      project: ProjectDetail;
    }>(`/api/projects/${projectId}/finish/videos`, { method: "POST" }),
  finishMissingAnimationPrompts: (projectId: number) =>
    http<{
      ok: boolean;
      kind: string;
      missing: number[];
      queued: number;
      already_done?: number;
      synced_from_xlsx?: number;
      already_running: boolean;
      message: string;
      project: ProjectDetail;
    }>(`/api/projects/${projectId}/finish/animation-prompts`, { method: "POST" }),
  startMassLanes: (
    projectId: number,
    body: { count?: number; topics?: string[] },
  ) =>
    http<{
      created: { id: number; topic: string; slug?: string }[];
      count: number;
      queue_size?: number;
      remaining?: number;
      started_id?: number | null;
    }>(`/api/projects/${projectId}/mass-lanes/start`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getMassFactoryStatus: (projectId: number) =>
    http<{
      active: boolean;
      topics: string[];
      cursor: number;
      revision: number;
      filename: string;
      factory: boolean;
      busy_child_id: number | null;
      children: { id: number; topic: string; slug: string; status: string; lane_position?: number }[];
      queued_after_current?: boolean;
    }>(`/api/projects/${projectId}/mass-factory/status`),
  parseMassTopicsXlsx: async (projectId: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/projects/${projectId}/mass-lanes/parse-topics`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<{
      topics: string[];
      count: number;
      revision?: number;
      queued_after_current?: boolean;
    }>;
  },
  getOutseeCreateSettings: () =>
    http<Record<string, unknown>>(`/api/outsee-create/settings`),
  putOutseeCreateSettings: (body: Record<string, unknown>) =>
    http<Record<string, unknown>>(`/api/outsee-create/settings`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  listOutseeCreateHistory: (
    kind: "all" | "image" | "video" | "audio" = "all",
    opts?: { limit?: number; scope?: "create" | "all" },
  ) =>
    http<
      {
        id: string;
        kind: string;
        artifact_kind?: string;
        preview_url: string | null;
        path: string | null;
        label: string;
        project_id: number | null;
        project_slug: string | null;
        frame_id: number | null;
        prompt: string | null;
        status?: string | null;
        job_id?: string | null;
        error?: string | null;
        model?: string | null;
        elapsed_sec?: number | null;
        elapsed_label?: string | null;
      }[]
    >(
      `/api/outsee-create/history?kind=${kind}&scope=${opts?.scope ?? "create"}&limit=${opts?.limit ?? 60}`,
    ),
  deleteOutseeCreateHistoryItem: (params: { path?: string; itemId?: string }) => {
    const q = new URLSearchParams();
    if (params.path) q.set("path", params.path);
    if (params.itemId) q.set("item_id", params.itemId);
    return http<{ ok: boolean; deleted: boolean }>(
      `/api/outsee-create/history?${q.toString()}`,
      { method: "DELETE" },
    );
  },
  enhanceOutseeCreatePrompt: (data: { prompt: string; style?: string }) =>
    http<{ ok: boolean; enhanced_prompt: string }>(`/api/outsee-create/enhance-prompt`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  outseeStatus: () =>
    http<{
      configured: boolean;
      enabled_image: boolean;
      enabled_video: boolean;
      http_api: boolean;
      fallback_cdp: boolean;
      image_provider: string;
      video_provider: string;
      base_url?: string;
      key_suffix?: string | null;
      balance?: Record<string, unknown> | null;
      queue?: number;
      hint: string;
    }>(`/api/outsee/status`),

  outseeGenerate: (body: {
    prompt: string;
    media?: "image" | "video" | "audio";
    model?: string;
    aspect?: string;
    resolution?: string;
    detail_level?: string | null;
    duration?: number;
    title?: string;
    relax?: boolean;
    generate_audio?: boolean | null;
    project_id?: number | null;
    first_frame_url?: string | null;
    last_frame_url?: string | null;
    reference_images?: string[] | null;
    nonce?: string | null;
    batch_index?: number | null;
  }) =>
    http<{
      ok: boolean;
      job_id: string;
      status: string;
      media: string;
      model: string | null;
      path: string;
      history_id: string;
      preview_url?: string | null;
      raw_url?: string | null;
      gen_id?: string | null;
      provider?: string;
      queue?: number;
      error?: string | null;
    }>(
      `/api/outsee/generate`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
      60_000,
    ),

  outseeJob: (jobId: string) =>
    http<{
      job_id: string;
      status: string;
      media: string;
      model: string;
      path: string;
      history_id: string;
      preview_url?: string | null;
      error?: string | null;
      bytes?: number;
      queue_position?: number | null;
    }>(`/api/outsee/jobs/${encodeURIComponent(jobId)}`),

  cancelCreateJob: (jobId: string) =>
    http<{ ok: boolean; job_id: string }>(
      `/api/create/jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    ),

  createQueue: () =>
    http<{
      max_parallel: number;
      max_parallel_outsee?: number;
      running_count: number;
      waiting_count: number;
      total_active: number;
      running: {
        job_id: string;
        status: string;
        media: string;
        model: string;
        history_id: string;
        prompt_preview?: string;
        queue_position?: number | null;
        provider: string;
        created_at?: string | null;
        started_at?: string | null;
        finished_at?: string | null;
        elapsed_sec?: number | null;
        elapsed_label?: string | null;
      }[];
      waiting: {
        job_id: string;
        status: string;
        media: string;
        model: string;
        history_id: string;
        prompt_preview?: string;
        queue_position?: number | null;
        provider: string;
        created_at?: string | null;
        started_at?: string | null;
        finished_at?: string | null;
        elapsed_sec?: number | null;
        elapsed_label?: string | null;
      }[];
      jobs: {
        job_id: string;
        status: string;
        media: string;
        model: string;
        history_id: string;
        prompt_preview?: string;
        queue_position?: number | null;
        provider: string;
        created_at?: string | null;
        started_at?: string | null;
        finished_at?: string | null;
        elapsed_sec?: number | null;
        elapsed_label?: string | null;
      }[];
    }>(`/api/create/queue`),

  // ---- KIE Create (вкладка «Генерация», провайдер kie.ai) ----
  kieCatalog: () =>
    http<KieCatalog>(`/api/kie-create/catalog`),
  kieCredits: () =>
    http<{ configured: boolean; credits: number | null; usd: number | null }>(
      `/api/kie-create/credits`,
    ),
  kieGenerate: (body: { model_id: string; values: Record<string, unknown> }) =>
    http<{
      job: {
        job_id: string;
        status: string;
        history_id: string;
        media: string;
        model: string;
        queue_position?: number | null;
      };
      estimate: { credits: number; usd: number; note?: string };
    }>(`/api/kie-create/generate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  kieUpload: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`/api/kie-create/upload`, { method: "POST", body: fd });
    const data = (await r.json().catch(() => ({}))) as {
      url?: string;
      detail?: string;
    };
    if (!r.ok || !data.url) {
      throw new Error(data.detail || `upload HTTP ${r.status}`);
    }
    return data as { url: string; filename: string; bytes: number };
  },

  createJob: (jobId: string) =>
    http<{
      job_id: string;
      status: string;
      history_id: string;
      preview_url?: string | null;
      error?: string | null;
      queue_position?: number | null;
      model?: string;
      elapsed_sec?: number | null;
      elapsed_label?: string | null;
    }>(`/api/create/jobs/${encodeURIComponent(jobId)}`),

  wizardCatalog: () =>
    http<{
      questions: { field: string; title: string; choices: { id: string; label: string }[]; cols: number }[];
      image_generators?: { id: string; label: string; description?: string }[];
      video_generators?: { id: string; label: string; description?: string }[];
      image_resolutions_by_generator?: Record<string, string[]>;
      defaults?: { image_generator?: string; video_generator?: string };
    }>(`/api/generation-options/wizard`),
  listGenerationConfigPresets: () =>
    http<{
      presets: GenerationConfigPreset[];
      fields: string[];
    }>(`/api/generation-config-presets`),
  createGenerationConfigPreset: (body: {
    name: string;
    settings: GenerationConfigPresetSettings;
  }) =>
    http<GenerationConfigPreset>(`/api/generation-config-presets`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteGenerationConfigPreset: (id: string) =>
    http<{ ok: boolean }>(`/api/generation-config-presets/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  resetProjectStep: (projectId: number, stepCode: string) =>
    http<ProjectDetail>(`/api/projects/${projectId}/steps/${stepCode}/reset`, {
      method: "POST",
    }),
  downloadProjectXlsx: (projectId: number, opts?: { nodeKey?: string }) => {
    const q = opts?.nodeKey
      ? `?node_key=${encodeURIComponent(opts.nodeKey)}`
      : "";
    return `/api/projects/${projectId}/xlsx${q}`;
  },
  reloadProjectXlsx: (projectId: number) =>
    http<ProjectDetail>(`/api/projects/${projectId}/xlsx/reload`, { method: "POST" }),
  uploadProjectXlsx: async (
    projectId: number,
    file: File,
    opts?: { nodeKey?: string },
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    const q = opts?.nodeKey
      ? `?node_key=${encodeURIComponent(opts.nodeKey)}`
      : "";
    const res = await fetch(`/api/projects/${projectId}/xlsx/upload${q}`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<ProjectDetail>;
  },
  previewProjectXlsx: (
    projectId: number,
    opts?: {
      sheet?: string;
      maxRows?: number;
      maxCols?: number;
      startRow?: number;
      row?: number;
      raw?: boolean;
      nodeKey?: string;
    },
  ) => {
    const q = new URLSearchParams();
    if (opts?.sheet) q.set("sheet", opts.sheet);
    if (opts?.maxRows != null) q.set("max_rows", String(opts.maxRows));
    if (opts?.maxCols != null) q.set("max_cols", String(opts.maxCols));
    if (opts?.startRow != null) q.set("start_row", String(opts.startRow));
    if (opts?.row != null) q.set("row", String(opts.row));
    if (opts?.raw) q.set("raw", "true");
    if (opts?.nodeKey) q.set("node_key", opts.nodeKey);
    const qs = q.toString();
    return http<XlsxPreview>(`/api/projects/${projectId}/xlsx/preview${qs ? `?${qs}` : ""}`);
  },
  ensureProjectRun: (projectId: number) =>
    http<{ run_id: number }>(`/api/projects/${projectId}/ensure-run`, { method: "POST" }),

  listProjectAssets: (projectId: number, kind = "all") =>
    http<ProjectAsset[]>(`/api/projects/${projectId}/assets?kind=${kind}`),

  replaceHeroImage: async (projectId: number, file: File, replacePath?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    const q = replacePath ? `?replace_path=${encodeURIComponent(replacePath)}` : "";
    const res = await fetch(`/api/projects/${projectId}/assets/hero/replace${q}`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<{ path: string; preview_url: string; id: string }>;
  },

  listMediaReview: (projectId: number, kind: "images" | "videos") =>
    http<
      {
        frame_id: number;
        number: number;
        voiceover_text: string;
        image_prompt: string | null;
        animation_prompt: string | null;
        status: string;
        artifact_uuid: string | null;
        file_path: string | null;
        preview_url: string | null;
      }[]
    >(`/api/projects/${projectId}/media-review?kind=${kind}`),

  // ── Prompt files (prompts/<step>/*.md на диске) ────────────────────
  getGlobalActivePrompts: () =>
    http<Record<string, string>>("/api/prompt-files/global-active"),
  listPromptFiles: (stepCode: string) =>
    http<PromptFileInfo[]>(`/api/prompt-files/${stepCode}`),
  getPromptFile: (stepCode: string, name: string) =>
    http<PromptFileContent>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}/content`,
    ),
  downloadPromptFileUrl: (stepCode: string, name: string) =>
    `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}/download`,
  savePromptFile: (stepCode: string, name: string, content: string) =>
    http<PromptFileContent>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify({ content }) },
    ),
  deletePromptFile: (stepCode: string, name: string) =>
    http<{ removed: boolean }>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  uploadPromptFile: async (
    stepCode: string,
    file: File,
    name?: string,
  ): Promise<PromptFileInfo> => {
    const fd = new FormData();
    fd.append("file", file);
    const q = name ? `?name=${encodeURIComponent(name)}` : "";
    const res = await fetch(`/api/prompt-files/${stepCode}/upload${q}`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<PromptFileInfo>;
  },
  listPromptFileHistory: (stepCode: string, name: string) =>
    http<PromptVersionInfo[]>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}/history`,
    ),
  getPromptFileHistory: (stepCode: string, name: string, versionId: string) =>
    http<PromptVersionContent>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}/history/${encodeURIComponent(versionId)}/content`,
    ),
  renamePromptFile: (stepCode: string, name: string, newName: string) =>
    http<PromptFileInfo>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}/rename`,
      { method: "PATCH", body: JSON.stringify({ new_name: newName }) },
    ),
  renamePromptVersionLabel: (
    stepCode: string,
    name: string,
    versionId: string,
    label: string,
  ) =>
    http<PromptVersionInfo>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}/history/${encodeURIComponent(versionId)}`,
      { method: "PATCH", body: JSON.stringify({ label }) },
    ),
  restorePromptFileVersion: (stepCode: string, name: string, versionId: string) =>
    http<PromptFileContent>(
      `/api/prompt-files/${stepCode}/${encodeURIComponent(name)}/history/${encodeURIComponent(versionId)}/restore`,
      { method: "POST" },
    ),

  // ── Local library (data/library + SQLite versions) ────────────────
  listLibraryItems: (params?: { kind?: string; q?: string }) => {
    const q = new URLSearchParams();
    if (params?.kind) q.set("kind", params.kind);
    if (params?.q) q.set("q", params.q);
    const qs = q.toString();
    return http<LibraryItemDTO[]>(`/api/library/items${qs ? `?${qs}` : ""}`);
  },
  getLibraryItem: (id: number) =>
    http<LibraryItemDetailDTO>(`/api/library/items/${id}`),
  createLibraryItem: (body: {
    kind: string;
    key?: string;
    title?: string;
    file_path?: string;
    content: string;
    message?: string;
    meta?: Record<string, unknown>;
  }) =>
    http<LibraryItemDetailDTO>(`/api/library/items`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateLibraryItem: (
    id: number,
    body: { title?: string; content: string; message?: string; meta?: Record<string, unknown> },
  ) =>
    http<LibraryItemDetailDTO>(`/api/library/items/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  listLibraryVersions: (id: number) =>
    http<LibraryVersionDTO[]>(`/api/library/items/${id}/versions`),
  restoreLibraryVersion: (id: number, version: number) =>
    http<LibraryItemDetailDTO>(`/api/library/items/${id}/restore/${version}`, {
      method: "POST",
    }),
  downloadLibraryItemUrl: (id: number) => `/api/library/items/${id}/download`,
  saveLibraryConfig: (body: {
    name?: string;
    project_id?: number;
    snapshot?: Record<string, unknown>;
  }) =>
    http<LibraryConfigDTO>(`/api/library/configs/save`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  savePromptBundle: (body: {
    project_id?: number;
    step_id?: string;
    step_code?: string;
    node_type?: string;
    source_name?: string;
    title?: string;
    source_prompt?: string;
    processed_prompt?: string;
    blocks?: { kind: string; label: string; body: string }[];
  }) =>
    http<{ ok: boolean; items: Record<string, unknown> }>(`/api/library/prompt-bundles/save`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── GPT Workspace (свободный чат) ──
  gptListSessions: () =>
    http<{ sessions: GptWorkspaceSessionSummary[] }>(`/api/gpt-workspace/sessions`),
  gptCreateSession: (title?: string) =>
    http<GptWorkspaceSession>(`/api/gpt-workspace/sessions`, {
      method: "POST",
      body: JSON.stringify({ title: title || null }),
    }),
  gptGetSession: (sessionId: string) =>
    http<GptWorkspaceSession>(`/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}`),
  gptRenameSession: (sessionId: string, title: string) =>
    http<GptWorkspaceSession>(`/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  gptDeleteSession: (sessionId: string) =>
    http<{ ok: boolean }>(`/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),
  gptUploadAttachment: async (sessionId: string, file: File | File[]) => {
    const fd = new FormData();
    const list = Array.isArray(file) ? file : [file];
    if (list.length === 1) {
      fd.append("file", list[0]);
    } else {
      for (const f of list) fd.append("files", f);
    }
    const res = await fetch(
      `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/attachments`,
      { method: "POST", body: fd },
    );
    if (!res.ok) throw new ApiError(res.status, await res.text());
    const body = await res.json();
    if (body && Array.isArray(body.files)) {
      return body.files as GptWorkspaceFile[];
    }
    return body as GptWorkspaceFile;
  },
  gptOutputsZipUrl: (sessionId: string) =>
    `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/outputs.zip`,
  gptDeleteAttachment: (sessionId: string, name: string) =>
    http<{ ok: boolean }>(
      `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  gptAttachmentToOutputs: (sessionId: string, name: string) =>
    http<GptWorkspaceFile>(
      `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(name)}/to-outputs`,
      { method: "POST" },
    ),
  gptAsk: (sessionId: string, message: string, withAttachments = true) =>
    // Совпадает с бэкендом gpt_workspace ask_timeout=1800с (30 мин).
    http<GptWorkspaceSession>(
      `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/ask`,
      {
        method: "POST",
        body: JSON.stringify({ message, with_attachments: withAttachments }),
      },
      1_800_000,
    ),
  gptAskStream: async (
    sessionId: string,
    message: string,
    withAttachments = true,
    callbacks?: {
      onDelta?: (delta: string) => void;
      onPhase?: (phase: string, phaseDetail?: string) => void;
      onDone?: (session: GptWorkspaceSession, message: any) => void;
      onError?: (error: string) => void;
    },
    signal?: AbortSignal,
  ) => {
    let res: Response;
    try {
      res = await fetch(
        `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/ask-stream`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message, with_attachments: withAttachments }),
          signal,
        },
      );
    } catch (e: any) {
      if (e?.name === "AbortError" || signal?.aborted) return;
      throw e;
    }

    if (!res.ok) {
      const errTxt = await res.text();
      throw new ApiError(res.status, errTxt);
    }
    if (!res.body) return;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let doneReceived = false;

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.startsWith("data:")) {
            try {
              const data = JSON.parse(trimmed.slice(5).trim());
              if (data.type === "delta" && data.delta) {
                callbacks?.onDelta?.(data.delta);
              } else if (data.type === "phase") {
                callbacks?.onPhase?.(data.phase, data.phase_detail);
              } else if (data.type === "done") {
                doneReceived = true;
                callbacks?.onDone?.(data.session, data.message);
              } else if (data.type === "error" && data.error) {
                callbacks?.onError?.(data.error);
              }
            } catch {
              // ignore JSON parse error
            }
          }
        }
      }
    } catch (err: any) {
      if (err?.name === "AbortError" || signal?.aborted || doneReceived) {
        return;
      }
      throw err;
    }
  },
  gptSaveToProject: (
    sessionId: string,
    body: { project_id: number; output_name: string; as_name?: string },
  ) =>
    http<{ saved_as: string; name: string; size: number }>(
      `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/save-to-project`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  gptSaveVoiceover: (
    sessionId: string,
    body: { project_id: number; message_id?: string },
  ) =>
    http<{ saved_as: string; name: string; chars: number }>(
      `/api/gpt-workspace/sessions/${encodeURIComponent(sessionId)}/save-voiceover`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  compileMetaPrompt: (body: {
    step_code: string;
    user_intent: string;
    project_id?: number | null;
    target_name?: string | null;
  }) =>
    http<{
      ok: boolean;
      step_code: string;
      name: string;
      compiled_prompt: string;
      stats: {
        length_chars: number;
        lines_count: number;
        has_schema: boolean;
        has_guards: boolean;
      };
    }>(
      "/api/meta-agent/compile",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
      120_000,
    ),
  saveAndActivateMetaPrompt: (body: {
    step_code: string;
    name: string;
    content: string;
    project_id?: number | null;
    activate?: boolean;
  }) =>
    http<{
      ok: boolean;
      step_code: string;
      name: string;
      file_name: string;
      file_path: string;
      activated: boolean;
    }>("/api/meta-agent/save-and-activate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  assistProject: (body: {
    topic_draft?: string;
    title_draft?: string;
    tone?: string | null;
    voiceover_style?: string | null;
    mode?: "expand" | "generate";
  }) =>
    http<{
      ok: boolean;
      title: string;
      topic: string;
      suggested_hero_mode: "hero" | "no_hero" | "auto";
      tone?: string | null;
      voiceover_style?: string | null;
    }>(
      "/api/meta-agent/assist-project",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
      90_000,
    ),
};

export type GptWorkspaceSessionSummary = {
  id: string;
  title: string;
  created_at?: string | null;
  updated_at?: string | null;
  message_count: number;
  status: string;
};

export type GptWorkspaceFile = {
  name: string;
  display_name?: string;
  size: number;
  path: string;
  url: string;
  download_url?: string;
  mime?: string;
  kind?: "image" | "file" | string;
};

export type GptWorkspaceMessage = {
  id: string;
  role: "user" | "assistant" | "system" | string;
  content: string;
  at?: string;
  model?: string;
  attachment_names?: string[];
  output_files?: string[];
};

export type GptWorkspaceSession = {
  id: string;
  title: string;
  created_at?: string | null;
  updated_at?: string | null;
  status: string;
  phase?: string;
  phase_detail?: string;
  messages: GptWorkspaceMessage[];
  attachments: GptWorkspaceFile[];
  outputs: GptWorkspaceFile[];
};

export interface PromptFileInfo {
  name: string;
  filename: string;
  size: number;
  modified: number | null;
  is_default: boolean;
}

export interface PromptFileContent {
  name: string;
  filename: string;
  content: string;
  size: number;
  modified: number | null;
}

export interface PromptVersionInfo {
  id: string;
  label: string;
  saved_at: number;
  size: number;
}

export interface PromptVersionContent {
  id: string;
  label: string;
  content: string;
  saved_at: number;
  size: number;
}

/**
 * WebSocket подписка на канал. Возвращает функцию отписки.
 * channel: "global" | "runs.<id>" | "projects.<id>" | "hitl.<id>" | "logs.<id>"
 *
 * Один сокет на channel (hub): иначе React remount / несколько панелей /
 * CONNECTING без close() копят 100+ WS и душат backend (anim_pr / GPT).
 */
type WsHandler = (event: unknown) => void;

type WsHub = {
  channel: string;
  ws: WebSocket | null;
  handlers: Set<WsHandler>;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  backoffMs: number;
  closing: boolean;
};

const WS_HUBS = new Map<string, WsHub>();
const WS_BACKOFF_MIN_MS = 1000;
const WS_BACKOFF_MAX_MS = 15000;

function _wsCloseQuiet(ws: WebSocket | null): void {
  if (!ws) return;
  try {
    // Важно: CLOSE и CONNECTING — иначе orphan-сокеты копятся.
    if (
      ws.readyState === WebSocket.CONNECTING ||
      ws.readyState === WebSocket.OPEN
    ) {
      ws.close();
    }
  } catch {
    /* ignore */
  }
}

function _wsConnectHub(hub: WsHub): void {
  if (hub.closing || hub.handlers.size === 0) return;
  if (
    hub.ws &&
    (hub.ws.readyState === WebSocket.CONNECTING ||
      hub.ws.readyState === WebSocket.OPEN)
  ) {
    return;
  }
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${location.host}/ws/${hub.channel}`;
  const ws = new WebSocket(url);
  hub.ws = ws;
  ws.addEventListener("open", () => {
    hub.backoffMs = WS_BACKOFF_MIN_MS;
  });
  ws.addEventListener("message", (ev) => {
    let data: unknown;
    try {
      data = JSON.parse(ev.data);
    } catch (e) {
      console.warn("ws parse error", e);
      return;
    }
    for (const h of [...hub.handlers]) {
      try {
        h(data);
      } catch (e) {
        console.warn("ws handler error", e);
      }
    }
  });
  ws.addEventListener("close", () => {
    if (hub.ws === ws) hub.ws = null;
    if (hub.closing || hub.handlers.size === 0) return;
    if (hub.reconnectTimer) clearTimeout(hub.reconnectTimer);
    const delay = hub.backoffMs;
    hub.backoffMs = Math.min(hub.backoffMs * 2, WS_BACKOFF_MAX_MS);
    hub.reconnectTimer = setTimeout(() => {
      hub.reconnectTimer = null;
      _wsConnectHub(hub);
    }, delay);
  });
  ws.addEventListener("error", () => {
    // close handler сделает reconnect
  });
}

export function subscribeWS(
  channel: string,
  onMessage: WsHandler,
  onClose?: (reason: string) => void
): () => void {
  let hub = WS_HUBS.get(channel);
  if (!hub) {
    hub = {
      channel,
      ws: null,
      handlers: new Set(),
      reconnectTimer: null,
      backoffMs: WS_BACKOFF_MIN_MS,
      closing: false,
    };
    WS_HUBS.set(channel, hub);
  }
  hub.closing = false;
  hub.handlers.add(onMessage);
  _wsConnectHub(hub);

  return () => {
    hub!.handlers.delete(onMessage);
    if (hub!.handlers.size > 0) return;
    hub!.closing = true;
    if (hub!.reconnectTimer) {
      clearTimeout(hub!.reconnectTimer);
      hub!.reconnectTimer = null;
    }
    _wsCloseQuiet(hub!.ws);
    hub!.ws = null;
    WS_HUBS.delete(channel);
    onClose?.("closed");
  };
}
