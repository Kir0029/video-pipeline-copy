"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { errorMessageFromUnknown } from "@/lib/error-message";
import { api } from "@/lib/api";
import { projectDisplayName } from "@/lib/project-display";
import type { ProjectSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

const TONE_CHOICES = [
  { id: "grimdark", label: "Grimdark / Sci-Fi" },
  { id: "action", label: "Кино-экшен" },
  { id: "drama", label: "Философия / Драма" },
  { id: "mystery", label: "Мистика / Саспенс" },
] as const;

const VOICEOVER_CHOICES = [
  { id: "epic_quotes", label: "Эпос + цитаты" },
  { id: "narrator", label: "Кино-рассказчик" },
  { id: "dynamic", label: "Динамичный темп" },
  { id: "none", label: "Без диктора (SFX)" },
] as const;

const HERO_CHOICES = [
  { id: "auto", label: "Авто" },
  { id: "hero", label: "С героями" },
  { id: "no_hero", label: "Без героев" },
] as const;

export function NewProjectWizard({
  trigger,
  onCreated,
  folderId = null,
}: {
  trigger: React.ReactNode;
  onCreated: (p: ProjectSummary) => void;
  folderId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [projectTitle, setProjectTitle] = useState("");
  const [topic, setTopic] = useState("");
  const [selectedTone, setSelectedTone] = useState<string | null>(null);
  const [selectedVoice, setSelectedVoice] = useState<string | null>(null);
  const [heroMode, setHeroMode] = useState<"hero" | "no_hero" | "auto">("auto");
  const [isAssisting, setIsAssisting] = useState(false);

  const qc = useQueryClient();

  const reset = () => {
    setProjectTitle("");
    setTopic("");
    setSelectedTone(null);
    setSelectedVoice(null);
    setHeroMode("auto");
    setIsAssisting(false);
  };

  const handleAssist = async (mode: "expand" | "generate") => {
    if (isAssisting) return;
    setIsAssisting(true);
    try {
      const res = await api.assistProject({
        topic_draft: topic.trim(),
        title_draft: projectTitle.trim(),
        tone: selectedTone,
        voiceover_style: selectedVoice,
        mode,
      });
      if (res.ok && res.topic) {
        setTopic(res.topic);
        if (res.title && !projectTitle.trim()) {
          setProjectTitle(res.title);
        }
        if (res.suggested_hero_mode) {
          setHeroMode(res.suggested_hero_mode);
        }
        toast.success(mode === "generate" ? "Идея сформирована ИИ-ассистентом" : "Сюжет доработан ИИ-ассистентом");
      } else {
        toast.error("ИИ не смог сформировать ответ. Попробуйте еще раз.");
      }
    } catch (e) {
      toast.error(errorMessageFromUnknown(e));
    } finally {
      setIsAssisting(false);
    }
  };

  const create = useMutation({
    mutationFn: async () => {
      let finalTopic = topic.trim();
      const toneLabel = TONE_CHOICES.find((t) => t.id === selectedTone)?.label;
      const voiceLabel = VOICEOVER_CHOICES.find((v) => v.id === selectedVoice)?.label;

      const extras: string[] = [];
      if (toneLabel && !finalTopic.toLowerCase().includes(toneLabel.toLowerCase())) {
        extras.push(`Атмосфера: ${toneLabel}`);
      }
      if (voiceLabel && !finalTopic.toLowerCase().includes(voiceLabel.toLowerCase())) {
        extras.push(`Стиль озвучки: ${voiceLabel}`);
      }
      if (extras.length > 0) {
        finalTopic = finalTopic ? `${finalTopic}\n\n${extras.join(". ")}.` : extras.join(". ") + ".";
      }

      const rawTitle = projectTitle.trim() || (topic.trim() ? topic.trim().slice(0, 40) : "Новый проект");
      const p = await api.createProject({
        title: rawTitle.slice(0, 120),
        topic: finalTopic || rawTitle,
        hero_mode: heroMode,
        auto_mode: false,
        sidebar_folder_id: folderId,
      });
      return p;
    },
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      onCreated(p);
      setOpen(false);
      reset();
      toast.success(`Проект «${projectDisplayName(p)}» создан`);
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) reset();
      }}
    >
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="sm:max-w-[560px] bg-card border-border p-6 shadow-2xl">
        <DialogHeader className="space-y-1">
          <DialogTitle className="text-lg font-semibold tracking-tight">Новый проект</DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">
            Задайте сюжет и ключевые параметры ролика. Технические настройки генераторов можно настроить прямо на нодах.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {/* Название */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Название проекта
            </label>
            <Input
              placeholder="Например: Warhammer 40K: Бастион Кровавых Ангелов"
              value={projectTitle}
              onChange={(e) => setProjectTitle(e.target.value)}
              className="h-9 bg-background"
            />
          </div>

          {/* Сюжет / Бриф + ИИ-ассистент */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Сюжет / Бриф
              </label>
              <div className="flex items-center gap-1.5">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={isAssisting}
                  onClick={() => handleAssist("expand")}
                  className="h-7 px-3 text-xs font-semibold text-cyan-200 bg-cyan-950/50 hover:bg-cyan-900/70 border border-cyan-500/40 shadow-sm rounded-lg transition-all"
                  title="Доработать сюжет с помощью ИИ-ассистента"
                >
                  {isAssisting ? <Loader2 className="h-3 w-3 animate-spin mr-1.5" /> : null}
                  <span>Развить сюжет</span>
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={isAssisting}
                  onClick={() => handleAssist("generate")}
                  className="h-7 px-3 text-xs font-semibold text-cyan-300/90 hover:text-cyan-100 bg-cyan-950/20 hover:bg-cyan-950/50 border border-cyan-500/30 hover:border-cyan-400/50 rounded-lg transition-all"
                  title="Сгенерировать сюжетную идею с помощью ИИ-ассистента"
                >
                  {isAssisting ? <Loader2 className="h-3 w-3 animate-spin mr-1.5" /> : null}
                  <span>Идея с нуля</span>
                </Button>
              </div>
            </div>
            <Textarea
              placeholder="Опишите сюжет своими словами или нажмите «Развить сюжет» для помощи ИИ-ассистента..."
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              rows={5}
              className="resize-none bg-background text-sm leading-relaxed"
            />
          </div>

          {/* Атмосфера (опционально) */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Атмосфера (по желанию)
              </label>
              {selectedTone && (
                <button
                  type="button"
                  onClick={() => setSelectedTone(null)}
                  className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
                >
                  Сбросить
                </button>
              )}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {TONE_CHOICES.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setSelectedTone((prev) => (prev === t.id ? null : t.id))}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
                    selectedTone === t.id
                      ? "border-cyan-400/60 bg-cyan-950/60 text-cyan-300 shadow-sm shadow-cyan-950/40 font-semibold"
                      : "border-border bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground"
                  )}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>

          {/* Озвучка и цитаты (опционально) */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Озвучка и цитаты (по желанию)
              </label>
              {selectedVoice && (
                <button
                  type="button"
                  onClick={() => setSelectedVoice(null)}
                  className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
                >
                  Сбросить
                </button>
              )}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {VOICEOVER_CHOICES.map((v) => (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => setSelectedVoice((prev) => (prev === v.id ? null : v.id))}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
                    selectedVoice === v.id
                      ? "border-cyan-400/60 bg-cyan-950/60 text-cyan-300 shadow-sm shadow-cyan-950/40 font-semibold"
                      : "border-border bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground"
                  )}
                >
                  {v.label}
                </button>
              ))}
            </div>
          </div>

          {/* Персонажи */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Персонажи
            </label>
            <div className="inline-flex rounded-lg border border-border p-0.5 bg-muted/30">
              {HERO_CHOICES.map((h) => (
                <button
                  key={h.id}
                  type="button"
                  onClick={() => setHeroMode(h.id)}
                  className={cn(
                    "rounded-md px-3 py-1 text-xs font-medium transition-all",
                    heroMode === h.id
                      ? "bg-cyan-500/20 text-cyan-300 border border-cyan-400/30 shadow-sm font-semibold"
                      : "text-muted-foreground hover:text-foreground border border-transparent"
                  )}
                >
                  {h.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <DialogFooter className="pt-2 flex items-center justify-between sm:justify-between border-t border-border mt-1">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setOpen(false);
              reset();
            }}
          >
            Отмена
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={create.isPending || (!projectTitle.trim() && !topic.trim())}
            onClick={() => create.mutate()}
            className="min-w-[140px] h-9 text-xs font-semibold text-white bg-cyan-600 hover:bg-cyan-500 border border-cyan-400/50 shadow-md shadow-cyan-500/25 rounded-xl transition-all disabled:opacity-50"
          >
            {create.isPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Создание...
              </>
            ) : (
              "Создать проект"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
