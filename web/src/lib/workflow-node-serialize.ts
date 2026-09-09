import type { Node } from "@xyflow/react";
import { getNodeSpec } from "@/lib/node-catalog";
import type { PipelineNodeData } from "@/components/canvas/pipeline-node";
import type { WorkflowNode } from "@/lib/types";

function isLegacyEnrichLabel(label: string | undefined): boolean {
  if (!label?.trim()) return false;
  const low = label.trim().toLowerCase();
  return (
    low.includes("дополнение") ||
    low.includes("доп работа") ||
    low.includes("доп. работа") ||
    low.includes("доп. excel") ||
    low.includes("enrich") ||
    low.includes("excel #")
  );
}

const EXCEL_GPT_DEFAULT_LABEL = "ИИ-редактор сцен";

function defaultExcelGptLabel(_slot: number): string {
  return EXCEL_GPT_DEFAULT_LABEL;
}

function migrateNodeType(type: string): string {
  if (type.startsWith("enrich_")) return "excel_gpt";
  return type;
}

function slotFromLegacyType(type: string): number | undefined {
  if (!type.startsWith("enrich_")) return undefined;
  const n = Number.parseInt(type.replace("enrich_", ""), 10);
  return Number.isFinite(n) && n >= 1 && n <= 5 ? n : undefined;
}

export function workflowNodeFromCanvas(n: Node): WorkflowNode {
  const d = n.data as PipelineNodeData;
  const type = migrateNodeType(d.type);
  const spec = getNodeSpec(type);
  const label = (typeof d.label === "string" && d.label.trim()) || spec.label;
  const data: Record<string, unknown> = {
    label,
    description: d.description ?? spec.description,
  };
  const legacySlot = slotFromLegacyType(d.type);
  if (legacySlot != null) data.slotIndex = legacySlot;
  if (typeof d.slotIndex === "number") data.slotIndex = d.slotIndex;
  // slotOverflow (проверки scene-веера и т.п.) — вне enrich-слотов, не терять.
  if (d.slotOverflow === true) {
    data.slotOverflow = true;
    delete data.slotIndex;
  }
  if (typeof d.inputSource === "string") data.inputSource = d.inputSource;
  if (typeof d.uploadedFileName === "string") data.uploadedFileName = d.uploadedFileName;
  if (typeof d.workMode === "string") data.workMode = d.workMode;
  if (typeof d.role === "string") data.role = d.role;
  // sd_agent: имя агента — идентичность ноды (per-agent rerun), не терять.
  if (typeof d.agent === "string") data.agent = d.agent;
  // Маркер scene-агента на ноде «Работа с GPT» — идентичность, не терять.
  if (typeof d.sdAgent === "string") data.sd_agent = d.sdAgent;
  // Принадлежность к импортированной группе — обводка на канвасе, не терять.
  if (typeof d.groupId === "string") data.groupId = d.groupId;
  if (typeof d.groupTitle === "string") data.groupTitle = d.groupTitle;
  if (typeof d.modelId === "string" && d.modelId.trim()) data.modelId = d.modelId.trim();
  if (typeof d.modelChannel === "string" && d.modelChannel.trim()) {
    data.modelChannel = d.modelChannel.trim();
  }
  if (typeof d.imageResolution === "string" && d.imageResolution.trim()) {
    data.imageResolution = d.imageResolution.trim();
  }
  if (typeof d.imageQuality === "string" && d.imageQuality.trim()) {
    data.imageQuality = d.imageQuality.trim();
  }
  if (typeof d.aspectRatio === "string" && d.aspectRatio.trim()) {
    data.aspectRatio = d.aspectRatio.trim();
  }
  return {
    id: n.id,
    type,
    position: n.position,
    data,
  };
}

