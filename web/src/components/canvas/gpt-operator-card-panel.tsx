"use client";

import { useState } from "react";
import { Bot, ChevronDown, ChevronUp } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { isBranchingRole, roleChip } from "@/lib/gpt-operator";
import { GptOperatorMenuPanel } from "./gpt-operator-menu";
import { cn } from "@/lib/utils";

/** Пульт оператора прямо на карточке ноды «Работа с GPT». */
export function GptOperatorCardPanel({
  projectId,
  nodeKey,
  onOpenStudio,
}: {
  projectId: number;
  nodeKey: string;
  onOpenStudio?: () => void;
}) {
  /** По умолчанию свёрнут — карточка компактная, пульт по клику. */
  const [open, setOpen] = useState(false);
  const resolve = useQuery({
    queryKey: ["gpt-operator-resolve", projectId, nodeKey],
    queryFn: () => api.resolveGptOperator(projectId, nodeKey),
    staleTime: 4000,
  });
  const data = resolve.data;

  return (
    <div className="border-t border-white/[0.06] px-3 pb-3 pt-2">
      <button
        type="button"
        className="nodrag nopan flex w-full items-center gap-2 rounded-xl border border-violet-500/30 bg-violet-950/40 px-3 py-2.5 text-left transition hover:border-violet-400/50 hover:bg-violet-900/40 shadow-sm"
        onMouseDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        <Bot className="h-4 w-4 shrink-0 text-violet-400" />
        <span className="min-w-0 flex-1">
          <span className="block text-xs sm:text-[13px] font-bold text-zinc-100">
            Пульт оператора
          </span>
          <span className="mt-1 block text-[11px] sm:text-xs text-zinc-300 font-medium leading-normal">
            {roleChip(data?.role)}
            {isBranchingRole(data?.role) ? " · ок/не ок" : ""}
            {" · "}файлов {data?.okFileCount ?? "…"}
            {data && !data.consistent ? " · рассинхрон" : ""}
            {data?.branching && isBranchingRole(data.role)
              ? ` · ветки ${data.branching.hasPass ? "ок" : "—"}/${data.branching.hasFail ? "неок" : "—"}`
              : ""}
            {data?.analysis?.verdict
              ? ` · вердикт ${data.analysis.verdict === "pass" ? "ок" : "не ок"}`
              : ""}
            {" · "}
            {open ? "свернуть" : "открыть роли / файлы / выход"}
          </span>
        </span>
        {open ? (
          <ChevronUp className="h-4 w-4 text-zinc-400" />
        ) : (
          <ChevronDown className="h-4 w-4 text-zinc-400" />
        )}
      </button>

      <div
        className={cn(
          "overflow-hidden transition-[max-height,opacity]",
          open ? "mt-2 max-h-[640px] opacity-100 overflow-y-auto" : "max-h-0 opacity-0",
        )}
        onMouseDown={(e) => e.stopPropagation()}
        onClick={(e) => e.stopPropagation()}
      >
        {open ? (
          <div className="rounded-xl border border-white/10 bg-black/30 p-2">
            <GptOperatorMenuPanel
              projectId={projectId}
              nodeKey={nodeKey}
              onOpenPrompts={onOpenStudio}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}
