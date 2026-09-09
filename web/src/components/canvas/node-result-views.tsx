"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  FileSpreadsheet,
  FileText,
  Loader2,
  Maximize2,
  Replace,
  Save,
  Upload,
} from "lucide-react";
import { toast } from "sonner";
import { errorMessageFromUnknown } from "@/lib/error-message";
import { api } from "@/lib/api";
import type { NodeResultItem, NodeResultSnapshot } from "@/lib/node-result-resolver";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { MediaFrameGallery } from "@/components/hitl/media-frame-gallery";
import { TopicEditor } from "@/components/inspector/topic-editor";
import {
  pickGeneralPlanSheet,
  ROW_VOICEOVER_V8,
  SHEET_PLAN_V8,
} from "@/lib/xlsx-sheets";

export function NodeResultViewBody({
  projectId,
  nodeKey,
  nodeType,
  snapshot,
  onHeroReplaced,
}: {
  projectId: number;
  nodeKey?: string | null;
  nodeType: string;
  snapshot: NodeResultSnapshot;
  onHeroReplaced?: () => void;
}) {
  switch (snapshot.viewMode) {
    case "xlsx_general_plan":
      return (
        <GeneralPlanSheetView
          projectId={projectId}
          nodeKey={nodeKey}
          nodeType={nodeType}
          snapshot={snapshot}
        />
      );
    case "voiceover_wide":
      return <VoiceoverWideView projectId={projectId} snapshot={snapshot} />;
    case "xlsx_split_row":
      return (
        <SplitRowView
          projectId={projectId}
          nodeKey={nodeKey}
          nodeType={nodeType}
        />
      );    case "frame_prompts":
      return <FramePromptsView items={snapshot.items} />;
    case "frame_images":
      return (
        <FrameImagesView
          projectId={projectId}
          nodeType={nodeType}
          items={snapshot.items}
          onHeroReplaced={onHeroReplaced}
        />
      );
    case "frame_videos":
      return <FrameVideosView items={snapshot.items} />;
    case "topic_edit":
      return <TopicEditView projectId={projectId} snapshot={snapshot} />;
    default:
      if (
        nodeType === "excel_gpt" ||
        Boolean(nodeType?.startsWith("enrich_"))
      ) {
        return (
          <ExcelGptResultView
            projectId={projectId}
            nodeKey={nodeKey}
            nodeType={nodeType}
            snapshot={snapshot}
          />
        );
      }
      return (
        <DefaultResultView
          projectId={projectId}
          nodeKey={nodeKey}
          snapshot={snapshot}
        />
      );
  }
}

function LoadingBlock() {
  return (
    <div className="flex items-center justify-center py-12">
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
    </div>
  );
}

function isSplitRowLabel(text: string): boolean {
  const t = text.trim().toLowerCase();
  if (!t) return true;
  return (
    t === "закадровый текст" ||
    t === "закадровый" ||
    t === "voiceover" ||
    (t.includes("закадров") && t.length < 48)
  );
}

