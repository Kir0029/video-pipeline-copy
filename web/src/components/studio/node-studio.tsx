"use client";

import type { SyntheticEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  FileSpreadsheet,
  FileText,
  Loader2,
  MessageSquareText,
  Play,
  RefreshCw,
  Settings2,
  Upload,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { errorMessageFromUnknown } from "@/lib/error-message";
import { api } from "@/lib/api";
import { getNodeSpec } from "@/lib/node-catalog";
import { getNodeIcon } from "@/lib/node-icons";
import { nodeTypeFromKey } from "@/lib/node-key";
import { stepCodeForNodeType, stepHasPromptVariants } from "@/lib/node-step-map";
import {
  defaultPromptSlots,
  gptTextSlotForNode,
  nodeTypeRequiresExcel,
  pipelinePromptSlots,
  resolvePromptSlots,
  resolvePromptSlotsForNode,
  sceneAgentFromNodeKey,
  type NodePromptSlot,
} from "@/lib/node-prompts";
import { nodeSupportsGptText } from "@/lib/gpt-text-steps";
import {
  excelGptAttachmentChipTitle,
  excelGptSlotIndex,
  isExcelGptNode,
  EXCEL_GPT_STEP_CODE,
  workModeLabel,
  type ExcelGptNodeConfig,
} from "@/lib/excel-gpt-config";
import { ExcelGptSettingsPanel } from "@/components/studio/excel-gpt-settings-panel";
import { ItemsConfigPanel } from "@/components/canvas/items-config-panel";
import { HeroConfigPanel } from "@/components/canvas/hero-config-panel";
import { CheckNodePromptPanel } from "@/components/studio/check-node-prompt-panel";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { formatNodeKeyLabel, humanizeSlug } from "@/lib/format-labels";
import { cn } from "@/lib/utils";
import { promptPathsForNode, legacyPromptFolder } from "@/lib/prompt-catalog";
import {
  activeVariantForExcelGpt,
  activeVariantForSlot,
  activeVariantSourceForExcelGpt,
  activeVariantSourceForSlot,
  preferredPromptFileName,
  promptSourceLabel,
  withSlotVariant,
} from "@/lib/prompt-slot-storage";
import {
  pickDefaultSheetForNode,
  xlsxPreviewFocusForNode,
  xlsxStudioPreviewParams,
} from "@/lib/xlsx-sheets";
import { StudioExcelGrid } from "@/components/studio/studio-excel-grid";
import { FramePromptsPanel } from "@/components/studio/frame-prompts-panel";
import { NodeResultViewBody } from "@/components/canvas/node-result-views";
import { resolveNodeResult } from "@/lib/node-result-resolver";
import type { FrameDTO } from "@/lib/types";
import { NodeStepParamsPanel } from "@/components/studio/node-step-params-panel";
import { PromptFilesPanel } from "@/components/studio/prompt-files-panel";
import { GptTextPanel } from "@/components/studio/gpt-text-panel";
import { shouldShowStopBar } from "@/lib/project-running";

type StudioTab = "settings" | "prompts" | "results" | "excel";

function slotStepCode(slot: NodePromptSlot | null, nodeStepCode: string | undefined): string | undefined {
  return slot?.stepCode ?? nodeStepCode;
}

export function NodeStudio({
  open,
  onOpenChange,
  projectId,
  nodeKey,
  initialTab = "settings",
  promptFocus,
  nodeDisabled = false,
  promptSlots: promptSlotsProp,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  projectId: number | null;
  nodeKey: string | null;
  initialTab?: StudioTab;
  promptFocus?: NodePromptSlot | null;
  nodeDisabled?: boolean;
  promptSlots?: NodePromptSlot[];
}) {
  const nodeType = nodeTypeFromKey(nodeKey);
  const spec = getNodeSpec(nodeType);
  const stepCode = stepCodeForNodeType(nodeType);
  const NodeIcon = getNodeIcon(spec.iconKey);

  const [tab, setTab] = useState<StudioTab>(initialTab);
  const [activeSlotId, setActiveSlotId] = useState<string | null>(null);
  const [xlsxSheet, setXlsxSheet] = useState<string>("");
  /** false = весь лист (дефолт); true = узкий ключевой фрагмент по типу ноды */
  const [xlsxFocusKeyRows, setXlsxFocusKeyRows] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const qc = useQueryClient();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId!),
    enabled: open && projectId != null,
    refetchInterval: (q) =>
      open && shouldShowStopBar(q.state.data?.status, q.state.data?.generation_active)
        ? 1500
        : false,
  });
  const globalActivePrompts = useQuery({
    queryKey: ["prompt-global-active"],
    queryFn: () => api.getGlobalActivePrompts(),
    enabled: open,
    staleTime: 5000,
  });
  const generationRunning = shouldShowStopBar(
    project.data?.status,
    project.data?.generation_active,
  );

  const excelGptConfig = useMemo((): ExcelGptNodeConfig => {
    if (!nodeKey || !isExcelGptNode(nodeType)) return {};
    const meta = (project.data?.meta || {}) as {
      excel_gpt_nodes?: Record<string, ExcelGptNodeConfig>;
    };
    const cfg = meta.excel_gpt_nodes?.[nodeKey] ?? {};
    const slotIndex = excelGptSlotIndex(nodeKey, cfg.slotIndex);
    return {
      label: cfg.label ?? spec.label,
      inputSource: cfg.inputSource ?? "project_xlsx",
      uploadedFileName: cfg.uploadedFileName,
      uploadedPreviewUrl: cfg.uploadedPreviewUrl,
      slotIndex,
      workMode: cfg.workMode ?? "assist",
      lastReplyPath: cfg.lastReplyPath,
      lastReplyAt: cfg.lastReplyAt,
    };
  }, [project.data?.meta, nodeKey, nodeType, spec.label]);

  const [excelConfig, setExcelConfig] = useState<ExcelGptNodeConfig>(excelGptConfig);
  useEffect(() => {
    setExcelConfig(excelGptConfig);
  }, [excelGptConfig]);

  // Тот же queryKey, что у ExcelGptSettingsPanel — общий кэш, лишних запросов нет.
  const operatorResolve = useQuery({
    queryKey: ["gpt-operator-resolve", projectId, nodeKey],
    queryFn: () => api.resolveGptOperator(projectId!, nodeKey!),
    enabled: open && projectId != null && !!nodeKey && isExcelGptNode(nodeType),
    staleTime: 5000,
  });
  const isCheckNode =
    isExcelGptNode(nodeType) && operatorResolve.data?.checkMode === true;

  const patchExcelNodeData = (patch: Partial<ExcelGptNodeConfig>) => {
    if (!nodeKey) return;
    setExcelConfig((prev) => ({ ...prev, ...patch }));
    window.dispatchEvent(
      new CustomEvent("canvas-patch-node-data", {
        detail: { nodeKey, patch },
      }),
    );
  };
  const artifacts = useQuery({
    queryKey: ["artifacts", projectId, nodeType],
    queryFn: () => api.listArtifacts({ project_id: projectId! }),
    enabled: open && projectId != null,
  });
  const allSlots = useMemo(() => {
    if (promptSlotsProp?.length) return resolvePromptSlots(nodeType, promptSlotsProp, nodeKey ?? undefined);
    const meta = (project.data?.meta || {}) as { custom_prompts?: Record<string, NodePromptSlot[]> };
    if (nodeKey) return resolvePromptSlotsForNode(nodeKey, nodeType, meta.custom_prompts);
    return resolvePromptSlots(nodeType, null);
  }, [project.data?.meta, nodeKey, nodeType, promptSlotsProp]);

  const showExcel =
    nodeTypeRequiresExcel(nodeType) ||
    allSlots.some((s) => s.kind === "excel") ||
    tab === "excel" ||
    isExcelGptNode(nodeType);

  const xlsxSheetsMeta = useQuery({
    queryKey: ["xlsx-sheets", projectId, nodeKey ?? "live"],
    queryFn: () =>
      api.previewProjectXlsx(projectId!, {
        maxRows: 1,
        nodeKey: nodeKey ?? undefined,
      }),
    enabled: open && projectId != null && showExcel,
  });

  const focusAvailable = useMemo(() => xlsxPreviewFocusForNode(nodeType) != null, [nodeType]);
  const xlsxParams = useMemo(
    () => xlsxStudioPreviewParams(nodeType, { focusKeyRows: xlsxFocusKeyRows && focusAvailable }),
    [nodeType, xlsxFocusKeyRows, focusAvailable],
  );

  const xlsxPreview = useQuery({
    queryKey: [
      "xlsx-preview",
      projectId,
      nodeKey ?? "live",
      xlsxSheet,
      xlsxParams.startRow,
      xlsxParams.maxRows,
      xlsxFocusKeyRows,
    ],
    queryFn: () =>
      api.previewProjectXlsx(projectId!, {
        sheet: xlsxSheet || undefined,
        raw: true,
        maxRows: xlsxParams.maxRows,
        maxCols: xlsxParams.maxCols,
        startRow: xlsxParams.startRow,
        nodeKey: nodeKey ?? undefined,
      }),
    enabled:
      open &&
      projectId != null &&
      tab === "excel" &&
      Boolean(xlsxSheet || xlsxSheetsMeta.data?.sheets?.length),
  });

  const pipelineSlots = useMemo(() => pipelinePromptSlots(allSlots), [allSlots]);

  useEffect(() => {
    if (!open) return;
    if (promptFocus) {
      setTab(promptFocus.kind === "excel" ? "excel" : "prompts");
      return;
    }
    setTab(initialTab);
  }, [open, initialTab, promptFocus]);

  useEffect(() => {
    if (!open) return;
    if (promptFocus?.id) {
      setActiveSlotId(promptFocus.id);
      return;
    }
    const firstPrompt = pipelineSlots.find((s) => s.kind !== "excel");
    if (firstPrompt) {
      setActiveSlotId(firstPrompt.id);
      return;
    }
    const excel = pipelineSlots.find((s) => s.kind === "excel");
    if (excel) {
      setActiveSlotId(excel.id);
    } else {
      setActiveSlotId(pipelineSlots[0]?.id ?? null);
    }
  }, [promptFocus, pipelineSlots, open, nodeKey]);

  useEffect(() => {
    if (!open || tab !== "excel") return;
    const sheets = xlsxSheetsMeta.data?.sheets ?? [];
    if (!sheets.length) return;
    setXlsxSheet((prev) => {
      if (prev && sheets.includes(prev)) return prev;
      return pickDefaultSheetForNode(nodeType, sheets);
    });
  }, [open, tab, nodeType, xlsxSheetsMeta.data?.sheets]);

  useEffect(() => {
    if (!open) {
      setXlsxSheet("");
      setXlsxFocusKeyRows(false);
    }
  }, [open, nodeKey]);

  const activeSlot =
    (activeSlotId === "gpt_text" ? gptTextSlotForNode(nodeType) : null) ??
    allSlots.find((s) => s.id === activeSlotId) ??
    (promptFocus?.kind === "text" ? promptFocus : null) ??
    pipelineSlots[0] ??
    null;

  const activeStepCode = slotStepCode(activeSlot, stepCode);
  const promptStepCode =
    isExcelGptNode(nodeType) && activeSlot?.kind === "gpt"
      ? EXCEL_GPT_STEP_CODE
      : activeStepCode;
  const promptPaths = promptPathsForNode(nodeType);
  const metaRecord = (project.data?.meta || {}) as Record<string, unknown>;
  const promptOverrides = (project.data?.prompt_overrides || {}) as Record<string, unknown>;
  const activeVariant =
    activeSlot && nodeKey
      ? isExcelGptNode(nodeType) && activeSlot.kind === "gpt"
        ? activeVariantForExcelGpt(
            metaRecord,
            nodeKey,
            activeSlot,
            promptOverrides,
            excelConfig.slotIndex,
            globalActivePrompts.data,
          )
        : activeVariantForSlot(
            metaRecord,
            nodeKey,
            activeSlot,
            promptOverrides,
            promptStepCode,
            globalActivePrompts.data,
          )
      : "default";
  const activeVariantSource =
    activeSlot && nodeKey
      ? isExcelGptNode(nodeType) && activeSlot.kind === "gpt"
        ? activeVariantSourceForExcelGpt(
            metaRecord,
            nodeKey,
            activeSlot,
            promptOverrides,
            excelConfig.slotIndex,
            globalActivePrompts.data,
          )
        : activeVariantSourceForSlot(
            metaRecord,
            nodeKey,
            activeSlot,
            promptOverrides,
            promptStepCode,
            globalActivePrompts.data,
          )
      : "default";
  const activeVariantSourceLabel = promptSourceLabel(activeVariantSource);
  const sdAgentFromKey = sceneAgentFromNodeKey(nodeKey);
  const preferredFile =
    preferredPromptFileName(activeSlot) ??
    (sdAgentFromKey ? `sd_${sdAgentFromKey}` : undefined);

  const activateVariant = useMutation({
    mutationFn: async (variant: string) => {
      if (!projectId || !promptStepCode || !nodeKey || !activeSlot) {
        return Promise.reject(new Error("no step"));
      }
      const meta = withSlotVariant(metaRecord, nodeKey, activeSlot.id, variant);
      // excel_gpt: SSoT — meta.prompt_slot_variants[nodeKey]. Не пишем в
      // prompt_overrides["excel_gpt"], иначе все ноды «Работа с GPT» шлют
      // один и тот же последний выбранный файл.
      if (isExcelGptNode(nodeType) && activeSlot.kind === "gpt") {
        await api.patchProject(projectId, { meta });
        return;
      }
      const prompt_overrides = {
        ...((project.data?.prompt_overrides || {}) as Record<string, unknown>),
        [promptStepCode]: variant,
      };
      await api.patchProject(projectId, { meta, prompt_overrides });
    },
    onSuccess: () => {
      toast.success("Активный промт обновлён");
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["prompt-global-active"] });
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const runStep = useMutation({
    mutationFn: (variables?: { mode?: "full" | "resume" }) =>
      api.runProjectStep(projectId!, stepCode!, {
        nodeKey: nodeKey ?? undefined,
        mode: variables?.mode ?? "full",
      }),
    onSuccess: (_, vars) => {
      const isResume = vars?.mode === "resume";
      toast.success(
        isResume
          ? `Доделка шага «${spec.label}» запущена`
          : `Шаг «${spec.label}» запущен начисто`,
      );
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["project-run", projectId] });
      void qc.refetchQueries({ queryKey: ["project-run", projectId] });
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const reloadXlsx = useMutation({
    mutationFn: () => api.reloadProjectXlsx(projectId!),
    onSuccess: () => {
      toast.success("Таблица перечитана из файла");
      qc.invalidateQueries({ queryKey: ["xlsx-preview", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-sheets", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-general-plan", projectId] });
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const uploadXlsx = useMutation({
    mutationFn: async (file: File) => {
      if (isExcelGptNode(nodeType) && nodeKey) {
        return api.uploadExcelGptFile(projectId!, nodeKey, file);
      }
      await api.uploadProjectXlsx(projectId!, file, {
        nodeKey: nodeKey ?? undefined,
      });
      return { fileName: file.name };
    },
    onSuccess: (res) => {
      const name =
        res && typeof res === "object" && "fileName" in res
          ? String((res as { fileName?: string }).fileName || "")
          : "";
      toast.success(
        isExcelGptNode(nodeType) && name
          ? `Excel подменён: ${name} (вход только этот файл)`
          : "Excel загружен",
      );
      qc.invalidateQueries({ queryKey: ["xlsx-preview", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-sheets", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-general-plan", projectId] });
      qc.invalidateQueries({ queryKey: ["v-menu-xlsx-preview", projectId] });
      qc.invalidateQueries({ queryKey: ["gpt-operator-resolve", projectId] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      void qc.refetchQueries({ queryKey: ["xlsx-preview", projectId] });
      void qc.refetchQueries({ queryKey: ["xlsx-sheets", projectId] });
      if (isExcelGptNode(nodeType) && nodeKey && name) {
        window.dispatchEvent(
          new CustomEvent("canvas-patch-node-data", {
            detail: {
              nodeKey,
              patch: {
                inputSource: "upload",
                uploadedFileName: name,
                takeFromEdges: false,
              },
            },
          }),
        );
      }
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const filteredArtifacts = useMemo(() => {
    const list = artifacts.data ?? [];
    if (nodeType.includes("image") || nodeType === "images") {
      return list.filter((a) => a.kind.includes("image") || a.kind.includes("scene"));
    }
    if (nodeType.includes("video") || nodeType === "videos") {
      return list.filter((a) => a.kind.includes("video"));
    }
    if (nodeType === "hero" || nodeType === "items") {
      return list.filter((a) => a.kind.includes("hero") || a.kind.includes("item"));
    }
    return list.slice(0, 12);
  }, [artifacts.data, nodeType]);

  const assets = useQuery({
    queryKey: ["project-assets", projectId],
    queryFn: () => api.listProjectAssets(projectId!),
    enabled: open && projectId != null,
  });

  const dbBrowser = useQuery({
    queryKey: ["db-graph-project", projectId],
    queryFn: () => api.dbGraph(projectId!),
    enabled: open && projectId != null,
  });

  const resultSnapshot = useMemo(() => {
    if (!projectId) return null;
    return resolveNodeResult(
      nodeType,
      {
        project: project.data,
        artifacts: artifacts.data ?? [],
        assets: assets.data ?? [],
        frames: (dbBrowser.data?.frames as unknown as FrameDTO[]) ?? [],
        mediaImages: [],
        mediaVideos: [],
      },
      undefined,
      nodeKey,
    );
  }, [
    nodeType,
    projectId,
    project.data,
    artifacts.data,
    assets.data,
    dbBrowser.data?.frames,
    nodeKey,
  ]);

  const showStepParams =
    projectId != null &&
    (nodeType === "plan" ||
      nodeType === "script" ||
      nodeType === "split" ||
      nodeType === "audio" ||
      nodeType === "assemble" ||
      nodeType === "images" ||
      nodeType === "videos");

  const showGptTextPanel = activeSlot?.kind === "text" && activeStepCode && projectId;
  const showFramePromptsPanel =
    activeSlot?.kind === "frame_prompts" && projectId != null;
  const showFilesPanel =
    activeSlot?.kind === "gpt" &&
    Boolean(promptStepCode) &&
    stepHasPromptVariants(promptStepCode);
  const [mounted, setMounted] = useState(false);
  const backdropGuardUntil = useRef(0);
  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (open) backdropGuardUntil.current = Date.now() + 500;
  }, [open]);

  const isThisNodeRunning = useMemo(() => {
    if (runStep.isPending) return true;
    const status = project.data?.status as string | undefined;
    const genActive = Boolean(project.data?.generation_active);
    if (!status) return false;
    if (status === "generating_images" && (nodeType === "images" || stepCode === "img")) return true;
    if (status === "generating_image_prompts" && (nodeType === "image_prompts" || stepCode === "img_pr")) return true;
    if (status === "generating_animation_prompts" && (nodeType === "animation_prompts" || stepCode === "anim_pr")) return true;
    if (status === "generating_videos" && (nodeType === "videos" || stepCode === "video")) return true;
    if (status === "generating_audio" && (nodeType === "audio" || stepCode === "audio")) return true;
    if (status === "generating_music" && (nodeType === "music" || stepCode === "music")) return true;
    if (status === "assembling" && (nodeType === "assemble" || stepCode === "assemble")) return true;
    if (status === "generating_hero" && (nodeType === "hero" || stepCode === "hero")) return true;
    if (status === "generating_items" && (nodeType === "items" || stepCode === "items")) return true;
    if (status === "planning" && (nodeType === "plan" || stepCode === "plan")) return true;
    if (status === "scripting" && (nodeType === "script" || stepCode === "script")) return true;
    if (status === "splitting" && (nodeType === "split" || stepCode === "split")) return true;
    if (status === "scene_designing" && (nodeType === "scene_design" || stepCode === "scene_d")) return true;
    if (status.startsWith("enriching_") && isExcelGptNode(nodeType)) return true;
    if (genActive && (status.includes(stepCode || "") || status.includes(nodeType))) return true;
    return false;
  }, [runStep.isPending, project.data?.status, project.data?.generation_active, nodeType, stepCode]);

  if (!nodeKey || !mounted || !open) return null;

  const closeNow = (e: SyntheticEvent) => {
    if (Date.now() < backdropGuardUntil.current) return;
    e.preventDefault();
    e.stopPropagation();
    onOpenChange(false);
  };

  return createPortal(
    <>
      <button
        type="button"
        aria-label="Закрыть студию"
        className="fixed inset-0 z-[90] bg-black/45 backdrop-blur-[2px]"
        onPointerDown={(e) => {
          e.preventDefault();
          e.stopPropagation();
        }}
        onMouseDown={(e) => {
          e.preventDefault();
          e.stopPropagation();
        }}
        onClick={closeNow}
      />
      <aside
        className="premium-sheet fixed right-0 top-0 z-[100] flex h-full w-[min(920px,92vw)] flex-col border-l border-white/10 shadow-2xl"
        role="dialog"
        aria-modal="true"
      >
        <div className="flex h-full flex-col">
          <header className="relative shrink-0 border-b border-white/10 bg-gradient-to-r from-amber-500/5 via-transparent to-violet-500/5 px-5 py-4">
            <button
              type="button"
              aria-label="Закрыть студию"
              title="Закрыть (Esc)"
              className="absolute right-3 top-3 z-[210] inline-flex h-9 w-9 cursor-pointer items-center justify-center rounded-md bg-background/90 text-foreground/90 ring-1 ring-white/10 transition hover:bg-destructive hover:text-destructive-foreground"
              onPointerDown={(e) => e.stopPropagation()}
              onMouseDown={(e) => e.stopPropagation()}
              onClickCapture={closeNow}
              onClick={closeNow}
            >
              <X className="h-5 w-5" />
            </button>
            <div className="flex items-start justify-between gap-4 pr-12">
              <div>
                <h2 className="flex items-center gap-2 text-lg font-semibold">
                  <span
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl shadow-inner"
                    style={{
                      background: `linear-gradient(135deg, hsl(${spec.accent} / 0.25), hsl(${spec.accent} / 0.08))`,
                      color: `hsl(${spec.accent})`,
                    }}
                  >
                    <NodeIcon className="h-4 w-4" />
                  </span>
                  {excelConfig.label?.trim() || spec.label}
                </h2>
                <p className="text-xs text-muted-foreground">{spec.description}</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Badge variant="muted" className="text-[10px]">
                    {formatNodeKeyLabel(nodeKey)}
                  </Badge>
                  {isExcelGptNode(nodeType) ? (
                    <>
                      <Badge variant="muted" className="text-[10px] text-violet-200/90">
                        {workModeLabel(excelConfig.workMode)}
                      </Badge>
                      <Badge variant="muted" className="text-[10px] text-emerald-200/90">
                        {excelGptAttachmentChipTitle(
                          excelConfig.inputSource ?? "project_xlsx",
                        )}
                      </Badge>
                    </>
                  ) : null}
                  {promptPaths.legacyDir && (
                    <Badge variant="muted" className="text-[9px] font-mono">
                      prompts/{promptPaths.legacyDir}
                    </Badge>
                  )}
                </div>
              </div>
              <div className="flex w-full flex-col gap-2 sm:flex-row sm:flex-wrap items-center">
                {stepCode && (
                  <>
                    <Button
                      size="sm"
                      onClick={() => runStep.mutate({ mode: "full" })}
                      disabled={!projectId || isThisNodeRunning || nodeDisabled}
                      className={cn(
                        "transition-all duration-200 gap-2 h-9 px-4 font-semibold text-xs text-white bg-gradient-to-r from-emerald-500 via-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 active:scale-[0.98] shadow-lg shadow-emerald-500/35 border border-emerald-300/40 rounded-xl backdrop-blur-md",
                        isThisNodeRunning &&
                          "border-emerald-400/80 bg-emerald-500/25 text-emerald-200 animate-pulse font-medium shadow-[0_0_20px_rgba(16,185,129,0.35)]",
                      )}
                      title={
                        nodeDisabled
                          ? "Нода отключена в графе"
                          : isThisNodeRunning
                            ? "Шаг сейчас выполняется..."
                            : "Запустить шаг начисто с 1-го кадра (полный перезапуск с очисткой)"
                      }
                    >
                      {isThisNodeRunning ? (
                        <>
                          <Loader2 className="h-3.5 w-3.5 animate-spin text-emerald-200" />
                          <span>В работе...</span>
                        </>
                      ) : (
                        <>
                          <Play className="h-3.5 w-3.5 fill-current" />
                          <span>Запустить шаг</span>
                        </>
                      )}
                    </Button>
                    {!isThisNodeRunning && !["plan", "script", "split", "assemble", "publish"].includes(nodeType) && (
                      <Button
                        size="sm"
                        onClick={() => runStep.mutate({ mode: "resume" })}
                        disabled={!projectId || isThisNodeRunning || nodeDisabled}
                        className="transition-all duration-200 gap-2 h-9 px-4 font-semibold text-xs text-amber-950 bg-gradient-to-r from-amber-400 via-amber-300 to-yellow-400 hover:from-amber-300 hover:to-yellow-300 active:scale-[0.98] shadow-lg shadow-amber-500/25 border border-amber-200/60 rounded-xl backdrop-blur-md"
                        title="Доделать только недостающие элементы (мягкое продолжение без удаления готовых)"
                      >
                        <Play className="h-3.5 w-3.5 text-amber-950 fill-current" />
                        <span>Продолжить / Доделать</span>
                      </Button>
                    )}
                  </>
                )}
              </div>
            </div>
            <div className="mt-3 flex flex-wrap gap-1">
              {(
                [
                  ["settings", "Настройки", Settings2],
                  ...(pipelineSlots.some((s) => s.kind !== "excel") ||
                  (nodeSupportsGptText(nodeType) && gptTextSlotForNode(nodeType))
                    ? ([["prompts", "Промпты", FileText]] as const)
                    : []),
                  ...(showExcel ? [["excel", "Excel", FileSpreadsheet] as const] : []),
                  ["results", "Результаты", FileText],
                ] as const
              ).map(([id, label, Icon]) => (
                <Button
                  key={id}
                  type="button"
                  size="sm"
                  variant="ghost"
                  className={cn(
                    "gap-1.5 text-xs font-semibold rounded-lg transition-all",
                    tab === id
                      ? "bg-zinc-800 text-zinc-100 border border-zinc-700 shadow-sm"
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50",
                  )}
                  onClick={() => {
                    setTab(id);
                    setActiveSlotId(pipelineSlots[0]?.id ?? null);
                  }}
                >
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </Button>
              ))}
            </div>
            {tab === "prompts" && (
              <div className="mt-3 flex flex-wrap gap-1 border-t border-white/5 pt-3">
                {pipelineSlots
                  .filter((slot) => slot.kind !== "excel")
                  .map((slot) => (
                    <Button
                      key={slot.id}
                      size="sm"
                      variant="ghost"
                      className={cn(
                        "h-7 text-xs font-medium rounded-lg transition-all",
                        activeSlotId === slot.id && !showGptTextPanel
                          ? "bg-zinc-800 text-white border border-zinc-700 shadow-sm"
                          : "border border-zinc-800 bg-zinc-900/60 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40",
                      )}
                      onClick={() => {
                        setActiveSlotId(slot.id);
                      }}
                    >
                      {slot.title}
                    </Button>
                  ))}
                {nodeSupportsGptText(nodeType) && gptTextSlotForNode(nodeType) && (
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className={cn(
                      "h-7 text-xs font-medium rounded-lg transition-all",
                      activeSlotId === "gpt_text"
                        ? "bg-violet-950/80 text-violet-200 border border-violet-500/50 shadow-sm"
                        : "border border-violet-900/40 bg-violet-950/20 text-violet-300 hover:text-violet-100 hover:bg-violet-900/30",
                    )}
                    onClick={() => {
                      setActiveSlotId("gpt_text");
                    }}
                    title="Сопроводительный (прилагаемый) текст в диалог GPT"
                  >
                    <MessageSquareText className="mr-1 h-3.5 w-3.5" />
                    Сопроводительный текст
                  </Button>
                )}
              </div>
            )}
          </header>

          {tab === "excel" && projectId ? (
            <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-5">
                  <div className="flex shrink-0 flex-wrap gap-2">
                    <Button size="sm" variant="outline" asChild>
                      <a
                        href={api.downloadProjectXlsx(projectId, {
                          nodeKey: nodeKey ?? undefined,
                        })}
                        download
                      >
                        <Download className="h-3.5 w-3.5" />
                        Скачать Excel
                      </a>
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => fileRef.current?.click()}
                      disabled={uploadXlsx.isPending}
                    >
                      <Upload className="h-3.5 w-3.5" />
                      Загрузить
                    </Button>
                    <input
                      ref={fileRef}
                      type="file"
                      accept=".xlsx"
                      className="hidden"
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) uploadXlsx.mutate(f);
                        e.target.value = "";
                      }}
                    />
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => reloadXlsx.mutate()}
                      disabled={reloadXlsx.isPending}
                    >
                      <RefreshCw className="h-3.5 w-3.5" />
                      Перечитать
                    </Button>
                  </div>
                  {(xlsxSheetsMeta.data?.sheets?.length ?? 0) > 0 && (
                    <div className="flex shrink-0 flex-wrap items-center gap-2">
                      <select
                        className="studio-select h-8 max-w-xs rounded-md border border-input bg-card px-2 text-xs"
                        value={xlsxSheet || pickDefaultSheetForNode(nodeType, xlsxSheetsMeta.data?.sheets ?? [])}
                        onChange={(e) => setXlsxSheet(e.target.value)}
                      >
                        {(xlsxSheetsMeta.data?.sheets ?? []).map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                      </select>
                      {focusAvailable ? (
                        <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground">
                          <input
                            type="checkbox"
                            className="rounded border-white/20"
                            checked={xlsxFocusKeyRows}
                            onChange={(e) => setXlsxFocusKeyRows(e.target.checked)}
                          />
                          Только ключевые строки
                        </label>
                      ) : null}
                    </div>
                  )}
                  {(xlsxSheetsMeta.isLoading || xlsxPreview.isLoading) && (
                    <div className="flex items-center justify-center py-12">
                      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                    </div>
                  )}
                  {!xlsxSheetsMeta.isLoading && !xlsxPreview.isLoading && (
                    <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-hidden">
                      {xlsxPreview.data?.xlsx_snapshot || xlsxSheetsMeta.data?.xlsx_snapshot ? (
                        <p className="shrink-0 text-[10px] text-muted-foreground">
                          Показан файл:{" "}
                          <span className="font-mono text-foreground/90">
                            {xlsxPreview.data?.xlsx_snapshot ||
                              xlsxSheetsMeta.data?.xlsx_snapshot}
                          </span>
                        </p>
                      ) : null}
                      {xlsxParams.hint && xlsxFocusKeyRows ? (
                        <p className="shrink-0 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-100/90">
                          {xlsxParams.hint}
                        </p>
                      ) : null}
                      {xlsxPreview.data?.truncated_rows || xlsxPreview.data?.truncated_cols ? (
                        <p className="shrink-0 rounded-lg border border-sky-500/20 bg-sky-500/10 px-3 py-2 text-[11px] text-sky-100/90">
                          Показана часть листа
                          {xlsxPreview.data.sheet_max_row
                            ? ` (в файле ~${xlsxPreview.data.sheet_max_row}×${xlsxPreview.data.sheet_max_col || "?"})`
                            : ""}
                          . Для полного файла — «Скачать Excel».
                        </p>
                      ) : null}
                      {(xlsxPreview.data?.rows?.length ?? 0) > 0 ? (
                        <StudioExcelGrid
                          className="min-h-0 flex-1"
                          rows={xlsxPreview.data?.rows ?? []}
                          startRow={xlsxPreview.data?.start_row ?? xlsxParams.startRow}
                          colLetters={xlsxPreview.data?.col_letters}
                        />
                      ) : (
                        <p className="rounded-xl border border-white/10 p-4 text-xs text-muted-foreground">
                          {nodeType === "plan"
                            ? "Лист пуст или Excel ещё не создан — запустите шаг или загрузите project.xlsx. Переключите лист в списке выше (например «план»)."
                            : "Таблица пуста или ещё не создана. Проверьте выбранный лист."}
                          {nodeType === "plan" && project.data?.general_plan?.trim() ? (
                            <span className="mt-2 block whitespace-pre-wrap text-foreground/90">
                              Текст плана в БД: {project.data.general_plan}
                            </span>
                          ) : null}
                        </p>
                      )}
                    </div>
                  )}
            </div>
          ) : (
          <ScrollArea className="flex-1">
            <div className="p-5">
              {tab === "settings" && (
                <div className="flex flex-col gap-4 text-sm text-muted-foreground">
                  {isExcelGptNode(nodeType) && projectId && nodeKey ? (
                    <ExcelGptSettingsPanel
                      projectId={projectId}
                      nodeKey={nodeKey}
                      config={excelConfig}
                      onConfigChange={patchExcelNodeData}
                    />
                  ) : null}
                  {showStepParams ? (
                    <NodeStepParamsPanel projectId={projectId!} nodeType={nodeType} />
                  ) : null}
                  {nodeType === "items" && projectId ? (
                    <div className="rounded-xl border border-cyan-400/20 bg-cyan-500/[0.05] p-3">
                      <ItemsConfigPanel projectId={projectId} />
                    </div>
                  ) : null}
                  {nodeType === "hero" && projectId ? (
                    <div className="rounded-xl border border-amber-400/20 bg-amber-500/[0.05] p-3">
                      <HeroConfigPanel projectId={projectId} />
                    </div>
                  ) : null}
                  {nodeDisabled && (
                    <p className="text-amber-400">Нода отключена в графе — шаг не запустится.</p>
                  )}
                </div>
              )}

              {tab === "prompts" && (
                <div className="flex flex-col gap-4">
                  {activeSlot && !showGptTextPanel && showFilesPanel && (
                    <p className="text-xs text-muted-foreground">
                      Редактируется:{" "}
                      <span className="font-medium text-foreground">{activeSlot.title}</span>
                    </p>
                  )}
                  {showGptTextPanel ? (
                    <GptTextPanel
                      key={`gpt-${activeSlot?.id}-${activeStepCode}`}
                      projectId={projectId}
                      stepCode={activeStepCode}
                    />
                  ) : showFramePromptsPanel ? (
                    <FramePromptsPanel
                      key={`frame-prompts-${projectId}`}
                      projectId={projectId}
                      field="image_prompt"
                    />
                  ) : showFilesPanel && isCheckNode ? (
                    <CheckNodePromptPanel
                      projectId={projectId!}
                      nodeKey={nodeKey!}
                      resolve={operatorResolve.data}
                      loading={operatorResolve.isLoading}
                    />
                  ) : showFilesPanel && promptStepCode ? (
                    <PromptFilesPanel
                      key={`files-${nodeKey}-${activeSlot?.id}-${promptStepCode}`}
                      stepCode={promptStepCode}
                      slotId={activeSlot?.id}
                      projectId={projectId ?? undefined}
                      preferredFile={preferredFile}
                      folderHint={
                        legacyPromptFolder(promptStepCode) ??
                        (activeSlot?.stepCode && activeSlot.stepCode !== stepCode
                          ? activeSlot.stepCode
                          : (promptPaths.legacyDir ?? promptStepCode))
                      }
                      activeVariant={activeVariant}
                      activeVariantSourceLabel={activeVariantSourceLabel}
                      onActivateVariant={(variant) => activateVariant.mutate(variant)}
                      activating={activateVariant.isPending}
                    />
                  ) : activeSlot?.kind === "excel" ? (
                    <div className="flex flex-col gap-3 text-sm text-muted-foreground">
                      <p>
                        Для этой ноды мастер-промт задаётся через{" "}
                        <span className="font-medium text-foreground">project.xlsx</span> — откройте
                        вкладку «Excel».
                      </p>
                      <Button
                        size="sm"
                        variant="outline"
                        className="w-fit"
                        onClick={() => setTab("excel")}
                      >
                        Открыть Excel
                      </Button>
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      Для этой ноды нет редактируемых промтов на этом шаге. Добавьте слот через «+
                      ещё» в меню V.
                    </p>
                  )}
                </div>
              )}


              {tab === "results" && (
                <div className="flex flex-col gap-4">
                  {resultSnapshot && (resultSnapshot.hasResult || resultSnapshot.items.length > 0) ? (
                    <NodeResultViewBody
                      projectId={projectId!}
                      nodeKey={nodeKey}
                      nodeType={nodeType}
                      snapshot={resultSnapshot}
                    />
                  ) : filteredArtifacts.length > 0 ? (
                    <div className="grid gap-3 sm:grid-cols-2">
                      {filteredArtifacts.map((a) => (
                        <div
                          key={a.id}
                          className="rounded-xl border border-white/10 bg-white/5 p-2"
                        >
                          <div className="text-[10px] uppercase text-muted-foreground">
                            {humanizeSlug(a.kind)}
                          </div>
                          {a.path.match(/\.(mp4|webm)$/i) ? (
                            <video
                              controls
                              className="mt-1 max-h-40 w-full rounded"
                              src={api.artifactFileUrl(a.uuid)}
                            />
                          ) : a.path.match(/\.(mp3|wav|m4a|ogg|aac|flac)$/i) ? (
                            <div className="mt-2 flex flex-col gap-1 rounded bg-black/40 p-2">
                              <audio
                                controls
                                className="w-full"
                                src={api.artifactFileUrl(a.uuid)}
                              />
                            </div>
                          ) : a.path.match(/\.(json|txt|tsv|md|csv)$/i) ? (
                            <div className="mt-1 flex max-h-40 flex-col overflow-auto rounded bg-black/40 p-2 text-[11px] font-mono text-zinc-300">
                              <div className="text-[9px] text-zinc-500">
                                {a.path.split(/[\\/]/).pop()}
                              </div>
                              <span className="mt-1 text-[10px] text-zinc-400">Текстовый артефакт (доступен для скачивания)</span>
                            </div>
                          ) : (
                            <img
                              alt=""
                              className="mt-1 max-h-40 w-full rounded object-contain"
                              src={api.artifactFileUrl(a.uuid)}
                            />
                          )}
                          <a
                            href={api.artifactFileUrl(a.uuid)}
                            download
                            className="mt-2 inline-flex items-center gap-1 text-[10px] text-primary hover:underline"
                          >
                            <Download className="h-3 w-3" />
                            Скачать
                          </a>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="flex flex-col items-center justify-center py-12 text-center text-muted-foreground">
                      <p className="text-sm">
                        {resultSnapshot?.summary || "Результаты ещё не сформированы. Запустите шаг для генерации."}
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          </ScrollArea>
          )}
        </div>
      </aside>
    </>,
    document.body,
  );
}
