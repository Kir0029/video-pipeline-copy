import { api, type ProjectAsset } from "@/lib/api";
import type { ArtifactDTO, FrameDTO, NodeRunStatus, ProjectDetail } from "@/lib/types";
import { MIN_GENERAL_PLAN_CHARS } from "@/lib/node-run-status";
import { isEnrichNode } from "@/lib/node-prompts";
import {
  pickGeneralPlanSheet,
  projectHasXlsx,
  ROW_VOICEOVER_V8,
  SHEET_PLAN_V8,
} from "@/lib/xlsx-sheets";

export type NodeResultItemKind =
  | "text"
  | "image"
  | "video"
  | "audio"
  | "file"
  | "xlsx"
  | "frames";

export interface NodeResultItem {
  id: string;
  label: string;
  kind: NodeResultItemKind;
  previewUrl?: string | null;
  downloadUrl?: string | null;
  content?: string | null;
  /** Путь на диске — для замены hero-файла. */
  filePath?: string | null;
  frameNumber?: number;
}

export type NodeResultReplaceMode = "text" | "xlsx" | "assets" | "studio" | "none";

export type NodeResultViewMode =
  | "default"
  | "xlsx_general_plan"
  | "voiceover_wide"
  | "xlsx_split_row"
  | "frame_prompts"
  | "frame_images"
  | "frame_videos"
  | "topic_edit";

export interface NodeResultSnapshot {
  hasResult: boolean;
  itemCount: number;
  summary: string;
  items: NodeResultItem[];
  replaceMode: NodeResultReplaceMode;
  viewMode: NodeResultViewMode;
  /** Поле проекта для текстовой замены (plan / script). */
  textField?: "general_plan" | "script_text";
}

export interface NodeResultContext {
  project: ProjectDetail | null | undefined;
  artifacts: ArtifactDTO[];
  assets: ProjectAsset[];
  frames: FrameDTO[];
  mediaImages: ProjectAsset[];
  mediaVideos: ProjectAsset[];
}

function meaningfulGeneralPlan(project: ProjectDetail | null | undefined): string | null {
  const text = project?.general_plan?.trim();
  if (!text || text.length < MIN_GENERAL_PLAN_CHARS) return null;
  return text;
}

function filterArtifacts(list: ArtifactDTO[], nodeType: string): ArtifactDTO[] {
  if (nodeType.includes("image") || nodeType === "images" || nodeType === "hitl_images") {
    return list.filter((a) => a.kind.includes("image") || a.kind.includes("scene"));
  }
  if (nodeType.includes("video") || nodeType === "videos" || nodeType === "hitl_videos") {
    return list.filter((a) => a.kind.includes("video") && !a.kind.includes("final"));
  }
  if (nodeType === "hero" || nodeType === "hitl_hero") {
    return list.filter((a) => a.kind.includes("hero"));
  }
  if (nodeType === "items") {
    return list.filter((a) => a.kind.includes("item"));
  }
  if (nodeType === "audio") {
    return list.filter((a) => a.kind === "audio" || a.kind.includes("subtitle"));
  }
  if (nodeType === "music") {
    return list.filter((a) => a.kind === "music" || a.kind.includes("music"));
  }
  if (nodeType === "assemble" || nodeType === "hitl_final") {
    return list.filter((a) => a.kind.includes("final"));
  }
  if (nodeType === "sfx" || nodeType === "sfx_gen") {
    return list.filter((a) => a.kind.includes("sfx") || a.kind.includes("sound"));
  }
  if (nodeType === "sfx_plan") {
    return list.filter((a) => a.kind.includes("sfx_plan"));
  }
  return [];
}