function XlsxUploadBar({
  projectId,
  nodeKey,
  nodeType,
}: {
  projectId: number;
  nodeKey?: string | null;
  nodeType?: string | null;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const isExcelGpt = nodeType === "excel_gpt" || Boolean(nodeType?.startsWith("enrich_"));
  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (isExcelGpt && nodeKey) {
        return api.uploadExcelGptFile(projectId, nodeKey, file);
      }
      await api.uploadProjectXlsx(projectId, file, {
        nodeKey: nodeKey ?? undefined,
      });
      return { fileName: file.name, replacedXlsx: true as const };
    },
    onSuccess: (res) => {
      const name =
        res && typeof res === "object" && "fileName" in res
          ? String((res as { fileName?: string }).fileName || "")
          : "";
      toast.success(
        isExcelGpt && name ? `Excel подменён: ${name}` : "Excel загружен",
      );
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-preview", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-general-plan", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-split-row", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-sheets", projectId] });
      qc.invalidateQueries({ queryKey: ["v-menu-xlsx-preview", projectId] });
      qc.invalidateQueries({ queryKey: ["gpt-operator-resolve", projectId] });
      if (isExcelGpt && nodeKey && name) {
        window.dispatchEvent(
          new CustomEvent("canvas-patch-node-data", {
            detail: {
              nodeKey,
              patch: { inputSource: "upload", uploadedFileName: name },
            },
          }),
        );
      }
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  return (
    <div className="mb-2 flex flex-wrap gap-2">
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
      <Button size="sm" variant="outline" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
        {upload.isPending ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Upload className="h-3.5 w-3.5" />
        )}
        Загрузить
      </Button>
      <input
        ref={fileRef}
        type="file"
        accept=".xlsx,.xls"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) upload.mutate(f);
          e.target.value = "";
        }}
      />
    </div>
  );
}

