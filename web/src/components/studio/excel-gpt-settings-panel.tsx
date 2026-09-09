"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  CheckSquare,
  ChevronDown,
  Eye,
  Film,
  Loader2,
  RefreshCw,
  SlidersHorizontal,
  Upload,
  Users,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { errorMessageFromUnknown } from "@/lib/error-message";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import type { ExcelGptNodeConfig } from "@/lib/excel-gpt-config";
import { withSlotVariant } from "@/lib/prompt-slot-storage";
import {
  EMIT_OPTIONS,
  OUTPUT_OPTIONS,
  ROLE_OPTIONS,
  type OperatorEmitKind,
  type OperatorOutputMode,
  type OperatorRole,
} from "@/lib/gpt-operator";

export function ExcelGptSettingsPanel({
  projectId,
  nodeKey,
  config,
  onConfigChange,
}: {
  projectId: number;
  nodeKey: string;
  config: ExcelGptNodeConfig;
  onConfigChange: (patch: Partial<ExcelGptNodeConfig>) => void;
}) {
  const qc = useQueryClient();
  const resolve = useQuery({
    queryKey: ["gpt-operator-resolve", projectId, nodeKey],
    queryFn: () => api.resolveGptOperator(projectId, nodeKey),
  });

  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.patchGptOperator(projectId, nodeKey, body),
    onSuccess: (res) => {
      if (res.resolve) {
        qc.setQueryData(["gpt-operator-resolve", projectId, nodeKey], res.resolve);
      }
      const cfg = res.resolve?.config || {};
      onConfigChange({
        workMode: (cfg.workMode as ExcelGptNodeConfig["workMode"]) || undefined,
        inputSource: (cfg.inputSource as ExcelGptNodeConfig["inputSource"]) || undefined,
        uploadedFileName: Array.isArray(cfg.uploadedFileNames)
          ? String(cfg.uploadedFileNames[0] || "")
          : undefined,
      });
      void qc.invalidateQueries({ queryKey: ["gpt-operator-resolve", projectId, nodeKey] });
      void qc.invalidateQueries({ queryKey: ["project", projectId] });
      window.setTimeout(() => {
        window.dispatchEvent(new CustomEvent("canvas-save-workflow"));
      }, 60);
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const data = resolve.data;
  const role = (data?.role || "assist") as OperatorRole;
  const checkMode = data?.checkMode === true;
  const checkFix = data?.checkFix !== false;
  const checkPromptSource =
    data?.checkPromptSource === "agent" ? "agent" : "upstream";
  const checkAgentStep = data?.checkAgentStep || null;
  const checkAgentFileName = data?.checkAgentFileName || null;
  const checkAgentChars = data?.checkAgentChars || 0;
  const outputMode = (data?.outputMode || "text") as OperatorOutputMode;
  const emitKinds = (data?.emitKinds?.length
    ? data.emitKinds
    : checkMode || role === "review" || role === "gate" || role === "compare"
      ? (["inputs", "reply_txt"] as OperatorEmitKind[])
      : (["result", "reply_txt"] as OperatorEmitKind[]));
  const sourcePrompts = data?.sourcePrompts || [];

  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
    enabled: Boolean(projectId),
  });

  const metaRecord = useMemo(
    () => ((project.data?.meta || {}) as Record<string, unknown>),
    [project.data?.meta],
  );

  const currentPromptVariant = useMemo(() => {
    const slotVariants = metaRecord.prompt_slot_variants as
      | Record<string, Record<string, string>>
      | undefined;
    const fromSlot = slotVariants?.[nodeKey]?.["main"];
    if (fromSlot) return fromSlot;
    const overrides = project.data?.prompt_overrides as Record<string, string> | undefined;
    return overrides?.["excel_gpt"] || "default";
  }, [metaRecord.prompt_slot_variants, nodeKey, project.data?.prompt_overrides]);

  const activePreset: "characters" | "scenes" | "audit" | "custom" = useMemo(() => {
    if (checkMode) return "audit";
    const lower = currentPromptVariant.toLowerCase();
    if (lower.includes("персонаж") || lower.includes("character")) return "characters";
    if (lower.includes("scene") || lower.includes("grammar")) return "scenes";
    return "custom";
  }, [checkMode, currentPromptVariant]);

  const [applyingPreset, setApplyingPreset] = useState<string | null>(null);

  const handleApplyPreset = async (preset: "characters" | "scenes" | "audit") => {
    setApplyingPreset(preset);
    try {
      if (preset === "characters") {
        await patch.mutateAsync({
          role: "assist",
          checkMode: false,
          outputMode: "project_file",
          emitKinds: ["result", "reply_txt"],
          transport: "api",
        });
        const nextMeta = withSlotVariant(
          metaRecord,
          nodeKey,
          "main",
          "агент по созданию персонажей 02.08.txt",
        );
        await api.patchProject(projectId, { meta: nextMeta });
        await qc.invalidateQueries({ queryKey: ["project", projectId] });
        await qc.invalidateQueries({ queryKey: ["gpt-operator-resolve", projectId, nodeKey] });
        toast.success("Режим «Разметка персонажей» установлен");
      } else if (preset === "scenes") {
        await patch.mutateAsync({
          role: "assist",
          checkMode: false,
          outputMode: "project_file",
          emitKinds: ["result", "reply_txt"],
          transport: "api",
        });
        const nextMeta = withSlotVariant(
          metaRecord,
          nodeKey,
          "main",
          "scene_grammar_unified_agent_v1",
        );
        await api.patchProject(projectId, { meta: nextMeta });
        await qc.invalidateQueries({ queryKey: ["project", projectId] });
        await qc.invalidateQueries({ queryKey: ["gpt-operator-resolve", projectId, nodeKey] });
        toast.success("Режим «Режиссура сцен» установлен");
      } else if (preset === "audit") {
        await patch.mutateAsync({
          role: "review",
          checkMode: true,
          checkFix: true,
          outputMode: "text",
          emitKinds: ["inputs", "reply_txt"],
          transport: "api",
        });
        await qc.invalidateQueries({ queryKey: ["gpt-operator-resolve", projectId, nodeKey] });
        toast.success("Режим «Проверка качества (Аудит)» включён");
      }
    } catch (err) {
      toast.error(errorMessageFromUnknown(err));
    } finally {
      setApplyingPreset(null);
    }
  };

  const [formatDraft, setFormatDraft] = useState("");
  const [formatDirty, setFormatDirty] = useState(false);
  useEffect(() => {
    if (!data || formatDirty) return;
    setFormatDraft(
      String(data.checkReportFormat || data.checkReportFormatDefault || ""),
    );
  }, [
    data,
    formatDirty,
    data?.checkReportFormat,
    data?.checkReportFormatDefault,
  ]);

  const agentFileRef = useRef<HTMLInputElement>(null);
  // Что смотрим в диалоге: финальный промт проверки / файл агента / промт источника.
  const [viewTarget, setViewTarget] = useState<
    { kind: "prompt" } | { kind: "agent" } | { kind: "source"; key: string } | null
  >(null);
  const agentFileView = useQuery({
    queryKey: ["check-agent-file", projectId, nodeKey],
    queryFn: () => api.getCheckAgentFile(projectId, nodeKey),
    enabled: viewTarget?.kind === "agent",
    staleTime: 10_000,
  });
  const viewSourceKey = viewTarget?.kind === "source" ? viewTarget.key : null;
  const sourcePromptView = useQuery({
    queryKey: ["check-source-prompt", projectId, nodeKey, viewSourceKey],
    queryFn: () => api.getGptOperatorSourcePrompt(projectId, nodeKey, viewSourceKey!),
    enabled: viewSourceKey !== null,
    staleTime: 10_000,
  });
  const checkPromptPreview = useQuery({
    queryKey: ["check-prompt-preview", projectId, nodeKey],
    queryFn: () => api.getCheckPromptPreview(projectId, nodeKey),
    enabled: viewTarget?.kind === "prompt",
    staleTime: 10_000,
  });
  const activeView =
    viewTarget?.kind === "prompt"
      ? checkPromptPreview
      : viewTarget?.kind === "agent"
        ? agentFileView
        : sourcePromptView;
  const viewOpen = viewTarget !== null;
  const viewLoading = activeView.isLoading;
  const viewError = activeView.isError ? activeView.error : null;
  const viewText = activeView.data?.text;
  const viewChars = activeView.data?.chars;
  const viewTitle =
    viewTarget?.kind === "prompt"
      ? "Промт проверки — финальный, как уйдёт в GPT"
      : viewTarget?.kind === "source"
        ? `${viewTarget.key}${sourcePromptView.data?.variant ? ` · ${sourcePromptView.data.variant}` : ""}`
        : agentFileView.data?.fileName || checkAgentFileName || "Агент проверки";
  const uploadAgent = useMutation({
    mutationFn: (file: File) => api.uploadCheckAgentFile(projectId, nodeKey, file),
    onSuccess: (res) => {
      toast.success(`Агент: ${res.fileName}`);
      if (res.resolve) {
        qc.setQueryData(["gpt-operator-resolve", projectId, nodeKey], res.resolve);
      }
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });
  const clearAgent = useMutation({
    mutationFn: () => api.clearCheckAgentFile(projectId, nodeKey),
    onSuccess: (res) => {
      toast.success("Свой агент сброшен");
      if (res.resolve) {
        qc.setQueryData(["gpt-operator-resolve", projectId, nodeKey], res.resolve);
      }
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const toggleEmit = (kind: OperatorEmitKind) => {
    const next = emitKinds.includes(kind)
      ? emitKinds.filter((k) => k !== kind)
      : [...emitKinds, kind];
    const safe = next.length ? next : (["result"] as OperatorEmitKind[]);
    patch.mutate({ emitKinds: safe, transport: "api" });
  };

  return (
    <div className="flex flex-col gap-4">
      {/* 1. Quick Task Presets */}
      <section className="rounded-xl border border-cyan-500/25 bg-cyan-500/[0.04] p-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold text-foreground">
              Целевая задача ноды (Быстрый выбор)
            </h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Выберите задачу — промпт и технические параметры выставятся автоматически в один клик.
            </p>
          </div>
        </div>

        <div className="mt-3.5 grid gap-3 sm:grid-cols-3">
          {/* Preset 1: Characters */}
          <button
            type="button"
            disabled={applyingPreset !== null}
            onClick={() => handleApplyPreset("characters")}
            className={cn(
              "flex flex-col text-left p-3.5 rounded-xl border transition-all relative group",
              activePreset === "characters"
                ? "border-cyan-400 bg-cyan-950/40 text-cyan-200 ring-1 ring-cyan-400/60 shadow-sm"
                : "border-white/10 bg-zinc-900/40 text-zinc-300 hover:border-cyan-500/40 hover:bg-zinc-900/80",
            )}
          >
            <div className="flex items-center gap-2 mb-1.5">
              <Users
                className={cn(
                  "h-4 w-4",
                  activePreset === "characters" ? "text-cyan-300" : "text-zinc-400 group-hover:text-cyan-400",
                )}
              />
              <span className="font-semibold text-xs text-foreground">Разметка персонажей</span>
            </div>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              Распределяет героев (c01, c02, c03) по кадрам перед генерацией референсов.
            </p>
            {activePreset === "characters" && (
              <span className="mt-2.5 inline-flex items-center gap-1 text-[10px] font-medium text-cyan-300 bg-cyan-500/10 px-2 py-0.5 rounded border border-cyan-500/20 w-fit">
                <CheckCircle2 className="h-3 w-3" /> Активно
              </span>
            )}
          </button>

          {/* Preset 2: Scenes */}
          <button
            type="button"
            disabled={applyingPreset !== null}
            onClick={() => handleApplyPreset("scenes")}
            className={cn(
              "flex flex-col text-left p-3.5 rounded-xl border transition-all relative group",
              activePreset === "scenes"
                ? "border-cyan-400 bg-cyan-950/40 text-cyan-200 ring-1 ring-cyan-400/60 shadow-sm"
                : "border-white/10 bg-zinc-900/40 text-zinc-300 hover:border-cyan-500/40 hover:bg-zinc-900/80",
            )}
          >
            <div className="flex items-center gap-2 mb-1.5">
              <Film
                className={cn(
                  "h-4 w-4",
                  activePreset === "scenes" ? "text-cyan-300" : "text-zinc-400 group-hover:text-cyan-400",
                )}
              />
              <span className="font-semibold text-xs text-foreground">Режиссура сцен</span>
            </div>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              Обогащает кадры светом, планами камеры, окружением и атмосферой.
            </p>
            {activePreset === "scenes" && (
              <span className="mt-2.5 inline-flex items-center gap-1 text-[10px] font-medium text-cyan-300 bg-cyan-500/10 px-2 py-0.5 rounded border border-cyan-500/20 w-fit">
                <CheckCircle2 className="h-3 w-3" /> Активно
              </span>
            )}
          </button>

          {/* Preset 3: Audit */}
          <button
            type="button"
            disabled={applyingPreset !== null}
            onClick={() => handleApplyPreset("audit")}
            className={cn(
              "flex flex-col text-left p-3.5 rounded-xl border transition-all relative group",
              activePreset === "audit"
                ? "border-cyan-400 bg-cyan-950/40 text-cyan-200 ring-1 ring-cyan-400/60 shadow-sm"
                : "border-white/10 bg-zinc-900/40 text-zinc-300 hover:border-cyan-500/40 hover:bg-zinc-900/80",
            )}
          >
            <div className="flex items-center gap-2 mb-1.5">
              <CheckSquare
                className={cn(
                  "h-4 w-4",
                  activePreset === "audit" ? "text-cyan-300" : "text-zinc-400 group-hover:text-cyan-400",
                )}
              />
              <span className="font-semibold text-xs text-foreground">Проверка качества</span>
            </div>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              Аудит сценария: проверяет данные по чек-листу и формирует check_report.txt.
            </p>
            {activePreset === "audit" && (
              <span className="mt-2.5 inline-flex items-center gap-1 text-[10px] font-medium text-cyan-300 bg-cyan-500/10 px-2 py-0.5 rounded border border-cyan-500/20 w-fit">
                <CheckCircle2 className="h-3 w-3" /> Активно
              </span>
            )}
          </button>
        </div>

        {/* Current Prompt Indicator Banner */}
        <div className="mt-3.5 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-white/10 bg-black/25 px-3 py-2 text-xs">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-muted-foreground shrink-0">Активный файл промпта:</span>
            <span className="font-mono text-cyan-200 truncate">{currentPromptVariant}</span>
          </div>
          {currentPromptVariant === "default" || currentPromptVariant.includes("default") ? (
            <span className="inline-flex items-center gap-1 text-[11px] text-amber-300 shrink-0">
              <AlertTriangle className="h-3.5 w-3.5" /> Рекомендуется выбрать задачу выше
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-[11px] text-emerald-400 shrink-0">
              <CheckCircle2 className="h-3.5 w-3.5" /> Готов к запуску
            </span>
          )}
        </div>
      </section>

      {/* 3. Check Mode Section (Only when checkMode is active) */}
      {checkMode && (
        <section className="rounded-xl border border-cyan-500/25 bg-cyan-950/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold text-foreground flex items-center gap-1.5">
                <CheckSquare className="h-4 w-4 text-cyan-400" />
                Параметры проверки и аудита
              </h3>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Отчёт check_report.txt пишется после запуска ▶.
              </p>
            </div>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() =>
                patch.mutate({
                  checkMode: false,
                  role: "assist",
                  emitKinds: ["result", "reply_txt"],
                  outputMode: "project_file",
                  transport: "api",
                })
              }
            >
              Отключить проверку
            </Button>
          </div>

          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              type="button"
              size="sm"
              variant={checkFix ? "default" : "outline"}
              className={cn(checkFix && "bg-cyan-600 text-white hover:bg-cyan-500 border-cyan-500")}
              onClick={() => patch.mutate({ checkFix: true, transport: "api" })}
            >
              Автоисправление (Чинить)
            </Button>
            <Button
              type="button"
              size="sm"
              variant={!checkFix ? "default" : "outline"}
              className={cn(!checkFix && "bg-cyan-600 text-white hover:bg-cyan-500 border-cyan-500")}
              onClick={() => patch.mutate({ checkFix: false, transport: "api" })}
            >
              Только отчёт (Без записи)
            </Button>

            <Button
              type="button"
              size="sm"
              variant={viewTarget?.kind === "prompt" ? "outline" : "secondary"}
              className="gap-1.5"
              onClick={() =>
                setViewTarget(viewTarget?.kind === "prompt" ? null : { kind: "prompt" })
              }
            >
              <Eye className="h-3.5 w-3.5" />
              Просмотр промпта проверки
            </Button>
          </div>

          {/* Criteria Sources */}
          <div className="mt-3.5 space-y-2 border-t border-white/5 pt-3">
            <p className="text-[11px] font-medium text-muted-foreground">
              Откуда брать критерии проверки:
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant={checkPromptSource === "upstream" ? "default" : "outline"}
                className={cn(
                  checkPromptSource === "upstream" && "bg-cyan-600 text-white hover:bg-cyan-500 border-cyan-500",
                )}
                title="Активный мастер-промт ноды выше по стрелке"
                onClick={() =>
                  patch.mutate({ checkPromptSource: "upstream", transport: "api" })
                }
              >
                {checkPromptSource === "upstream" ? "✓ " : ""}
                Промпт источника (выше по стрелке)
              </Button>
              <Button
                type="button"
                size="sm"
                variant={checkPromptSource === "agent" ? "default" : "outline"}
                className={cn(
                  checkPromptSource === "agent" && "bg-cyan-600 text-white hover:bg-cyan-500 border-cyan-500",
                )}
                title="Свой .txt/.md или встроенный агент"
                onClick={() =>
                  patch.mutate({ checkPromptSource: "agent", transport: "api" })
                }
              >
                {checkPromptSource === "agent" ? "✓ " : ""}
                Готовый агент проверки
              </Button>
            </div>

            {checkPromptSource === "agent" ? (
              <div className="space-y-2 mt-2">
                <p className="text-[11px] text-muted-foreground">
                  {checkAgentFileName ? (
                    <>
                      Файл: <span className="font-mono text-cyan-200">{checkAgentFileName}</span>
                      {checkAgentChars ? ` · ${checkAgentChars} симв.` : ""}
                    </>
                  ) : (
                    <>
                      Встроенный: <span className="font-mono text-cyan-200">{checkAgentStep || "—"}</span>
                    </>
                  )}
                </p>
                <div className="flex flex-wrap gap-2">
                  <input
                    ref={agentFileRef}
                    type="file"
                    accept=".txt,.md,text/plain,text/markdown"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      e.target.value = "";
                      if (f) uploadAgent.mutate(f);
                    }}
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={uploadAgent.isPending}
                    onClick={() => agentFileRef.current?.click()}
                    className="gap-1.5"
                  >
                    {uploadAgent.isPending ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Upload className="h-3.5 w-3.5" />
                    )}
                    Загрузить .txt / .md
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="gap-1.5"
                    onClick={() =>
                      setViewTarget(
                        viewTarget?.kind === "agent" ? null : { kind: "agent" },
                      )
                    }
                  >
                    <Eye className="h-3.5 w-3.5" />
                    Просмотр агента
                  </Button>
                  {checkAgentFileName ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      disabled={clearAgent.isPending}
                      onClick={() => clearAgent.mutate()}
                    >
                      Сбросить
                    </Button>
                  ) : null}
                </div>
              </div>
            ) : (
              <ul className="space-y-1 text-[11px] mt-2">
                {sourcePrompts.length ? (
                  sourcePrompts.map((s) => (
                    <li
                      key={String(s.nodeKey)}
                      className={cn(
                        "flex items-center gap-1.5",
                        s.ok ? "text-emerald-300" : "text-destructive",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">
                        {s.ok ? "✓" : "✗"} {s.nodeKey}
                        {s.variant ? ` · ${s.variant}` : ""}
                        {s.chars ? ` · ${s.chars} симв` : ""}
                        {s.error ? ` · ${s.error}` : ""}
                      </span>
                      {s.ok ? (
                        <button
                          type="button"
                          title="Просмотр промта источника"
                          className="shrink-0 rounded p-0.5 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
                          onClick={() =>
                            setViewTarget({ kind: "source", key: String(s.nodeKey) })
                          }
                        >
                          <Eye className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                    </li>
                  ))
                ) : (
                  <li className="text-muted-foreground">Нет входящих стрелок с промптами</li>
                )}
              </ul>
            )}
          </div>

          {/* Collapsible report format */}
          <details className="mt-3.5 rounded-lg border border-white/10 bg-black/20 p-3 group">
            <summary className="flex cursor-pointer items-center justify-between text-[11px] font-medium text-muted-foreground hover:text-foreground select-none">
              <span className="flex items-center gap-1.5">
                <span>Шаблон формата отчёта GPT</span>
                {data?.checkReportFormatCustom ? (
                  <span className="text-amber-300 font-mono">(свой)</span>
                ) : (
                  <span className="opacity-60 font-mono">(стандартный)</span>
                )}
              </span>
              <ChevronDown className="h-3.5 w-3.5 transition group-open:rotate-180" />
            </summary>
            <div className="mt-3 space-y-2 border-t border-white/5 pt-2">
              <div className="flex flex-wrap items-center justify-end gap-1.5">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={patch.isPending || !formatDirty}
                  onClick={() => {
                    patch.mutate(
                      { checkReportFormat: formatDraft, transport: "api" },
                      {
                        onSuccess: () => {
                          setFormatDirty(false);
                          toast.success("Формат отчёта сохранён");
                        },
                      },
                    );
                  }}
                >
                  Сохранить
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={patch.isPending}
                  onClick={() => {
                    const def = String(data?.checkReportFormatDefault || "");
                    setFormatDraft(def);
                    setFormatDirty(false);
                    patch.mutate(
                      { checkReportFormat: null, transport: "api" },
                      {
                        onSuccess: () => toast.success("Формат сброшен к дефолту"),
                      },
                    );
                  }}
                >
                  Дефолт
                </Button>
              </div>
              <Textarea
                value={formatDraft}
                onChange={(e) => {
                  setFormatDraft(e.target.value);
                  setFormatDirty(true);
                }}
                rows={10}
                className="min-h-[160px] font-mono text-[11px] leading-snug"
                spellCheck={false}
              />
            </div>
          </details>
        </section>
      )}

      {/* 4. Collapsible Advanced Technical Settings */}
      <details className="group rounded-xl border border-white/10 bg-white/[0.02] p-4">
        <summary className="flex cursor-pointer items-center justify-between font-medium text-xs text-muted-foreground hover:text-cyan-300 select-none">
          <div className="flex items-center gap-2">
            <SlidersHorizontal className="h-3.5 w-3.5 text-cyan-400" />
            <span>Расширенные технические настройки (роли, потоки, форматы)</span>
          </div>
          <ChevronDown className="h-4 w-4 transition-transform duration-200 group-open:rotate-180 text-muted-foreground" />
        </summary>

        <div className="mt-4 flex flex-col gap-4 border-t border-white/5 pt-4">
          {/* Roles */}
          <div>
            <h4 className="text-xs font-semibold text-foreground">Техническая роль</h4>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              Транспорт: API (прямой вызов модели без браузера).
            </p>
            <div className="mt-2.5 grid gap-2 sm:grid-cols-3">
              {ROLE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => patch.mutate({ role: opt.value, transport: "api" })}
                  className={cn(
                    "rounded-lg border px-3 py-2 text-left transition",
                    role === opt.value
                      ? "border-cyan-400/60 bg-cyan-950/40 text-cyan-200 ring-1 ring-cyan-400/50"
                      : "border-white/10 bg-black/20 text-muted-foreground hover:border-white/20 hover:text-foreground",
                  )}
                >
                  <span className="block text-[12px] font-medium">{opt.title}</span>
                  <span className="mt-0.5 block text-[10px] opacity-75">{opt.hint}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Output Mode */}
          <div>
            <h4 className="text-xs font-semibold text-foreground">Формат ответа GPT</h4>
            <div className="mt-2 flex flex-wrap gap-2">
              {OUTPUT_OPTIONS.map((opt) => (
                <Button
                  key={opt.value}
                  type="button"
                  size="sm"
                  title={opt.hint}
                  variant={outputMode === opt.value ? "default" : "outline"}
                  className={cn(
                    outputMode === opt.value &&
                      "bg-cyan-600 text-white hover:bg-cyan-500 border-cyan-500",
                  )}
                  onClick={() => patch.mutate({ outputMode: opt.value, transport: "api" })}
                >
                  {opt.title}
                </Button>
              ))}
            </div>
          </div>

          {/* What to Emit */}
          <div>
            <h4 className="text-xs font-semibold text-foreground">Что отдаёт дальше по стрелкам</h4>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              Файлы и потоки данных для последующих нод.
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {EMIT_OPTIONS.map((opt) => {
                const on = emitKinds.includes(opt.value);
                return (
                  <Button
                    key={opt.value}
                    type="button"
                    size="sm"
                    variant={on ? "default" : "outline"}
                    title={opt.hint}
                    className={cn(
                      on && "bg-cyan-600 text-white hover:bg-cyan-500 border-cyan-500",
                    )}
                    onClick={() => toggleEmit(opt.value)}
                  >
                    {opt.title}
                  </Button>
                );
              })}
            </div>
          </div>
        </div>
      </details>

      {/* 5. Input Files Inspection */}
      <section className="rounded-xl border border-white/10 bg-white/[0.02] p-4">
        <div className="flex items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-foreground">Фактические файлы на входе</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Список файлов с диска и со стрелок графа.
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void resolve.refetch()}
            className="hover:border-cyan-400 hover:text-cyan-300"
          >
            {resolve.isFetching ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            Сверить
          </Button>
        </div>

        {!data?.consistent ? (
          <p className="mt-2 text-[11px] text-destructive">
            {(data?.errors || []).join("; ") || "Рассинхрон данных"}
          </p>
        ) : null}

        <ul className="mt-3 max-h-48 space-y-1.5 overflow-y-auto">
          {(data?.files || []).map((f) => (
            <li
              key={f.path}
              className={cn(
                "flex items-center gap-2 rounded-md border px-2 py-1.5 text-[11px]",
                f.ok ? "border-white/10" : "border-destructive/40 text-destructive",
              )}
            >
              {f.preview_url && f.kind === "image" ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={f.preview_url} alt="" className="h-8 w-8 rounded object-cover" />
              ) : (
                <span className="flex h-8 w-8 items-center justify-center rounded bg-white/5 font-mono text-[9px] uppercase">
                  {f.kind.slice(0, 3)}
                </span>
              )}
              <span className="min-w-0 flex-1 truncate font-mono">
                {f.name}
                {f.fromNode ? ` ← ${f.fromNode}` : ` · ${f.origin}`}
              </span>
            </li>
          ))}
          {!resolve.isLoading && !(data?.files || []).length ? (
            <li className="text-[11px] text-muted-foreground">Файлов нет</li>
          ) : null}
        </ul>
      </section>

      {/* 6. Text Preview Overlay */}
      {viewOpen ? (
        <section className="overflow-hidden rounded-xl border border-cyan-500/30 bg-black/40">
          <div className="flex items-center justify-between gap-2 border-b border-white/10 px-3 py-1.5 bg-white/[0.03]">
            <p className="min-w-0 truncate font-mono text-[11px] text-foreground">
              {viewTitle}
              {viewChars ? (
                <span className="text-muted-foreground"> · {viewChars} симв.</span>
              ) : null}
            </p>
            <button
              type="button"
              title="Скрыть"
              className="shrink-0 rounded p-0.5 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
              onClick={() => setViewTarget(null)}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <ScrollArea className="max-h-[55vh]">
            {viewLoading ? (
              <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Читаю файл…
              </div>
            ) : viewError ? (
              <p className="p-4 text-xs text-destructive">
                {errorMessageFromUnknown(viewError)}
              </p>
            ) : (
              <pre className="whitespace-pre-wrap p-4 font-mono text-[11px] leading-snug text-foreground/90">
                {viewText || ""}
              </pre>
            )}
          </ScrollArea>
        </section>
      ) : null}
    </div>
  );
}