function mediaKind(path: string, kindHint: string): NodeResultItemKind {
  const hint = (kindHint || "").toLowerCase();
  if (/\.(mp4|webm)$/i.test(path) || hint.includes("video") || hint.includes("final")) {
    return "video";
  }
  if (
    /\.(mp3|wav|m4a|ogg|aac|flac)$/i.test(path) ||
    hint === "audio" ||
    hint === "music" ||
    hint.includes("music")
  ) {
    return "audio";
  }
  if (
    /\.(png|jpe?g|webp|gif)$/i.test(path) ||
    hint.includes("image") ||
    hint.includes("hero") ||
    hint.includes("scene")
  ) {
    return "image";
  }
  return "file";
}

/** Уникальные файлы по basename (assets+artifacts часто дублируют один path). */
function dedupeResultItems(items: NodeResultItem[]): NodeResultItem[] {
  const seen = new Set<string>();
  const out: NodeResultItem[] = [];
  for (const item of items) {
    const raw = (item.filePath || item.downloadUrl || item.previewUrl || item.id || "")
      .toLowerCase()
      .replace(/\\/g, "/");
    const base = raw.split("/").pop() || raw;
    if (!base || seen.has(base)) continue;
    seen.add(base);
    out.push(item);
  }
  return out;
}

function artifactItems(arts: ArtifactDTO[]): NodeResultItem[] {
  return arts.map((a) => {
    const path = a.path || "";
    return {
      id: a.uuid,
      label: a.kind,
      kind: mediaKind(path, a.kind || ""),
      previewUrl: api.artifactFileUrl(a.uuid),
      downloadUrl: api.artifactFileUrl(a.uuid),
      filePath: path || null,
    };
  });
}

function assetItems(assets: ProjectAsset[]): NodeResultItem[] {
  return assets.map((a) => {
    const path = a.path || "";
    const voiceover = (a as { voiceover?: string }).voiceover;
    const kind: NodeResultItemKind =
      a.kind === "xlsx" ? "xlsx" : mediaKind(path, a.kind || "");
    return {
      id: String(a.id),
      label: a.label || a.kind || a.id,
      kind,
      previewUrl: a.preview_url,
      downloadUrl: a.preview_url || undefined,
      content: voiceover ?? undefined,
      filePath: path || null,
      frameNumber: a.frame_id ?? undefined,
    };
  });
}

function xlsxAsset(assets: ProjectAsset[]): ProjectAsset | undefined {
  return assets.find((a) => a.id === "project.xlsx" || a.kind === "xlsx");
}

function textAsset(assets: ProjectAsset[], name: string): ProjectAsset | undefined {
  return assets.find((a) => a.id === name || a.label === name);
}

/** Зелёный кубик — только когда шаг реально завершён (не из общего xlsx). */
export function gateNodeResultVisibility(
  snapshot: NodeResultSnapshot,
  nodeType: string,
  nodeStatus?: NodeRunStatus,
): NodeResultSnapshot {
  if (nodeStatus === "done" || nodeStatus === "waiting_hitl") {
    if (!snapshot.hasResult) {
      return {
        ...snapshot,
        hasResult: true,
        summary: snapshot.summary || "Шаг завершён",
      };
    }
    return snapshot;
  }
  if (nodeType === "topic") {
    return snapshot;
  }
  // Реальные артефакты (final mp4 и т.п.) не прячем из‑за unknown/stale NodeRun:
  // иначе assemble при assembled показывает «ещё не готов» при готовом файле.
  if (snapshot.hasResult && snapshot.itemCount > 0) {
    return snapshot;
  }
  if (
    nodeStatus === "pending" ||
    nodeStatus === "skipped" ||
    nodeStatus === "running" ||
    nodeStatus === "failed"
  ) {
    return { ...snapshot, hasResult: false, itemCount: 0 };
  }
  // nodeStatus undefined — доверяем computeNodeResult (не гасим hasResult).
  return snapshot;
}

export function resolveNodeResult(
  nodeType: string,
  ctx: NodeResultContext,
  nodeStatus?: NodeRunStatus,
  nodeKey?: string | null,
): NodeResultSnapshot {
  const snapshot = computeNodeResult(nodeType, ctx, nodeKey);
  return gateNodeResultVisibility(snapshot, nodeType, nodeStatus);
}

