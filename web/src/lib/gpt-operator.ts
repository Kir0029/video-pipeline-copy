/** Контракт оператора GPT: роли, стрелки, фактические файлы. */

/** Связь: порядок + кандидат на вход. Файлы — настройка приёмника (takeFromEdges). */
export type OperatorEdgeKind = "after" | "gate" | "pass" | "fail" | "feed" | "review";

export type OperatorRole =
  | "assist"
  | "review"
  | "transform"
  | "extract"
  | "compare"
  | "gate";

export type OperatorOutputMode = "text" | "project_file" | "sidecar";

/** Что нода отдаёт дальше по стрелке (мультивыбор). */
export type OperatorEmitKind = "result" | "reply_txt" | "analysis" | "inputs";

export interface OperatorFileProbe {
  name: string;
  path: string;
  exists: boolean;
  size: number;
  ok: boolean;
  kind: string;
  origin: string;
  fromNode?: string | null;
  preview_url?: string | null;
  error?: string | null;
}

export interface OperatorEdgeSummary {
  id: string;
  source: string;
  target: string;
  kind: OperatorEdgeKind;
  fileCount?: number;
  ok?: boolean;
  errors?: string[];
  takesFiles?: boolean;
}

export interface OperatorBranching {
  enabled: boolean;
  passEdges: OperatorEdgeSummary[];
  failEdges: OperatorEdgeSummary[];
  hasPass: boolean;
  hasFail: boolean;
  verdict?: "pass" | "fail" | null;
}

export interface CheckAnalysisItem {
  id: string;
  ok: boolean;
  note?: string;
}

export interface CheckAnalysisView {
  schema?: string;
  verdict?: "pass" | "fail" | string;
  summary?: string;
  checks?: CheckAnalysisItem[];
  forward?: { mode?: string; paths?: string[] };
  fix?: { target?: string; instructions?: string; rewrite_file?: string | null };
  raw_error?: string | null;
}

export interface SourcePromptView {
  nodeKey?: string | null;
  nodeType?: string | null;
  stepCode?: string | null;
  ok: boolean;
  chars: number;
  variant?: string | null;
  source?: string | null;
  path?: string | null;
  error?: string | null;
}

export interface OperatorResolve {
  nodeKey: string;
  role: OperatorRole;
  outputMode: OperatorOutputMode;
  emitKinds: OperatorEmitKind[];
  useSnapshot: boolean;
  takeFromEdges: boolean;
  /** Тумблер «Проверка»: промты со стрелок + check_report.txt. */
  checkMode?: boolean;
  /** true = чинить, false = только отчёт. */
  checkFix?: boolean;
  /** upstream = промты прошлой ноды; agent = готовый агент check_operator. */
  checkPromptSource?: "upstream" | "agent";
  /** Имя агента (plan/images/… или upload:file.txt). */
  checkAgentStep?: string | null;
  /** Загруженный .txt/.md агента (если есть). */
  checkAgentFileName?: string | null;
  /** Размер текста загруженного агента. */
  checkAgentChars?: number;
  /** Шаблон ответа модели (дефолт или свой на ноде). */
  checkReportFormat?: string;
  checkReportFormatCustom?: boolean;
  checkReportFormatDefault?: string;
  sourcePrompts?: SourcePromptView[];
  transport: string;
  label: string;
  files: OperatorFileProbe[];
  okFileCount: number;
  incomingEdges: {
    id: string;
    source: string;
    target: string;
    kind: OperatorEdgeKind;
    fileCount: number;
    ok: boolean;
    errors: string[];
    takesFiles?: boolean;
  }[];
  outgoingEdges: OperatorEdgeSummary[];
  branching?: OperatorBranching;
  /** Последний разбор vp.check.v1 / TXT-отчёта (если нода проверяет). */
  analysis?: CheckAnalysisView | null;
  errors: string[];
  warnings: string[];
  consistent: boolean;
  canRun: boolean;
  lastResult?: Record<string, unknown>;
  config?: Record<string, unknown>;
}

/** Только семантика стрелки — без отдельной кнопки «Файлы». */
export const EDGE_KIND_OPTIONS: {
  value: "after" | "pass" | "fail";
  title: string;
  short: string;
  hint: string;
}[] = [
  {
    value: "after",
    title: "Связь",
    short: "связь",
    hint: "Порядок шагов. Файлы подтянет приёмник, если у него включён вход от прошлых нод.",
  },
  {
    value: "pass",
    title: "Ок",
    short: "ок",
    hint: "Ветка «всё хорошо»: дальше только при вердикте pass.",
  },
  {
    value: "fail",
    title: "Не ок",
    short: "не ок",
    hint: "Ветка «нужно чинить»: дальше только при вердикте fail.",
  },
];

export const EDGE_KIND_MENU_OPTIONS = EDGE_KIND_OPTIONS;

export const ROLE_OPTIONS: { value: OperatorRole; title: string; hint: string }[] = [
  { value: "assist", title: "Участвует", hint: "Обычный шаг пайплайна" },
  { value: "review", title: "Проверяет", hint: "Ок / не ок — две ветки" },
  { value: "transform", title: "Переделывает", hint: "Вход → другой вид" },
  { value: "extract", title: "Достаёт данные", hint: "Структура из файла" },
  { value: "compare", title: "Сравнивает", hint: "Два входа" },
  { value: "gate", title: "Шлагбаум", hint: "Ок / не ок — две ветки" },
];