function GeneralPlanSheetView({
  projectId,
  nodeKey,
  nodeType,
  snapshot,
}: {
  projectId: number;
  nodeKey?: string | null;
  nodeType?: string | null;
  snapshot?: NodeResultSnapshot;
}) {
  const qc = useQueryClient();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });
  const meta = useQuery({
    queryKey: ["xlsx-sheets", projectId, nodeKey ?? "live"],
    queryFn: () =>
      api.previewProjectXlsx(projectId, {
        maxRows: 1,
        nodeKey: nodeKey ?? undefined,
      }),
  });
  const sheet = pickGeneralPlanSheet(meta.data?.sheets ?? []);
  const grid = useQuery({
    queryKey: ["xlsx-general-plan", projectId, nodeKey ?? "live", sheet],
    queryFn: () =>
      api.previewProjectXlsx(projectId, {
        sheet,
        raw: true,
        maxRows: 200,
        maxCols: 30,
        nodeKey: nodeKey ?? undefined,
      }),
    enabled: Boolean(sheet),
  });

  const rawPlanText =
    project.data?.general_plan?.trim() ||
    snapshot?.items.find((i) => i.kind === "text")?.content?.trim() ||
    "";
  const [text, setText] = useState(rawPlanText);
  const [dirty, setDirty] = useState(false);
  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState<"text" | "table">("text");

  useEffect(() => {
    if (!dirty && rawPlanText) {
      setText(rawPlanText);
    }
  }, [rawPlanText, dirty]);

  const save = useMutation({
    mutationFn: (body: string) =>
      api.patchProject(projectId, { general_plan: body }),
    onSuccess: (updated) => {
      const saved = (updated.general_plan ?? text).trim();
      setText(saved);
      setDirty(false);
      qc.setQueryData(["project", projectId], updated);
      qc.invalidateQueries({ queryKey: ["xlsx-general-plan"] });
      toast.success("Сценарий сохранён");
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const handleCopy = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success("Сценарий скопирован в буфер обмена");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Не удалось скопировать текст");
    }
  };

  if (project.isLoading && !rawPlanText) return <LoadingBlock />;

  const charCount = text.length;
  const wordCount = text.trim() ? text.trim().split(/\s+/).length : 0;
  const snapLabel = grid.data?.xlsx_snapshot || meta.data?.xlsx_snapshot;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/10 pb-2">
        <div className="flex items-center gap-1.5">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className={cn(
              "h-8 gap-1.5 rounded-lg px-3 text-xs font-medium transition-all",
              activeTab === "text"
                ? "bg-zinc-800 text-zinc-100 border border-zinc-700 shadow-sm"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50",
            )}
            onClick={() => setActiveTab("text")}
          >
            <FileText className="h-3.5 w-3.5 text-teal-400" />
            Сценарий (текст)
            {charCount > 0 && (
              <span className="ml-1 rounded bg-zinc-700/60 px-1.5 py-0.5 text-[10px] text-zinc-300">
                {charCount.toLocaleString("ru-RU")} симв.
              </span>
            )}
          </Button>

          <Button
            type="button"
            size="sm"
            variant="ghost"
            className={cn(
              "h-8 gap-1.5 rounded-lg px-3 text-xs font-medium transition-all",
              activeTab === "table"
                ? "bg-zinc-800 text-zinc-100 border border-zinc-700 shadow-sm"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50",
            )}
            onClick={() => setActiveTab("table")}
          >
            <FileSpreadsheet className="h-3.5 w-3.5 text-emerald-400" />
            Таблица Excel
            {sheet && (
              <span className="ml-1 rounded bg-zinc-700/60 px-1.5 py-0.5 text-[10px] text-zinc-300">
                {sheet}
              </span>
            )}
          </Button>
        </div>

        {activeTab === "text" && text && (
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-zinc-400">
              {wordCount} слов
            </span>
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs gap-1"
              onClick={handleCopy}
            >
              {copied ? (
                <Check className="h-3 w-3 text-emerald-400" />
              ) : (
                <Copy className="h-3 w-3" />
              )}
              {copied ? "Скопировано" : "Копировать"}
            </Button>
            {dirty && (
              <Button
                size="sm"
                className="h-7 text-xs gap-1 bg-teal-600 hover:bg-teal-500 text-white"
                disabled={save.isPending}
                onClick={() => save.mutate(text)}
              >
                {save.isPending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Replace className="h-3 w-3" />
                )}
                Сохранить
              </Button>
            )}
          </div>
        )}
      </div>

      {activeTab === "text" ? (
        text ? (
          <div className="flex min-h-0 flex-1 flex-col gap-2">
            <Textarea
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                setDirty(true);
              }}
              className="min-h-[420px] flex-1 resize-none rounded-lg border border-white/10 bg-black/40 p-4 font-mono text-xs leading-relaxed text-zinc-200 focus-visible:ring-1 focus-visible:ring-teal-500"
              placeholder="Сценарий ролика..."
            />
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-16 text-center text-zinc-400">
            <FileText className="mb-2 h-8 w-8 text-zinc-600" />
            <p className="text-sm font-medium">Сценарий ещё не сгенерирован</p>
            <p className="text-xs text-zinc-500 mt-1">
              Запустите ноду «Сценарий» для создания плана ролика
            </p>
          </div>
        )
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <XlsxUploadBar projectId={projectId} nodeKey={nodeKey} nodeType={nodeType} />
          {snapLabel ? (
            <p className="mb-1 text-[10px] text-muted-foreground">
              Снимок ноды: {snapLabel}
            </p>
          ) : null}
          {grid.isLoading ? (
            <LoadingBlock />
          ) : !sheet || !grid.data?.rows?.length ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              Лист «Общий план» пуст или Excel ещё не загружен.
            </p>
          ) : (
            <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-white/10 bg-black/20">
              <table className="min-w-max border-collapse text-left text-xs">
                <tbody>
                  {grid.data.rows.map((row, ri) => (
                    <tr key={ri} className="border-b border-white/5 hover:bg-white/[0.02]">
                      <td className="sticky left-0 z-10 border-r border-white/10 bg-card/95 px-2 py-1.5 text-[10px] text-muted-foreground">
                        {ri + 1}
                      </td>
                      {row.map((cell, ci) => (
                        <td
                          key={ci}
                          className="max-w-[320px] min-w-[80px] whitespace-pre-wrap border-r border-white/5 px-2 py-1.5 align-top"
                        >
                          {cell || "\u00a0"}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function VoiceoverWideView({
  projectId,
  snapshot,
}: {
  projectId: number;
  snapshot: NodeResultSnapshot;
}) {
  const qc = useQueryClient();
  const textItem = snapshot.items.find((i) => i.kind === "text");
  const fileItem = snapshot.items.find((i) => i.downloadUrl);
  const [text, setText] = useState(textItem?.content ?? "");
  const [dirty, setDirty] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const fileText = useQuery({
    queryKey: ["voiceover-file", fileItem?.downloadUrl],
    queryFn: async () => {
      const res = await fetch(fileItem!.downloadUrl!);
      if (!res.ok) throw new Error(await res.text());
      return res.text();
    },
    enabled: Boolean(fileItem?.downloadUrl) && !textItem?.content && !dirty,
  });

  useEffect(() => {
    if (dirty) return;
    if (textItem?.content) setText(textItem.content);
    else if (fileText.data) setText(fileText.data);
  }, [textItem?.content, fileText.data, dirty]);

  const save = useMutation({
    mutationFn: (body: string) => api.patchProject(projectId, { script_text: body }),
    onSuccess: (updated) => {
      const saved = (updated.script_text ?? text).trim();
      setText(saved);
      setDirty(false);
      qc.setQueryData(["project", projectId], updated);
      toast.success("Текст сохранён");
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success("Закадровый текст скопирован в буфер");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Не удалось скопировать текст");
    }
  };

  if (fileText.isLoading && !text) return <LoadingBlock />;
  if (fileText.isError && !text) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        Не удалось загрузить voiceover.txt. Откройте проект заново или сохраните текст вручную.
      </p>
    );
  }

  const charCount = text.length;
  const wordCount = text.trim() ? text.trim().split(/\s+/).length : 0;
  const estSeconds = Math.round(charCount / 14);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-white/10 pb-2">
        <div className="flex flex-wrap items-center gap-2">
          {fileItem?.downloadUrl && (
            <Button size="sm" variant="outline" className="h-8 gap-1.5 text-xs" asChild>
              <a href={fileItem.downloadUrl} download target="_blank" rel="noreferrer">
                <Download className="h-3.5 w-3.5" />
                Скачать voiceover.txt
              </a>
            </Button>
          )}
          <Button
            size="sm"
            className="h-8 gap-1.5 text-xs bg-teal-600 hover:bg-teal-500 text-white shadow-sm"
            disabled={save.isPending}
            onClick={() => save.mutate(text)}
          >
            {save.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Save className="h-3.5 w-3.5" />
            )}
            Сохранить текст
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 text-xs"
            onClick={handleCopy}
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-emerald-400" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
            {copied ? "Скопировано" : "Копировать"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 text-xs"
            onClick={() => fileRef.current?.click()}
          >
            <Upload className="h-3.5 w-3.5" />
            Загрузить файл
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept=".txt,text/plain"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              const reader = new FileReader();
              reader.onload = () => {
                const body = String(reader.result ?? "");
                setText(body);
                save.mutate(body);
              };
              reader.readAsText(f, "utf-8");
              e.target.value = "";
            }}
          />
        </div>

        {charCount > 0 && (
          <div className="flex items-center gap-2 text-xs text-zinc-400">
            <span className="rounded bg-zinc-800 px-2 py-0.5 text-[11px] font-medium text-teal-300 border border-zinc-700/60">
              {charCount.toLocaleString("ru-RU")} симв.
            </span>
            <span className="text-[11px] text-zinc-400">
              {wordCount} слов
            </span>
            <span className="text-[11px] text-zinc-500">
              ≈ {estSeconds} сек (14 зн/сек)
            </span>
          </div>
        )}
      </div>

      <Textarea
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setDirty(true);
        }}
        className="min-h-[55vh] flex-1 resize-none rounded-lg border border-white/10 bg-black/40 p-4 font-mono text-xs leading-relaxed text-zinc-200 focus-visible:ring-1 focus-visible:ring-teal-500"
        placeholder="Закадровый текст…"
      />
    </div>
  );
}

function SplitRowView({
  projectId,
  nodeKey,
  nodeType,
}: {
  projectId: number;
  nodeKey?: string | null;
  nodeType?: string | null;
}) {
  // DB SoT: после replace_frames UI должен показывать кадры из БД, не stale xlsx/snapshot.
  const framesQ = useQuery({
    queryKey: ["frames", projectId],
    queryFn: () => api.listFrames(projectId),
    refetchInterval: 8000,
  });
  const row = useQuery({
    queryKey: ["xlsx-split-row", projectId, nodeKey ?? "live"],
    queryFn: () =>
      api.previewProjectXlsx(projectId, {
        sheet: SHEET_PLAN_V8,
        row: ROW_VOICEOVER_V8,
        maxCols: 300,
        nodeKey: nodeKey ?? undefined,
      }),
    // xlsx — fallback, если кадров в DB ещё нет
    enabled: (framesQ.data?.length ?? 0) < 2,
  });

  if (framesQ.isLoading) return <LoadingBlock />;

  const dbFrames = (framesQ.data ?? [])
    .slice()
    .sort((a, b) => a.number - b.number)
    .filter((f) => (f.voiceover_text || "").trim());

  if (dbFrames.length >= 2) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <XlsxUploadBar projectId={projectId} nodeKey={nodeKey} nodeType={nodeType} />
        <div className="min-h-0 flex-1 overflow-x-auto overflow-y-hidden rounded-lg border border-white/10 bg-black/20 p-3">
          <p className="mb-2 text-[11px] text-muted-foreground">
            База · {dbFrames.length} кадров (закадр) — прокрутите вправо
          </p>
          <div className="flex min-w-max gap-2">
            {dbFrames.map((fr) => (
              <div
                key={fr.id}
                className="flex w-[220px] shrink-0 flex-col rounded-lg border border-white/10 bg-black/30 p-2"
              >
                <span className="mb-1 text-[10px] font-medium text-primary">
                  Кадр {fr.number}
                </span>
                <p className="max-h-40 overflow-auto whitespace-pre-wrap text-xs leading-relaxed text-foreground/90">
                  {(fr.voiceover_text || "").trim()}
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (row.isLoading) return <LoadingBlock />;

  const cells = row.data?.cells ?? [];
  const frameCells = cells
    .map((cell, colIndex) => ({ cell, colIndex }))
    .filter(({ cell }) => cell.trim() && !isSplitRowLabel(cell));

  if (!frameCells.length) {
    return (
      <div className="flex flex-col gap-3">
        <XlsxUploadBar projectId={projectId} nodeKey={nodeKey} nodeType={nodeType} />
        <p className="py-8 text-center text-sm text-muted-foreground">
          Строка {ROW_VOICEOVER_V8} листа «{SHEET_PLAN_V8}» пока пуста — сначала выполните разбивку.
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <XlsxUploadBar projectId={projectId} nodeKey={nodeKey} nodeType={nodeType} />
      {row.data?.xlsx_snapshot ? (
        <p className="mb-1 text-[10px] text-muted-foreground">
          Снимок ноды: {row.data.xlsx_snapshot}
        </p>
      ) : null}
      <div className="min-h-0 flex-1 overflow-x-auto overflow-y-hidden rounded-lg border border-white/10 bg-black/20 p-3">
      <p className="mb-2 text-[11px] text-muted-foreground">
        Лист «{SHEET_PLAN_V8}», строка {ROW_VOICEOVER_V8} — прокрутите вправо для всех кадров
      </p>
      <div className="flex min-w-max gap-2">
        {frameCells.map(({ cell, colIndex }, frameIdx) => (
          <div
            key={colIndex}
            className="flex w-[220px] shrink-0 flex-col rounded-lg border border-white/10 bg-black/30 p-2"
          >
            <span className="mb-1 text-[10px] font-medium text-primary">
              Кадр {frameIdx + 1}
              <span className="ml-1 font-normal text-muted-foreground">· кол. {colIndex + 1}</span>
            </span>
            <p className="max-h-40 overflow-auto whitespace-pre-wrap text-xs leading-relaxed text-foreground/90">
              {cell.trim()}
            </p>
          </div>
        ))}
      </div>
      </div>
    </div>
  );
}

export function FramePromptsView({ items }: { items: NodeResultItem[] }) {
  const [selectedId, setSelectedId] = useState(items[0]?.id ?? "");
  const selected = items.find((i) => i.id === selectedId) ?? items[0];

  useEffect(() => {
    if (items[0]?.id) setSelectedId(items[0].id);
  }, [items]);

  return (
    <div className="grid min-h-0 flex-1 gap-3 md:grid-cols-[200px_1fr] md:items-stretch">
      <ScrollArea className="h-[65vh] rounded-lg border border-white/10">
        <div className="p-1">
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setSelectedId(item.id)}
              className={cn(
                "mb-1 w-full rounded-md px-2 py-2 text-left text-[11px] transition",
                selected?.id === item.id
                  ? "bg-primary/20 text-primary"
                  : "text-muted-foreground hover:bg-white/5",
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      </ScrollArea>
      <Textarea
        readOnly
        value={selected?.content ?? ""}
        className="h-[65vh] resize-none text-xs leading-relaxed"
      />
    </div>
  );
}

function FrameImagesView({
  projectId,
  nodeType,
  items,
  onHeroReplaced,
}: {
  projectId: number;
  nodeType: string;
  items: NodeResultItem[];
  onHeroReplaced?: () => void;
}) {
  if (nodeType === "images") {
    return <SceneImagesGalleryView projectId={projectId} />;
  }

  const [index, setIndex] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const isHero = nodeType === "hero" || nodeType === "hitl_hero";
  const current = items[index] ?? items[0];

  const replaceHero = useMutation({
    mutationFn: (file: File) =>
      api.replaceHeroImage(projectId, file, current?.filePath ?? undefined),
    onSuccess: () => {
      toast.success("Персонаж заменён");
      qc.invalidateQueries({ queryKey: ["project-assets", projectId] });
      onHeroReplaced?.();
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        {isHero && (
          <>
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) replaceHero.mutate(f);
                e.target.value = "";
              }}
            />
            <Button
              size="sm"
              variant="outline"
              disabled={replaceHero.isPending}
              onClick={() => fileRef.current?.click()}
            >
              {replaceHero.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Replace className="h-3.5 w-3.5" />
              )}
              Заменить персонажа
            </Button>
          </>
        )}
        {current?.downloadUrl && (
          <Button size="sm" variant="outline" asChild>
            <a href={current.downloadUrl} download target="_blank" rel="noreferrer">
              <Download className="h-3.5 w-3.5" />
              Скачать
            </a>
          </Button>
        )}
        {items.length > 1 && (
          <>
            <Button
              size="icon"
              variant="ghost"
              className="h-8 w-8"
              disabled={index <= 0}
              onClick={() => setIndex((i) => Math.max(0, i - 1))}
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <span className="text-[11px] text-muted-foreground">
              {index + 1} / {items.length}
            </span>
            <Button
              size="icon"
              variant="ghost"
              className="h-8 w-8"
              disabled={index >= items.length - 1}
              onClick={() => setIndex((i) => Math.min(items.length - 1, i + 1))}
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </>
        )}
      </div>
      {items.length > 1 && (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {items.map((item, i) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setIndex(i)}
              className={cn(
                "h-16 w-12 shrink-0 overflow-hidden rounded-lg border transition",
                i === index ? "border-primary ring-1 ring-primary/40" : "border-white/10",
              )}
            >
              {item.previewUrl ? (
                <img src={item.previewUrl} alt="" className="h-full w-full object-cover" />
              ) : (
                <span className="flex h-full items-center justify-center px-1 text-[9px]">{item.label}</span>
              )}
            </button>
          ))}
        </div>
      )}
      {current?.previewUrl && (
        <div className="flex justify-center rounded-xl border border-white/10 bg-black/30 p-2">
          <img src={current.previewUrl} alt="" className="max-h-[45vh] w-full object-contain" />
        </div>
      )}
      {!isHero && (
        <div className="rounded-lg border border-white/10 bg-black/20 p-3">
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            Закадровый текст
          </p>
          <p className="max-h-36 overflow-auto whitespace-pre-wrap text-sm leading-relaxed">
            {current?.content?.trim() || "—"}
          </p>
        </div>
      )}
    </div>
  );
}

function FrameVideosView({ items }: { items: NodeResultItem[] }) {
  const [index, setIndex] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const current = items[index] ?? items[0];

  const goFullscreen = async () => {
    const el = videoRef.current;
    if (!el) return;
    try {
      if (el.requestFullscreen) await el.requestFullscreen();
    } catch {
      toast.error("Полноэкранный режим недоступен");
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={goFullscreen} disabled={!current?.previewUrl}>
          <Maximize2 className="h-3.5 w-3.5" />
          На весь экран
        </Button>
        {current?.downloadUrl && (
          <Button size="sm" variant="outline" asChild>
            <a href={current.downloadUrl} download target="_blank" rel="noreferrer">
              <Download className="h-3.5 w-3.5" />
              Скачать
            </a>
          </Button>
        )}
        {items.length > 1 && (
          <>
            <Button
              size="icon"
              variant="ghost"
              className="h-8 w-8"
              disabled={index <= 0}
              onClick={() => setIndex((i) => Math.max(0, i - 1))}
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <span className="text-[11px] text-muted-foreground">
              {index + 1} / {items.length}
            </span>
            <Button
              size="icon"
              variant="ghost"
              className="h-8 w-8"
              disabled={index >= items.length - 1}
              onClick={() => setIndex((i) => Math.min(items.length - 1, i + 1))}
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </>
        )}
      </div>
      <div className="flex flex-1 items-center justify-center rounded-xl border border-white/10 bg-black/40 p-2">
        {current?.previewUrl ? (
          <video
            ref={videoRef}
            src={current.previewUrl}
            controls
            className="max-h-[60vh] w-full object-contain"
          />
        ) : (
          <p className="py-12 text-sm text-muted-foreground">Видео ещё не сгенерировано</p>
        )}
      </div>
    </div>
  );
}

function SceneImagesGalleryView({ projectId }: { projectId: number }) {
  const media = useQuery({
    queryKey: ["media-review", projectId, "images"],
    queryFn: () => api.listMediaReview(projectId, "images"),
  });

  if (media.isLoading) return <LoadingBlock />;

  const items = (media.data ?? []).filter((f) => f.preview_url);

  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <MediaFrameGallery
        projectId={projectId}
        kind="images"
        items={items}
        showApproveButtons={false}
      />
    </div>
  );
}

function TopicEditView({
  projectId,
  snapshot,
}: {
  projectId: number;
  snapshot: NodeResultSnapshot;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <TopicEditor
        projectId={projectId}
        initialTopic={snapshot.items[0]?.content ?? ""}
      />
      <p className="text-xs text-muted-foreground">
        Для массовой генерации используйте Excel с колонкой «Название ролика».
      </p>
    </div>
  );
}

function ExcelGptResultView({
  projectId,
  nodeKey,
  nodeType,
  snapshot,
}: {
  projectId: number;
  nodeKey?: string | null;
  nodeType?: string | null;
  snapshot: NodeResultSnapshot;
}) {
  const resolve = useQuery({
    queryKey: ["gpt-operator-resolve", projectId, nodeKey],
    queryFn: () => api.resolveGptOperator(projectId, nodeKey!),
    enabled: Boolean(nodeKey),
    staleTime: 5000,
  });
  const replyFromMeta = snapshot.items.find((i) => i.kind === "text")?.content?.trim();
  const replyFromApi =
    typeof resolve.data?.lastResult?.replyPreview === "string"
      ? String(resolve.data.lastResult.replyPreview).trim()
      : "";
  const reply = replyFromMeta || replyFromApi;
  const xlsxItem = snapshot.items.find((i) => i.kind === "xlsx");

  return (
    <div className="space-y-3">
      <XlsxUploadBar
        projectId={projectId}
        nodeKey={nodeKey}
        nodeType={nodeType}
      />
      {xlsxItem ? (
        <p className="text-xs text-muted-foreground">{snapshot.summary}</p>
      ) : null}
      {resolve.isLoading && !reply ? (
        <LoadingBlock />
      ) : reply ? (
        <ScrollArea className="max-h-[55vh] rounded-lg border border-white/10 bg-black/20 p-3">
          <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-foreground/90">
            {reply}
          </pre>
        </ScrollArea>
      ) : (
        <p className="py-4 text-sm text-muted-foreground">
          Ответа GPT пока нет. Запустите ноду — после прогона здесь появится
          текст из gpt_reply.txt.
        </p>
      )}
    </div>
  );
}

function DefaultResultView({
  projectId,
  nodeKey,
  snapshot,
}: {
  projectId: number;
  nodeKey?: string | null;
  snapshot: NodeResultSnapshot;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const uploadXlsx = useMutation({
    mutationFn: (file: File) =>
      api.uploadProjectXlsx(projectId, file, {
        nodeKey: nodeKey ?? undefined,
      }),
    onSuccess: () => {
      toast.success("Excel заменён");
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-preview", projectId] });
      qc.invalidateQueries({ queryKey: ["xlsx-sheets", projectId] });
      qc.invalidateQueries({ queryKey: ["v-menu-xlsx-preview", projectId] });
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const item = snapshot.items[0];
  if (!item) {
    return <p className="text-sm text-muted-foreground">Нет данных для отображения.</p>;
  }

  return (
    <div className="space-y-3">
      {item.kind === "xlsx" && (
        <div className="flex gap-2">
          <Button size="sm" variant="outline" asChild>
            <a
              href={
                item.downloadUrl ||
                api.downloadProjectXlsx(projectId, {
                  nodeKey: nodeKey ?? undefined,
                })
              }
              download
            >
              <Download className="h-3.5 w-3.5" />
              Скачать Excel
            </a>
          </Button>
          <Button size="sm" variant="outline" onClick={() => fileRef.current?.click()}>
            <Upload className="h-3.5 w-3.5" />
            Заменить
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept=".xlsx,.xls"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) uploadXlsx.mutate(f);
              e.target.value = "";
            }}
          />
        </div>
      )}
      {item.previewUrl && item.kind === "image" && (
        <img src={item.previewUrl} alt="" className="max-h-64 rounded-lg object-contain" />
      )}
      {item.previewUrl && item.kind === "video" && (
        <div className="space-y-2">
          <video
            src={item.previewUrl}
            controls
            className="max-h-[60vh] w-full rounded-lg bg-black object-contain"
          />
          {item.downloadUrl ? (
            <Button size="sm" variant="outline" asChild>
              <a href={item.downloadUrl} download>
                <Download className="h-3.5 w-3.5" />
                Скачать
              </a>
            </Button>
          ) : null}
        </div>
      )}
      {item.previewUrl && item.kind === "audio" && (
        <div className="space-y-2">
          <audio src={item.previewUrl} controls className="w-full" />
          {item.downloadUrl ? (
            <Button size="sm" variant="outline" asChild>
              <a href={item.downloadUrl} download>
                <Download className="h-3.5 w-3.5" />
                Скачать
              </a>
            </Button>
          ) : null}
        </div>
      )}
      {item.content && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-black/30 p-3 text-xs">
          {item.content}
        </pre>
      )}
    </div>
  );
}

