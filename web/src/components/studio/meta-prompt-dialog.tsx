"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Loader2,
  CheckCircle2,
  FileCode,
  Save,
  Wand2,
  ShieldCheck,
  Zap,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { errorMessageFromUnknown } from "@/lib/error-message";
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

const STEP_PRESETS: Record<string, { label: string; intent: string; name: string }[]> = {
  excel_gpt: [
    {
      label: "🎨 Настроение и свет",
      intent: "Определяй настроение каждого кадра (1-10), тип освещения и 3 ключевых цвета палитры.",
      name: "mood_lighting",
    },
    {
      label: "✍️ Сценарист (пачками)",
      intent: "Упакуй закадровый текст в плотные смысловые блоки по 9 ячеек со связкой темпа.",
      name: "scriptwriter_batch",
    },
    {
      label: "🔍 Контроль качества (QC)",
      intent: "Проверяй логику переходов между кадрами и отсутствие противоречий в действиях персонажей.",
      name: "scene_qc_check",
    },
  ],
  hero_style: [
    {
      label: "🌆 Киберпанк 8K",
      intent: "Стиль киберпанк, неоновые акценты, ночной дождь, 8k, Unreal Engine 5, 85mm anamorphic lens, rim lighting.",
      name: "cyberpunk_neon_8k",
    },
    {
      label: "🌸 Аниме (Синкай)",
      intent: "Высокохудожественное аниме в стиле Макото Синкая, мягкие облака, золотой час, акварельные детали.",
      name: "anime_shinkai_sunset",
    },
    {
      label: "🎬 Кинематографичный реализм",
      intent: "Фотореалистичный 35mm пленочный кадр, естественная текстура кожи, естественные тени, Hasselblad.",
      name: "cinematic_realism",
    },
  ],
  items: [
    {
      label: "📱 Футуристичный гаджет",
      intent: "Высокотехнологичное наручное устройство / коммуникатор с прозрачным голографическим дисплеем, титан и стекло.",
      name: "holo_gadget_16_9",
    },
    {
      label: "🛸 Дрон-разведчик",
      intent: "Компактный автономный дрон с матовым карбоновым корпусом, оптическими сенсорами и подсветкой слотов.",
      name: "stealth_drone_sheet",
    },
  ],
  img_pr: [
    {
      label: "🎥 Кинематографичный ракурс",
      intent: "Создавай детальные промпты кадров с указанием оптики, глубины резкости, ракурса камеры и световой схемы.",
      name: "cinematic_shot_framing",
    },
  ],
  anim_pr: [
    {
      label: "📹 Плавный наезд камеры",
      intent: "Директивы движения для Veo 3.1 / Kling: медленный плавный зум вперед (slow dolly in), микро-движения и параллакс.",
      name: "smooth_dolly_pan",
    },
  ],
};