function computeNodeResult(
  nodeType: string,
  ctx: NodeResultContext,
  nodeKey?: string | null,
): NodeResultSnapshot {
  const empty = (
    summary: string,
    replaceMode: NodeResultReplaceMode = "none",
    viewMode: NodeResultViewMode = "default",
  ): NodeResultSnapshot => ({
    hasResult: false,
    itemCount: 0,
    summary,
    items: [],
    replaceMode,
    viewMode,
  });

  const ready = (
    items: NodeResultItem[],
    summary: string,
    replaceMode: NodeResultReplaceMode,
    viewMode: NodeResultViewMode = "default",
    textField?: NodeResultSnapshot["textField"],
  ): NodeResultSnapshot => ({
    hasResult: items.length > 0,
    itemCount: items.length,
    summary,
    items,
    replaceMode,
    viewMode,
    textField,
  });

  const project = ctx.project;
  const arts = filterArtifacts(ctx.artifacts, nodeType);

  switch (nodeType) {
    case "topic": {
      const text = project?.topic?.trim() ?? "";
      return {
        hasResult: Boolean(text),
        itemCount: 1,
        summary: text ? "Тема ролика задана" : "Укажите тему ролика",
        items: [
          {
            id: "topic",
            label: "Тема ролика",
            kind: "text",
            content: text,
          },
        ],
        replaceMode: "text",
        viewMode: "topic_edit",
      };
    }

    case "plan":
    case "hitl_gate": {
      const planText = meaningfulGeneralPlan(project);
      if (planText) {
        const items: NodeResultItem[] = [
          { id: "general_plan", label: "Сценарий (текст)", kind: "text", content: planText },
        ];
        if (projectHasXlsx(ctx.assets)) {
          items.push({ id: "xlsx_general", label: "Сценарий (Excel)", kind: "xlsx" });
        }
        return {
          hasResult: true,
          itemCount: items.length,
          summary: `Сценарий готов (${planText.length} симв.)`,
          items,
          replaceMode: "text",
          viewMode: "xlsx_general_plan",
          textField: "general_plan",
        };
      }
      return empty("Сценарий ещё не сгенерирован", "text", "xlsx_general_plan");
    }

    case "script": {
      const text = project?.script_text?.trim();
      if (text) {
        return ready(
          [{ id: "script_text", label: "Закадровый текст", kind: "text", content: text }],
          "Закадровый текст готов",
          "text",
          "voiceover_wide",
          "script_text",
        );
      }
      const voiceFile = textAsset(ctx.assets, "voiceover.txt");
      if (voiceFile) {
        return {
          hasResult: true,
          itemCount: 1,
          summary: "voiceover.txt",
          items: [
            {
              id: voiceFile.id,
              label: "voiceover.txt",
              kind: "file",
              downloadUrl: voiceFile.preview_url,
            },
          ],
          replaceMode: "text",
          viewMode: "voiceover_wide",
          textField: "script_text",
        };
      }
      return empty("Закадровый текст ещё не готов", "text", "voiceover_wide");
    }

    case "split": {
      if (ctx.frames.length > 0) {
        return {
          hasResult: true,
          itemCount: ctx.frames.length,
          summary: `Раскадровка готова (${ctx.frames.length} кадров)`,
          items: [{ id: "split_row", label: "Разбивка", kind: "frames" }],
          replaceMode: "studio",
          viewMode: "xlsx_split_row",
        };
      }
      return empty("Раскадровка ещё не создана", "studio", "xlsx_split_row");
    }

    case "scene_design":
    case "sd_assemble": {
      if (ctx.frames.length > 0) {
        return {
          hasResult: true,
          itemCount: ctx.frames.length,
          summary: "Сцены и атрибуты кадров (лист «план»)",
          items: [{ id: "scene_design_row", label: "Сцены", kind: "frames" }],
          replaceMode: "studio",
          viewMode: "xlsx_split_row",
        };
      }
      return empty("Сцены ещё не собраны", "studio", "xlsx_split_row");
    }

    case "sd_agent": {
      if (ctx.frames.length > 0) {
        return {
          hasResult: true,
          itemCount: ctx.frames.length,
          summary: "Срез агента лёг в staging-ячейки; кадры — после сборки",
          items: [{ id: "sd_agent_row", label: "Агент сцен", kind: "frames" }],
          replaceMode: "studio",
          viewMode: "xlsx_split_row",
        };
      }
      return empty("Агент ещё не отработал", "studio", "xlsx_split_row");
    }

    case "hero":
    case "hitl_hero": {
      const heroAssets = ctx.assets.filter(
        (a) => a.kind === "hero_reference" || a.kind === "hero",
      );
      const items = dedupeResultItems([
        ...artifactItems(arts.filter((a) => a.kind.includes("hero"))),
        ...assetItems(heroAssets),
      ]);
      if (items.length) {
        const descriptions = project?.hero_descriptions ?? [];
        const enriched = items.map((item, i) => ({
          ...item,
          content: item.content?.trim() || descriptions[i] || descriptions[0] || item.label,
        }));
        return ready(enriched, `${items.length} reference персонажей`, "assets", "frame_images");
      }
      if ((project?.hero_descriptions?.length ?? 0) > 0) {
        return ready(
          (project?.hero_descriptions ?? []).map((d, i) => ({
            id: `hero_desc_${i}`,
            label: `Персонаж ${i + 1}`,
            kind: "text" as const,
            content: d,
          })),
          "Описания персонажей без картинок",
          "assets",
        );
      }
      return empty("Персонажи ещё не сгенерированы", "assets");
    }

    case "items": {
      const itemAssets = ctx.assets.filter((a) => a.kind.includes("item") || a.path?.includes("item"));
      const items = [
        ...assetItems(itemAssets),
        ...artifactItems(arts.filter((a) => a.kind.includes("item"))),
      ];
      if (items.length) return ready(items, `${items.length} reference предметов`, "assets");
      if ((project?.item_descriptions?.length ?? 0) > 0) {
        return ready(
          (project?.item_descriptions ?? []).map((d, i) => ({
            id: `item_desc_${i}`,
            label: `Предмет ${i + 1}`,
            kind: "text" as const,
            content: d,
          })),
          "Описания предметов без картинок",
          "assets",
        );
      }
      return empty("Предметы ещё не сгенерированы", "assets");
    }

    case "excel_gpt":
    case "enrich_1":
    case "enrich_2":
    case "enrich_3":
    case "enrich_4":
    case "enrich_5":
    case "enrich": {
      const meta = project?.meta as
        | {
            xlsx_snapshots_by_node?: Record<string, { name?: string }>;
            excel_gpt_nodes?: Record<
              string,
              { lastReplyPath?: string; lastReplyAt?: string }
            >;
            gpt_operator_results?: Record<
              string,
              { replyPreview?: string; outputPaths?: string[] }
            >;
          }
        | undefined;
      const last =
        nodeKey && meta?.gpt_operator_results?.[nodeKey]
          ? meta.gpt_operator_results[nodeKey]
          : undefined;
      const cfg =
        nodeKey && meta?.excel_gpt_nodes?.[nodeKey]
          ? meta.excel_gpt_nodes[nodeKey]
          : undefined;
      const replyPreview = String(last?.replyPreview || "").trim();
      const hasReplyMeta = Boolean(replyPreview || cfg?.lastReplyPath);
      const xlsx = xlsxAsset(ctx.assets);
      const snapMeta = meta?.xlsx_snapshots_by_node;
      const snapName =
        nodeKey && snapMeta?.[nodeKey]?.name
          ? String(snapMeta[nodeKey].name)
          : null;
      if ((hasReplyMeta || snapName) && xlsx && project?.id) {
        const items: NodeResultItem[] = [
          {
            id: xlsx.id,
            label: snapName || "project.xlsx",
            kind: "xlsx",
            downloadUrl: api.downloadProjectXlsx(project.id, {
              nodeKey: nodeKey ?? undefined,
            }),
          },
        ];
        if (replyPreview) {
          items.push({
            id: "gpt_reply",
            label: "Ответ GPT",
            kind: "text",
            content: replyPreview,
          });
        }
        return ready(
          items,
          snapName
            ? `Снимок Excel: ${snapName}`
            : hasReplyMeta
              ? "Excel + ответ GPT"
              : "Таблица Excel",
          "xlsx",
          "default",
        );
      }
      if (replyPreview) {
        return ready(
          [
            {
              id: "gpt_reply",
              label: "Ответ GPT",
              kind: "text",
              content: replyPreview,
            },
          ],
          "Ответ GPT готов",
          "text",
        );
      }
      if (hasReplyMeta) {
        // Путь есть, текст подтянет панель через /gpt-operator/.../resolve
        return {
          hasResult: true,
          itemCount: 1,
          summary: "Ответ GPT на диске",
          items: [
            {
              id: "gpt_reply_path",
              label: "Ответ GPT",
              kind: "text",
              content: "",
            },
          ],
          replaceMode: "none",
          viewMode: "default",
        };
      }
      return empty("Оператор GPT ещё не запускался", "xlsx", "default");
    }

    case "image_prompts": {
      const withPrompt = ctx.frames.filter((f) => f.image_prompt?.trim());
      if (withPrompt.length) {
        return ready(
          withPrompt.map((f) => ({
            id: `frame_${f.id}`,
            label: `Кадр ${f.number}`,
            kind: "text" as const,
            content: f.image_prompt,
            frameNumber: f.number,
          })),
          `Промты картинок: ${withPrompt.length} кадров`,
          "studio",
          "frame_prompts",
        );
      }
      return empty("Промты картинок ещё не готовы", "studio", "frame_prompts");
    }

    case "images":
    case "hitl_images": {
      const frameById = new Map(ctx.frames.map((f) => [f.id, f]));
      const fromMedia = ctx.mediaImages.map((a) => {
        const fr = a.frame_id != null ? frameById.get(a.frame_id) : undefined;
        const base = assetItems([a])[0];
        return {
          ...base,
          content: (a as { voiceover?: string }).voiceover || fr?.voiceover_text || base.content,
        };
      });
      const fromArts = artifactItems(arts);
      const items = (fromMedia.length ? fromMedia : fromArts).filter((i) => i.previewUrl);
      if (items.length) {
        return ready(items, `${items.length} картинок`, "assets", "frame_images");
      }
      return empty("Картинки ещё не сгенерированы", "assets", "frame_images");
    }

    case "animation_prompts": {
      const withPrompt = ctx.frames.filter((f) => f.animation_prompt?.trim());
      if (withPrompt.length) {
        return ready(
          withPrompt.slice(0, 12).map((f) => ({
            id: `frame_${f.id}`,
            label: `Кадр ${f.number}`,
            kind: "text" as const,
            content: f.animation_prompt,
          })),
          `Промты анимации: ${withPrompt.length} кадров`,
          "studio",
        );
      }
      return empty("Промты анимации ещё не готовы", "studio");
    }

    case "videos":
    case "hitl_videos": {
      const fromMedia = assetItems(ctx.mediaVideos).filter((i) => i.previewUrl);
      const fromArts = artifactItems(arts);
      const items = fromMedia.length ? fromMedia : fromArts;
      if (items.length) {
        return ready(items, `${items.length} видео`, "assets", "frame_videos");
      }
      return empty("Видео ещё не сгенерированы", "assets", "frame_videos");
    }

    case "audio": {
      const audioAssets = ctx.assets.filter(
        (a) => a.kind === "audio" || a.kind.includes("subtitle"),
      );
      const items = dedupeResultItems([
        ...artifactItems(arts),
        ...assetItems(audioAssets),
      ]);
      if (items.length) return ready(items, `${items.length} аудио-файлов`, "assets");
      return empty("Аудио ещё не сгенерировано", "assets");
    }

    case "music": {
      const musicAssets = ctx.assets.filter(
        (a) => a.kind === "music" || /[/\\]music[/\\]/i.test(a.path || ""),
      );
      const items = dedupeResultItems([
        ...artifactItems(arts),
        ...assetItems(musicAssets),
      ]);
      if (items.length) {
        return ready(items, `Музыка: ${items.length} файл(ов)`, "assets");
      }
      return empty("Музыка ещё не сгенерирована", "assets");
    }

    case "sfx_plan": {
      const meta = project?.meta as Record<string, unknown> | undefined;
      const aiJobs = meta?.ai_jobs as Record<string, unknown> | undefined;
      const sfxPlan = meta?.sfx_plan || meta?.sound_plan || aiJobs?.sfx_plan;
      const sfxPlanAsset = ctx.assets.find(
        (a) => a.id === "sfx_plan.json" || a.kind === "sfx_plan" || a.path?.includes("sfx_plan.json"),
      );
      if (sfxPlan || sfxPlanAsset) {
        return {
          hasResult: true,
          itemCount: 1,
          summary: "План звуков готов",
          items: [
            {
              id: "sfx_plan",
              label: "План звуков (sfx_plan.json)",
              kind: "file",
              downloadUrl: sfxPlanAsset?.preview_url,
            },
          ],
          replaceMode: "studio",
          viewMode: "default",
        };
      }
      return empty("План звуков ещё не составлен", "studio");
    }

    case "sfx":
    case "sfx_gen": {
      const sfxAssets = ctx.assets.filter(
        (a) => a.kind === "sfx" || a.kind.includes("sound") || /[/\\]sfx[/\\]/i.test(a.path || ""),
      );
      const items = dedupeResultItems([
        ...artifactItems(arts),
        ...assetItems(sfxAssets),
      ]);
      if (items.length) {
        return ready(items, `Звуки: ${items.length} файл(ов)`, "assets");
      }
      return empty("Звуки ещё не сгенерированы", "assets");
    }

    case "assemble":
    case "hitl_final": {
      const finalArts = artifactItems(arts);
      const finalAssets = ctx.assets.filter((a) => a.kind.includes("final"));
      const items = dedupeResultItems([...finalArts, ...assetItems(finalAssets)]);
      if (items.length) return ready(items, "Финальный ролик готов", "assets");
      if (project?.status === "assembled") {
        return {
          hasResult: true,
          itemCount: 0,
          summary: "Сборка завершена",
          items: [],
          replaceMode: "assets",
          viewMode: "default",
        };
      }
      return empty("Финальный ролик ещё не собран", "assets");
    }

    case "publish": {
      if (project?.status === "published") {
        return {
          hasResult: true,
          itemCount: 0,
          summary: "Опубликовано",
          items: [],
          replaceMode: "none",
          viewMode: "default",
        };
      }
      const final = ctx.assets.filter((a) => a.kind.includes("final"));
      if (final.length) return ready(assetItems(final), "Готово к публикации", "assets");
      return empty("Публикация ещё не выполнена", "none");
    }

    default: {
      if (isEnrichNode(nodeType)) {
        return computeNodeResult("enrich_1", ctx, nodeKey);
      }
      const generic = artifactItems(filterArtifacts(ctx.artifacts, nodeType)).slice(0, 8);
      if (generic.length) return ready(generic, `${generic.length} артефактов`, "studio");
      return empty("Результата пока нет", "none");
    }
  }
}