export function assignExcelGptSlotIndices(nodes: WorkflowNode[]): WorkflowNode[] {
  // scene-агенты (data.sd_agent) — не enrich-слоты: нумерацию пропускают.
  const isSceneAgent = (n: WorkflowNode) =>
    typeof (n.data as Record<string, unknown> | undefined)?.sd_agent === "string" ||
    typeof (n.data as Record<string, unknown> | undefined)?.agent === "string";
  // slotOverflow — закреплённые вне слотов (проверки scene-веера): не трогаем.
  const isOverflow = (n: WorkflowNode) =>
    (n.data as Record<string, unknown> | undefined)?.slotOverflow === true;
  const excel = nodes
    .filter((n) => n.type === "excel_gpt" && !isSceneAgent(n) && !isOverflow(n))
    .sort((a, b) => (a.position?.x ?? 0) - (b.position?.x ?? 0));
  const slotById = new Map<string, number>();
  excel.forEach((n, i) => slotById.set(n.id, Math.min(i + 1, 5)));
  return nodes.map((n) => {
    if (n.type !== "excel_gpt") return n;
    if (isSceneAgent(n)) {
      const data = { ...(n.data || {}) } as Record<string, unknown>;
      delete data.slotIndex;
      return { ...n, data };
    }
    if (isOverflow(n)) {
      const data = { ...(n.data || {}) } as Record<string, unknown>;
      delete data.slotIndex;
      return { ...n, data };
    }
    const slotIndex = slotById.get(n.id) ?? 1;
    const rawLabel = (n.data?.label as string) || "";
    const label =
      rawLabel.trim() && !isLegacyEnrichLabel(rawLabel)
        ? rawLabel.trim()
        : defaultExcelGptLabel(slotIndex);
    return {
      ...n,
      data: {
        ...(n.data || {}),
        slotIndex,
        label,
      },
    };
  });
}

export function ensureSpaciousNodePositions(nodes: WorkflowNode[]): WorkflowNode[] {
  if (!nodes || nodes.length <= 1) return nodes;

  const sorted = [...nodes].sort((a, b) => (a.position?.x ?? 0) - (b.position?.x ?? 0));

  let needsExpansion = false;
  for (let i = 0; i < sorted.length - 1; i++) {
    const diff = (sorted[i + 1].position?.x ?? 0) - (sorted[i].position?.x ?? 0);
    if (diff > 50 && diff < 340) {
      needsExpansion = true;
      break;
    }
  }

  if (!needsExpansion) return nodes;

  const columns: WorkflowNode[][] = [];
  for (const node of sorted) {
    const matchCol = columns.find(
      (col) => Math.abs((col[0].position?.x ?? 0) - (node.position?.x ?? 0)) < 160,
    );
    if (matchCol) {
      matchCol.push(node);
    } else {
      columns.push([node]);
    }
  }

  let currentX = columns[0]?.[0]?.position?.x ?? 80;
  const STEP_X = 380;
  const newPositions = new Map<string, { x: number; y: number }>();

  columns.forEach((col) => {
    col.forEach((node) => {
      newPositions.set(node.id, {
        x: currentX,
        y: node.position?.y ?? 200,
      });
    });
    const maxColWidth = Math.max(
      ...col.map((n) =>
        n.type === "excel_gpt" || n.type === "storage" || n.type === "shot_menu" ? 320 : 280,
      ),
    );
    currentX += Math.max(STEP_X, maxColWidth + 70);
  });

  return nodes.map((n) => {
    const pos = newPositions.get(n.id);
    return pos ? { ...n, position: pos } : n;
  });
}

export function migrateWorkflowNodes(nodes: WorkflowNode[]): WorkflowNode[] {
  const migrated = nodes.map((n) => {
    if (!n.type.startsWith("enrich_")) return n;
    const slot = slotFromLegacyType(n.type);
    const rawLabel = (n.data?.label as string) || "";
    const label =
      rawLabel.trim() && !isLegacyEnrichLabel(rawLabel)
        ? rawLabel.trim()
        : defaultExcelGptLabel(slot ?? 1);
    return {
      ...n,
      type: "excel_gpt",
      data: {
        ...(n.data || {}),
        slotIndex: slot ?? (n.data?.slotIndex as number | undefined),
        label,
      },
    };
  });
  return ensureSpaciousNodePositions(assignExcelGptSlotIndices(migrated));
}