export const OUTPUT_OPTIONS: {
  value: OperatorOutputMode;
  title: string;
  hint: string;
}[] = [
  { value: "text", title: "Текст", hint: "Ответ в gpt_reply.txt (Excel не трогает)" },
  {
    value: "project_file",
    title: "Excel проекта",
    hint: "Пишет обратно в project.xlsx",
  },
  {
    value: "sidecar",
    title: "Отдельный .txt",
    hint: "Сохраняет ответ рядом с нодой, project.xlsx не меняет",
  },
];

/** Что отдаёт дальше следующей ноде (мультивыбор). */
export const EMIT_OPTIONS: {
  value: OperatorEmitKind;
  title: string;
  hint: string;
}[] = [
  {
    value: "result",
    title: "Результат",
    hint: "xlsx / скачанные файлы (без reply и analysis)",
  },
  {
    value: "reply_txt",
    title: "Текст .txt",
    hint: "gpt_reply.txt или operator_transform.txt",
  },
  {
    value: "analysis",
    title: "Проверка",
    hint: "analysis.json (карточка вердикта)",
  },
  {
    value: "inputs",
    title: "Вход как есть",
    hint: "те же файлы, что пришли на ноду",
  },
];

export const BRANCHING_ROLES: OperatorRole[] = ["review", "gate", "compare"];

export const ROLE_DEFAULT_LABELS: Record<OperatorRole, string> = {
  assist: "ИИ-редактор сцен",
  review: "ИИ-редактор сцен",
  transform: "ИИ-редактор сцен",
  extract: "ИИ-редактор сцен",
  compare: "ИИ-редактор сцен",
  gate: "ИИ-редактор сцен",
};

export const OPERATOR_MENU_ACTIONS = [
  { id: "upload", group: "in", title: "Загрузить файл(ы)" },
  { id: "from_edge", group: "in", title: "Взять от прошлых нод (стрелки)" },
  { id: "multi", group: "in", title: "Несколько файлов" },
  { id: "snapshot", group: "in", title: "Снимок результата, не live" },
  { id: "role_assist", group: "role", title: "Роль: участвует" },
  { id: "role_review", group: "role", title: "Роль: проверяет" },
  { id: "role_transform", group: "role", title: "Роль: переделывает" },
  { id: "role_extract", group: "role", title: "Роль: достаёт данные" },
  { id: "role_compare", group: "role", title: "Роль: сравнивает" },
  { id: "role_gate", group: "role", title: "Роль: шлагбаум" },
  { id: "out_text", group: "out", title: "Выход: текст" },
  { id: "out_project", group: "out", title: "Выход: файл проекта" },
  { id: "out_sidecar", group: "out", title: "Выход: отдельный .txt" },
  { id: "emit_result", group: "emit", title: "Отдаёт: результат" },
  { id: "emit_reply", group: "emit", title: "Отдаёт: текст .txt" },
  { id: "emit_analysis", group: "emit", title: "Отдаёт: проверка" },
  { id: "emit_inputs", group: "emit", title: "Отдаёт: вход как есть" },
  { id: "prompts", group: "always", title: "Промт + короткий текст" },
  { id: "resolve", group: "always", title: "Показать файлы / пересверить" },
] as const;

export function normalizeEdgeKind(raw?: string | null): OperatorEdgeKind {
  const s = String(raw || "after").trim().toLowerCase();
  if (s === "gate") return "gate";
  if (s === "pass" || s === "ok" || s === "если ok") return "pass";
  if (s === "fail" || s === "не ок" || s === "неok" || s === "not_ok") return "fail";
  // legacy feed/review → обычная связь
  if (s === "feed" || s === "review" || s === "after") return "after";
  return "after";
}

export function isPassEdgeKind(kind?: string | null): boolean {
  const k = normalizeEdgeKind(kind);
  return k === "pass" || k === "gate";
}

export function isFailEdgeKind(kind?: string | null): boolean {
  return normalizeEdgeKind(kind) === "fail";
}

export function edgeKindLabel(kind?: string | null): string {
  const k = normalizeEdgeKind(kind);
  if (k === "gate") return "ок";
  const found = EDGE_KIND_OPTIONS.find((o) => o.value === k);
  return found?.short ?? "связь";
}

export function roleChip(role?: string | null): string {
  const found = ROLE_OPTIONS.find((o) => o.value === role);
  return found?.title ?? "Участвует";
}

export function defaultLabelForRole(role?: string | null): string {
  const r = (role || "assist") as OperatorRole;
  return ROLE_DEFAULT_LABELS[r] ?? "ИИ-редактор сцен";
}

export function isBranchingRole(role?: string | null): boolean {
  return BRANCHING_ROLES.includes((role || "") as OperatorRole);
}

export function nextEdgeKind(kind?: string | null): "after" | "pass" | "fail" {
  const order: Array<"after" | "pass" | "fail"> = ["after", "pass", "fail"];
  const cur = normalizeEdgeKind(kind);
  const mapped = cur === "gate" ? "pass" : cur === "pass" || cur === "fail" ? cur : "after";
  const i = order.indexOf(mapped);
  return order[(i + 1) % order.length];
}