export function MetaPromptDialog({
  stepCode,
  projectId,
  onSaved,
}: {
  stepCode: string;
  projectId?: number;
  onSaved?: (fileName: string) => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [intent, setIntent] = useState("");
  const [presetName, setPresetName] = useState("");
  const [compiledPrompt, setCompiledPrompt] = useState<string | null>(null);
  const [stats, setStats] = useState<{
    length_chars: number;
    lines_count: number;
    has_schema: boolean;
    has_guards: boolean;
  } | null>(null);

  const presets = STEP_PRESETS[stepCode] ?? STEP_PRESETS.excel_gpt;

  const compileMutation = useMutation({
    mutationFn: async () => {
      if (!intent.trim()) {
        throw new Error("Введите описание задачи для Агента");
      }
      return await api.compileMetaPrompt({
        step_code: stepCode,
        user_intent: intent,
        project_id: projectId,
        target_name: presetName.trim() || undefined,
      });
    },
    onSuccess: (data) => {
      setCompiledPrompt(data.compiled_prompt);
      setStats(data.stats);
      if (!presetName.trim() && data.name) {
        setPresetName(data.name);
      }
      toast.success("Промпт успешно скомпилирован ИИ-Агентом!");
    },
    onError: (err) => {
      toast.error(errorMessageFromUnknown(err));
    },
  });

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!compiledPrompt) return;
      const name = presetName.trim() || "custom_meta_prompt";
      return await api.saveAndActivateMetaPrompt({
        step_code: stepCode,
        name,
        content: compiledPrompt,
        project_id: projectId,
        activate: true,
      });
    },
    onSuccess: (data) => {
      if (!data) return;
      toast.success(`Промпт ${data.file_name} сохранён и активирован!`);
      qc.invalidateQueries({ queryKey: ["prompt-files"] });
      qc.invalidateQueries({ queryKey: ["project"] });
      if (onSaved) {
        onSaved(data.file_name);
      }
      setOpen(false);
      setCompiledPrompt(null);
      setIntent("");
      setPresetName("");
    },
    onError: (err) => {
      toast.error(errorMessageFromUnknown(err));
    },
  });

  const handleApplyPreset = (p: { label: string; intent: string; name: string }) => {
    setIntent(p.intent);
    setPresetName(p.name);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="gap-1.5 border-cyan-500/40 bg-cyan-500/10 text-xs font-semibold text-cyan-300 hover:bg-cyan-500/20 shadow-sm transition-all"
        >
          Создать с ИИ-агентом
        </Button>
      </DialogTrigger>

      <DialogContent
        overlayClassName="z-[200]"
        className="z-[210] max-w-2xl max-h-[90vh] overflow-y-auto border-border/80 bg-card/95 backdrop-blur-xl"
      >
        <DialogHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/20 text-primary">
              <Wand2 className="h-4 w-4" />
            </div>
            <div>
              <DialogTitle className="text-base font-semibold">
                Конструктор мастер-промптов ИИ-Агентом
              </DialogTitle>
              <DialogDescription className="text-xs">
                Шаг: <span className="font-mono text-foreground font-semibold">{stepCode}</span> — напишите задачу своими словами, агент скомпилирует системный .md промпт.
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {/* Быстрые шаблоны */}
          <div>
            <span className="text-[11px] font-medium text-muted-foreground">
              Быстрые примеры для этого шага:
            </span>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {presets.map((p, idx) => (
                <button
                  key={idx}
                  type="button"
                  onClick={() => handleApplyPreset(p)}
                  className="rounded-full border border-white/10 bg-muted/40 px-2.5 py-1 text-[11px] hover:border-primary/50 hover:bg-primary/10 transition-colors"
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          {/* Имя пресета */}
          <div className="grid grid-cols-3 gap-2">
            <div className="col-span-1">
              <label className="text-xs font-medium text-muted-foreground">
                Имя файла (.md):
              </label>
              <Input
                placeholder="my_custom_prompt"
                value={presetName}
                onChange={(e) => setPresetName(e.target.value)}
                className="mt-1 h-8 text-xs font-mono"
              />
            </div>
            <div className="col-span-2">
              <label className="text-xs font-medium text-muted-foreground">
                Что должен делать этот промпт:
              </label>
              <Textarea
                placeholder="Опишите желаемую логику, колонки, стиль или правила..."
                value={intent}
                onChange={(e) => setIntent(e.target.value)}
                className="mt-1 min-h-[70px] text-xs resize-none"
              />
            </div>
          </div>

          {/* Кнопка запуска компиляции */}
          <Button
            type="button"
            className="w-full gap-2 text-xs font-semibold"
            disabled={compileMutation.isPending || !intent.trim()}
            onClick={() => compileMutation.mutate()}
          >
            {compileMutation.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Компилируем промпт через ИИ-Агента...
              </>
            ) : (
              <>
                <Zap className="h-3.5 w-3.5 fill-current" />
                Скомпилировать системный .md промпт
              </>
            )}
          </Button>

          {/* Результат компиляции */}
          {compiledPrompt && (
            <div className="space-y-2 rounded-xl border border-border/80 bg-background/60 p-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-xs font-semibold text-emerald-400">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  Готовый результат компиляции
                </div>
                {stats && (
                  <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                    <span>{stats.lines_count} строк</span>
                    <span>·</span>
                    <span>{stats.length_chars} символов</span>
                    {stats.has_guards && (
                      <span className="flex items-center gap-0.5 text-emerald-400 font-medium">
                        <ShieldCheck className="h-3 w-3" /> No-Chat Guard
                      </span>
                    )}
                  </div>
                )}
              </div>

              <Textarea
                value={compiledPrompt}
                onChange={(e) => setCompiledPrompt(e.target.value)}
                className="min-h-[220px] font-mono text-[11px] leading-relaxed bg-black/40"
              />
            </div>
          )}
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setOpen(false)}
            className="text-xs"
          >
            Отмена
          </Button>

          {compiledPrompt && (
            <Button
              type="button"
              size="sm"
              className="w-28 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold"
              disabled={saveMutation.isPending}
              onClick={() => saveMutation.mutate()}
            >
              {saveMutation.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : null}
              Сохранить
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}