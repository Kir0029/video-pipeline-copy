"use client";

import { useEffect, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Ban,
  Eye,
  FileSpreadsheet,
  FileText,
  MessageSquareText,
  Play,
  Plus,
  Trash2,
  Unlink,
  X,
} from "lucide-react";
import type { NodePromptSlot } from "@/lib/node-prompts";
import {
  gptTextSlotForNode,
  isCustomPromptSlot,
  resolvePromptSlots,
} from "@/lib/node-prompts";
import { nodeSupportsGptText } from "@/lib/gpt-text-steps";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { stepCodeForNodeType } from "@/lib/node-step-map";
import {
  attachmentLabel,
  excelGptAttachmentChipTitle,
  excelGptSlotIndex,
  isExcelGptNode,
  workModeChip,
  type ExcelGptInputSource,
  type ExcelGptWorkMode,
} from "@/lib/excel-gpt-config";
import { Button } from "@/components/ui/button";
import { GptOperatorMenuPanel } from "./gpt-operator-menu";

function openPromptSlot(
  slot: NodePromptSlot,
  onClose: () => void,
  onSelectPrompt: (slot: NodePromptSlot) => void,
) {
  onClose();
  // Два кадра: закрытие V-menu в portal + backdrop guard студии успевают сработать.
  window.setTimeout(() => onSelectPrompt(slot), 32);
}

export function NodeVMenu({
  open,
  anchorRef,
  nodeKey,
  nodeType,
  slots,
  disabled,
  projectId,
  inputSource,
  uploadedFileName,
  workMode,
  slotIndex,
  onSelectPrompt,
  onOpenGptText,
  onAddPrompt,
  onRemovePrompt,
  onViewAllPrompts,
  onDownloadPrompts,
  onRunNode,
  onOpenAssets,
  onDetachNode,
  onToggleDisable,
  onDeleteNode,
  onClose,
  hasAssets,
  canvasZoom = 1,
}: {
  open: boolean;
  anchorRef: RefObject<HTMLElement | null>;
  nodeKey: string;
  nodeType: string;
  slots: NodePromptSlot[];
  disabled: boolean;
  onSelectPrompt: (slot: NodePromptSlot) => void;
  onOpenGptText: () => void;
  onAddPrompt: () => void;
  onRemovePrompt: (slot: NodePromptSlot) => void;
  onViewAllPrompts: () => void;
  onDownloadPrompts: () => void;
  onRunNode: () => void;
  onOpenAssets?: () => void;
  onDetachNode: () => void;
  onToggleDisable: () => void;
  onDeleteNode: () => void;
  onClose: () => void;
  hasAssets: boolean;
  projectId?: number | null;
  inputSource?: ExcelGptInputSource;
  uploadedFileName?: string;
  workMode?: ExcelGptWorkMode;
  slotIndex?: number;
  canvasZoom?: number;
}) {
  const [mounted, setMounted] = useState(false);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const isGptOp = isExcelGptNode(nodeType);
  const menuWidth = Math.max(
    220,
    Math.min(isGptOp ? 360 : 340, (isGptOp ? 360 : 340) * Math.max(0.35, Math.min(canvasZoom, 1.5))),
  );

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!open || !anchorRef.current) return;
    const update = () => {
      const el = anchorRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const pad = 8;
      const estHeight = Math.min(window.innerHeight * 0.7, 520);
      let left = r.left + r.width / 2;
      let top = r.bottom + 8;
      // Не уезжать за правый/левый край экрана (меню центрируется translateX(-50%)).
      const half = menuWidth / 2;
      left = Math.min(window.innerWidth - pad - half, Math.max(pad + half, left));
      if (top + estHeight > window.innerHeight - pad) {
        top = Math.max(pad, r.top - estHeight - 8);
      }
      setPos({ top, left });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(anchorRef.current);
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    const tick = window.setInterval(update, 150);
    return () => {
      ro.disconnect();
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
      window.clearInterval(tick);
    };
  }, [open, anchorRef, menuWidth]);

  useEffect(() => {
    if (!open) return;
    const close = (ev: Event) => {
      const t = ev.target as HTMLElement;
      if (t.closest(".node-v-menu") || t.closest(".node-v-trigger")) return;
      onClose();
    };
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", close, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", close, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open, onClose]);

  const stepCode = stepCodeForNodeType(nodeType) ?? nodeType;
  const outbound = useQuery({
    queryKey: ["step-attachments", projectId, stepCode, nodeKey],
    queryFn: () => api.getStepAttachments(projectId!, stepCode, nodeKey),
    enabled: open && projectId != null,
  });
  const outboundFiles = outbound.data?.files ?? [];
  const source: ExcelGptInputSource = inputSource ?? "project_xlsx";
  const slotIdx = isExcelGptNode(nodeType) ? excelGptSlotIndex(nodeKey, slotIndex) : undefined;
  const excelAttachmentName = isExcelGptNode(nodeType)
    ? attachmentLabel(source, uploadedFileName)
    : "project.xlsx";
  const excelAttachmentTitle = isExcelGptNode(nodeType)
    ? excelGptAttachmentChipTitle(source)
    : "Excel";

  if (!open || !mounted) return null;

  const zoom = Math.max(0.35, Math.min(canvasZoom, 1.5));

  const menuSlots = resolvePromptSlots(nodeType, slots, nodeKey, slotIndex).filter(
    (s) => s.kind !== "blocks",
  );
  const excelSlot = menuSlots.find((s) => s.kind === "excel");
  const gptTextSlot = gptTextSlotForNode(nodeType);
  const showGptText = nodeSupportsGptText(nodeType) && gptTextSlot;

  const menu = (
    <div
      className="node-v-menu nodrag nopan nowheel fixed z-[10000] animate-in fade-in slide-in-from-top-2 duration-200"
      style={{
        top: pos.top,
        left: pos.left,
        width: menuWidth,
        maxHeight: isGptOp ? "min(80vh, 720px)" : "min(70vh, 560px)",
        overflowY: "auto",
        overflowX: "hidden",
        transform: `translateX(-50%) scale(${zoom})`,
        transformOrigin: "top center",
      }}
      onPointerDown={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="rounded-2xl border border-white/12 bg-gradient-to-b from-[hsl(240_8%_9%/0.98)] to-[hsl(240_10%_5%/0.99)] p-3 shadow-2xl shadow-black/60 backdrop-blur-xl">
        <div className="sticky top-0 z-10 mb-2 flex items-center justify-between gap-2 rounded-lg bg-[hsl(240_8%_9%/0.95)] pb-1">
          <span className="text-[10px] font-semibold uppercase tracking-widest text-sky-300/90">
            {isGptOp ? "ИИ-редактор сцен" : "Меню ноды"}
          </span>
          <button
            type="button"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-white/15 text-muted-foreground hover:bg-white/10 hover:text-foreground"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              e.preventDefault();
              onClose();
            }}
            title="Закрыть (Esc / клик снаружи)"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        {isGptOp ? (
          <div className="mb-2 flex flex-wrap gap-1.5">
            <span className="rounded-md border border-violet-400/25 bg-violet-500/10 px-1.5 py-0.5 text-[9px] font-medium text-violet-100/90">
              {workModeChip(workMode)}
            </span>
            <span className="rounded-md border border-emerald-400/20 bg-emerald-500/10 px-1.5 py-0.5 text-[9px] font-medium text-emerald-100/90">
              {excelAttachmentTitle}: {excelAttachmentName}
            </span>
          </div>
        ) : null}

        {/* Сопроводительный (= прилагаемый) текст — СВЕРХУ, до пульта.
            Раньше кнопка была внизу и пропадала за длинным пультом оператора. */}
        {showGptText ? (
          <div className="mb-3">
            <button
              type="button"
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation();
                e.preventDefault();
                onOpenGptText();
              }}
              className="flex w-full items-start gap-2 rounded-xl border border-violet-400/40 bg-violet-500/15 px-3 py-2.5 text-left transition hover:border-violet-400/70 hover:bg-violet-500/25"
            >
              <MessageSquareText className="mt-0.5 h-4 w-4 shrink-0 text-violet-300" />
              <span className="min-w-0">
                <span className="block text-[11px] font-semibold text-violet-50">
                  Сопроводительный текст
                </span>
                <span className="mt-0.5 block text-[9px] leading-snug text-muted-foreground">
                  Прилагаемое сообщение в диалог GPT — отдельно от файла мастер-промта
                </span>
              </span>
            </button>
          </div>
        ) : null}

        {/* Пульт оператора — сразу под сопроводительным текстом */}
        {isGptOp && projectId != null ? (
          <div className="mb-3 rounded-xl border border-sky-400/30 bg-sky-500/[0.08] p-2">
            <p className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-widest text-sky-300">
              Пульт оператора — роли и файлы
            </p>
            <GptOperatorMenuPanel
              projectId={projectId}
              nodeKey={nodeKey}
              onOpenPrompts={() => {
                if (showGptText) onOpenGptText();
                else onViewAllPrompts();
              }}
            />
          </div>
        ) : null}

        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-widest text-amber-400/90">
            Мастер-промты
          </span>
          <span className="text-[9px] text-muted-foreground">{menuSlots.length} шт.</span>
        </div>

        {menuSlots.length > 0 ? (
          <div className="mb-3 overflow-x-auto pb-1">
            <div className="flex min-w-min items-center gap-1">
              {menuSlots.map((slot, i) => (
                <div key={slot.id} className="relative flex items-center gap-1">
                  <button
                    type="button"
                    onMouseDown={(e) => e.stopPropagation()}
                    onClick={(e) => {
                      e.stopPropagation();
                      openPromptSlot(slot, onClose, onSelectPrompt);
                    }}
                    className={cn(
                      "flex w-[88px] shrink-0 flex-col items-center rounded-xl border px-2 py-2 text-center transition-all",
                      slot.kind === "excel"
                        ? "border-emerald-500/40 bg-emerald-500/10 hover:border-emerald-400/60 hover:bg-emerald-500/20"
                        : "border-white/10 bg-white/[0.04] hover:border-amber-400/40 hover:bg-amber-400/10",
                    )}
                    title={slot.description || slot.title}
                  >
                    {slot.kind === "excel" ? (
                      isExcelGptNode(nodeType) && source === "voiceover" ? (
                        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-sky-500/25 text-sky-300">
                          <FileText className="h-3.5 w-3.5" />
                        </span>
                      ) : (
                        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-500/25 text-emerald-300">
                          <FileSpreadsheet className="h-3.5 w-3.5" />
                        </span>
                      )
                    ) : (
                      <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary/20 text-[10px] font-bold text-primary">
                        {(excelSlot ? 1 : 0) +
                          menuSlots.slice(0, i).filter((s) => s.kind !== "excel").length +
                          1}
                      </span>
                    )}
                    <span className="mt-1 line-clamp-2 text-[9px] font-medium leading-tight">
                      {slot.kind === "excel"
                        ? isExcelGptNode(nodeType)
                          ? excelAttachmentTitle
                          : "Excel"
                        : slot.title}
                    </span>
                    <span
                      className={cn(
                        "mt-0.5 text-[8px]",
                        slot.kind === "excel" ? "text-emerald-400/90" : "text-muted-foreground",
                      )}
                    >
                      {slot.kind === "excel"
                        ? isExcelGptNode(nodeType)
                          ? excelAttachmentName
                          : "project.xlsx"
                        : slotKindLabel(slot.kind)}
                    </span>
                  </button>
                  {isCustomPromptSlot(slot) && slot.kind !== "excel" && (
                    <button
                      type="button"
                      className="absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full border border-white/20 bg-destructive/90 text-white shadow"
                      title="Удалить промт"
                      onClick={(e) => {
                        e.stopPropagation();
                        onRemovePrompt(slot);
                      }}
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  )}
                  {i < menuSlots.length - 1 && (
                    <ArrowRight className="h-3.5 w-3.5 shrink-0 text-amber-500/40" aria-hidden />
                  )}
                </div>
              ))}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onAddPrompt();
                }}
                className="flex h-[72px] w-[52px] shrink-0 flex-col items-center justify-center rounded-xl border border-dashed border-white/15 text-muted-foreground transition-colors hover:border-primary/40 hover:text-primary"
                title="Добавить промт"
              >
                <Plus className="h-4 w-4" />
                <span className="mt-1 text-[8px]">ещё</span>
              </button>
            </div>
          </div>
        ) : (
          <p className="mb-3 text-[10px] text-muted-foreground">
            Для этой ноды нет файловых мастер-промтов.
          </p>
        )}

        {outboundFiles.length > 0 && !isExcelGptNode(nodeType) && (
          <div className="mb-3 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2">
            <span className="text-[9px] font-semibold uppercase tracking-widest text-muted-foreground">
              Отправляемые файлы
            </span>
            <ul className="mt-1.5 space-y-0.5">
              {outboundFiles.map((f) => (
                <li key={f} className="font-mono text-[10px] text-foreground/90">
                  {f}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="grid grid-cols-2 gap-1 border-t border-white/8 pt-2">
          <MenuAction icon={Play} label="Запустить шаг" onClick={onRunNode} />
          {hasAssets && onOpenAssets && (
            <MenuAction icon={Eye} label="Файлы и превью" onClick={onOpenAssets} />
          )}
          {excelSlot != null && projectId != null && (
            <MenuAction
              icon={FileSpreadsheet}
              label="Просмотр Excel"
              onClick={() => openPromptSlot(excelSlot, onClose, onSelectPrompt)}
            />
          )}
          <MenuAction icon={Unlink} label="Открепить связи" onClick={onDetachNode} />
          <MenuAction
            icon={Ban}
            label={disabled ? "Включить ноду" : "Отключить ноду"}
            onClick={onToggleDisable}
          />
          <MenuAction
            icon={Trash2}
            label="Удалить ноду"
            onClick={onDeleteNode}
            destructive
          />
        </div>
      </div>
      <div
        className="absolute -top-1.5 left-1/2 h-3 w-3 -translate-x-1/2 rotate-45 border-l border-t border-white/12 bg-[hsl(240_8%_9%)]"
        aria-hidden
      />
    </div>
  );

  return createPortal(menu, document.body);
}

function MenuAction({
  icon: Icon,
  label,
  onClick,
  destructive,
}: {
  icon: typeof Eye;
  label: string;
  onClick: () => void;
  destructive?: boolean;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className={cn(
        "h-8 justify-start gap-1.5 px-2 text-[10px] font-normal",
        destructive && "text-destructive hover:text-destructive",
      )}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
    >
      <Icon className="h-3 w-3 shrink-0" />
      {label}
    </Button>
  );
}

function slotKindLabel(kind: NodePromptSlot["kind"]): string {
  if (kind === "gpt") return "файл .md";
  if (kind === "text") return "текст GPT";
  if (kind === "excel") return "project.xlsx";
  if (kind === "blocks") return "outsee";
  if (kind === "frame_prompts") return "кадры";
  return kind;
}
