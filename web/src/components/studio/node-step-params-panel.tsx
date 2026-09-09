"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Save, ChevronDown, ChevronUp, Calculator } from "lucide-react";
import { toast } from "sonner";
import { errorMessageFromUnknown } from "@/lib/error-message";
import { api } from "@/lib/api";
import type { ProjectDetail } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DEFAULT_ELEVENLABS_VOICE_ID,
  ELEVENLABS_VOICES,
  elevenLabsVoiceLabel,
  findElevenLabsVoice,
} from "@/lib/elevenlabs-voices";
import {
  charCountFromDuration,
  effectiveDurationSeconds,
  readNodeStepParams,
  withNodeStepParams,
  bgmLevelToDb,
  type AssembleStepParams,
  type AudioStepParams,
  type NodeStepParamsMeta,
  type PlanScriptStepParams,
  type SplitStepParams,
} from "@/lib/node-step-params";
import { cn } from "@/lib/utils";

function NumField({
  label,
  description,
  value,
  onChange,
  placeholder = "____",
}: {
  label: string;
  description?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-sm font-medium text-foreground">{label}</span>
      {description ? (
        <span className="text-xs text-muted-foreground">{description}</span>
      ) : null}
      <Input
        type="number"
        min={1}
        step={1}
        className="max-w-[200px] font-mono"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function PlanScriptFields({
  header,
  step,
  params,
  inheritedFromPlan,
  onSave,
  saving,
}: {
  header: string;
  step: "plan" | "script";
  params: NodeStepParamsMeta;
  inheritedFromPlan: boolean;
  onSave: (patch: PlanScriptStepParams) => void;
  saving: boolean;
}) {
  const own = params[step]?.duration_seconds;
  const effective = effectiveDurationSeconds(params, step);
  const [durDraft, setDurDraft] = useState(
    own != null && own > 0 ? String(own) : "",
  );

  useEffect(() => {
    setDurDraft(own != null && own > 0 ? String(own) : "");
  }, [own, step]);

  const charCount = charCountFromDuration(effective);

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-white/10 bg-white/[0.02] p-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground">{header}</h3>
        {inheritedFromPlan ? (
          <p className="mt-1 text-xs text-amber-400/90">
            Длина не задана для закадрового текста — используются значения из «Сценарий» (
            {effective} сек).
          </p>
        ) : null}
      </div>
      <NumField
        label="Длина, секунд"
        description="Пустое поле в GPT-сообщении отображается как ____"
        value={durDraft}
        onChange={setDurDraft}
      />
      <div className="rounded-md border border-white/5 bg-black/20 px-3 py-2 text-sm">
        <p className="text-muted-foreground">Количество символов (длина × 14)</p>
        <p className="mt-1 font-mono text-foreground">
          = {charCount != null ? charCount : "____"}
        </p>
      </div>
      <Button
        size="sm"
        className="w-fit self-start h-8 px-5 text-xs font-semibold text-white bg-cyan-600 hover:bg-cyan-500 border border-cyan-400/50 shadow-md shadow-cyan-500/20 rounded-xl transition-all disabled:opacity-50"
        disabled={saving}
        onClick={() => {
          const trimmed = durDraft.trim();
          onSave({
            duration_seconds: trimmed ? Math.max(1, Math.round(Number(trimmed))) : null,
          });
        }}
      >
        {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
        Сохранить
      </Button>
    </section>
  );
}

function AudioFields({
  params,
  onSave,
  saving,
}: {
  params: NodeStepParamsMeta;
  onSave: (patch: AudioStepParams) => void;
  saving: boolean;
}) {
  const savedId = params.audio?.elevenlabs_voice_id ?? DEFAULT_ELEVENLABS_VOICE_ID;
  const [voiceDraft, setVoiceDraft] = useState(savedId);

  useEffect(() => {
    setVoiceDraft(savedId);
  }, [savedId]);

  const selected = findElevenLabsVoice(voiceDraft);

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-white/10 bg-white/[0.02] p-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground">11Labs — голос</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Бот откроет Text to Speech, выберет модель Eleven v3, вставит ID голоса в поиск
          голоса, выберет карточку и сгенерирует полный закадровый текст.
        </p>
      </div>
      <label className="flex flex-col gap-1.5">
        <span className="text-sm font-medium text-foreground">Голос</span>
        <select
          className="h-9 w-full max-w-md rounded-md border border-input bg-background px-2 text-sm"
          value={voiceDraft}
          onChange={(e) => setVoiceDraft(e.target.value)}
        >
          {ELEVENLABS_VOICES.map((v) => (
            <option key={v.id} value={v.id}>
              {elevenLabsVoiceLabel(v)}
            </option>
          ))}
        </select>
        {selected ? (
          <span className="font-mono text-[11px] text-muted-foreground">ID: {selected.id}</span>
        ) : null}
      </label>
      <Button
        size="sm"
        className="w-fit self-start h-8 px-5 text-xs font-semibold text-white bg-cyan-600 hover:bg-cyan-500 border border-cyan-400/50 shadow-md shadow-cyan-500/20 rounded-xl transition-all disabled:opacity-50"
        disabled={saving}
        onClick={() =>
          onSave({
            elevenlabs_voice_id: voiceDraft || DEFAULT_ELEVENLABS_VOICE_ID,
          })
        }
      >
        {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
        Сохранить
      </Button>
    </section>
  );
}

function StepperControl({
  label,
  description,
  value,
  min,
  max,
  step,
  valueLabel,
  disabled,
  onChange,
}: {
  label: string;
  description?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  valueLabel: string;
  disabled?: boolean;
  onChange: (next: number) => void;
}) {
  const dec = () => onChange(Math.max(min, value - step));
  const inc = () => onChange(Math.min(max, value + step));

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium text-foreground">{label}</span>
      {description ? (
        <span className="text-xs text-muted-foreground">{description}</span>
      ) : null}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="h-8 w-8 shrink-0"
          disabled={disabled || value <= min}
          onClick={dec}
          aria-label="Уменьшить"
        >
          <ChevronDown className="h-4 w-4" />
        </Button>
        <span className="min-w-[7rem] text-center font-mono text-sm text-foreground">
          {valueLabel}
        </span>
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="h-8 w-8 shrink-0"
          disabled={disabled || value >= max}
          onClick={inc}
          aria-label="Увеличить"
        >
          <ChevronUp className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function AssembleFields({
  params,
  metaRecord,
  onSave,
  saving,
}: {
  params: NodeStepParamsMeta;
  metaRecord: Record<string, unknown>;
  onSave: (patch: AssembleStepParams) => void;
  saving: boolean;
}) {
  const subsOn = params.assemble?.subtitles_enabled === true;
  const tailSaved = params.assemble?.post_voiceover_tail_seconds ?? 0;
  const bgmFromMeta =
    typeof metaRecord.bgm_level === "number" ? Math.round(metaRecord.bgm_level) : 35;
  const bgmSaved = params.assemble?.bgm_level ?? bgmFromMeta;
  const sendToMain = params.assemble?.send_to_main_pc === true;
  const skipIntroOn = params.assemble?.skip_intro_enabled === true;
  const skipIntroSec = Math.min(
    2,
    Math.max(0, Number(params.assemble?.skip_intro_seconds ?? 0) || 0),
  );
  const skipIntroLabel = `${skipIntroSec.toFixed(2)} с`;

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-white/10 bg-white/[0.02] p-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground">Сборка FFmpeg</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Субтитры, хвост после озвучки, пропуск начала ролика и громкость фона из music/.
        </p>
      </div>
      <StepperControl
        label="Время видео после окончания озвучки"
        description="Секунды: последний кадр держится после конца голоса; музыка продолжается."
        value={tailSaved}
        min={0}
        max={120}
        step={1}
        valueLabel={`${tailSaved} с`}
        disabled={saving}
        onChange={(next) => onSave({ post_voiceover_tail_seconds: next })}
      />
      <StepperControl
        label="Громкость фоновой музыки"
        description="Файл bgm.mp3 в music/. 100% = 0 дБ на шкале слайдера."
        value={bgmSaved}
        min={0}
        max={100}
        step={5}
        valueLabel={`${bgmSaved} · ${bgmLevelToDb(bgmSaved)}`}
        disabled={saving}
        onChange={(next) => onSave({ bgm_level: next })}
      />
      <button
        type="button"
        disabled={saving}
        onClick={() => onSave({ send_to_main_pc: !sendToMain })}
        className={cn(
          "flex w-full items-start justify-between gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
          sendToMain ? "border-primary/40 bg-primary/10" : "border-border/60 hover:bg-accent/40",
        )}
      >
        <span className="flex flex-col">
          <span className="text-sm font-medium text-foreground">Отправить на основной ПК</span>
          <span className="text-xs text-muted-foreground">
            {sendToMain
              ? "После музыки проект уйдёт на hub для ASR и FFmpeg"
              : "Монтаж локально на этой станции"}
          </span>
        </span>
        <span
          className={cn(
            "mt-0.5 h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors border shadow-inner",
            sendToMain ? "bg-primary border-primary" : "bg-zinc-700/90 border-zinc-500/80",
          )}
        >
          <span
            className={cn(
              "block h-4 w-4 rounded-full bg-white shadow-md transition-transform",
              sendToMain && "translate-x-4",
            )}
          />
        </span>
      </button>
      <button
        type="button"
        disabled={saving}
        onClick={() => onSave({ subtitles_enabled: !subsOn })}
        className={cn(
          "flex w-full items-start justify-between gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
          subsOn ? "border-primary/40 bg-primary/10" : "border-border/60 hover:bg-accent/40",
        )}
      >
        <span className="flex flex-col">
          <span className="text-sm font-medium text-foreground">Субтитры в ролике</span>
          <span className="text-xs text-muted-foreground">
            {subsOn ? "Включены — ASS вшивается при сборке" : "Выключены — без прожига текста"}
          </span>
        </span>
        <span
          className={cn(
            "mt-0.5 h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors border shadow-inner",
            subsOn ? "bg-primary border-primary" : "bg-zinc-700/90 border-zinc-500/80",
          )}
        >
          <span
            className={cn(
              "block h-4 w-4 rounded-full bg-white shadow-md transition-transform",
              subsOn && "translate-x-4",
            )}
          />
        </span>
      </button>
      <button
        type="button"
        disabled={saving}
        onClick={() =>
          onSave({
            skip_intro_enabled: !skipIntroOn,
            skip_intro_seconds: skipIntroOn ? skipIntroSec : skipIntroSec || 0.5,
          })
        }
        className={cn(
          "flex w-full items-start justify-between gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
          skipIntroOn ? "border-primary/40 bg-primary/10" : "border-border/60 hover:bg-accent/40",
        )}
      >
        <span className="flex flex-col">
          <span className="text-sm font-medium text-foreground">Не учитывать первые секунды</span>
          <span className="text-xs text-muted-foreground">
            {skipIntroOn
              ? `Отрезаем начало ролика: ${skipIntroLabel}`
              : "Выкл — ролик с 0:00 без обрезки начала"}
          </span>
        </span>
        <span
          className={cn(
            "mt-0.5 h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors border shadow-inner",
            skipIntroOn ? "bg-primary border-primary" : "bg-zinc-700/90 border-zinc-500/80",
          )}
        >
          <span
            className={cn(
              "block h-4 w-4 rounded-full bg-white shadow-md transition-transform",
              skipIntroOn && "translate-x-4",
            )}
          />
        </span>
      </button>
      {skipIntroOn ? (
        <StepperControl
          label="Сколько секунд от начала отрезать"
          description="Шаг 0.01 с (сотые). Диапазон 0.00–2.00. Не учитываются в финальном mp4."
          value={Number(skipIntroSec.toFixed(2))}
          min={0}
          max={2}
          step={0.01}
          valueLabel={skipIntroLabel}
          disabled={saving}
          onChange={(next) =>
            onSave({
              skip_intro_enabled: true,
              skip_intro_seconds: Math.round(Math.min(2, Math.max(0, next)) * 100) / 100,
            })
          }
        />
      ) : null}
      {saving ? (
        <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          Сохранение…
        </span>
      ) : null}
    </section>
  );
}

function SplitFields({
  params,
  project,
  onSave,
  saving,
}: {
  params: NodeStepParamsMeta;
  project?: ProjectDetail | null;
  onSave: (patch: SplitStepParams) => void;
  saving: boolean;
}) {
  const split = params.split || {};
  const [minDraft, setMinDraft] = useState(split.cell_min_chars?.toString() ?? "");
  const [maxDraft, setMaxDraft] = useState(split.cell_max_chars?.toString() ?? "");
  const [avgMinDraft, setAvgMinDraft] = useState(split.cell_avg_min?.toString() ?? "");
  const [avgMaxDraft, setAvgMaxDraft] = useState(split.cell_avg_max?.toString() ?? "");

  useEffect(() => {
    setMinDraft(split.cell_min_chars?.toString() ?? "");
    setMaxDraft(split.cell_max_chars?.toString() ?? "");
    setAvgMinDraft(split.cell_avg_min?.toString() ?? "");
    setAvgMaxDraft(split.cell_avg_max?.toString() ?? "");
  }, [split.cell_avg_max, split.cell_avg_min, split.cell_max_chars, split.cell_min_chars]);

  const generalPlan = (project?.general_plan || ((project?.meta as Record<string, unknown> | undefined)?.general_plan as string) || "").trim();
  const scriptText = (project?.script_text || "").trim();
  const charCount = scriptText.length;

  const targetFrames = useMemo(() => {
    if (!generalPlan) return null;
    const matches = Array.from(generalPlan.matchAll(/(?:Кадр|кадр)\s*(\d+)/g)).map((m) => parseInt(m[1], 10));
    if (matches.length > 0) return Math.max(...matches);
    const m = generalPlan.match(/(\d+)\s*(?:кадр|кадров|кадра)/i);
    if (m) return parseInt(m[1], 10);
    return null;
  }, [generalPlan]);

  const calc = useMemo(() => {
    if (!targetFrames || targetFrames <= 0 || charCount <= 0) return null;
    const avg = Math.round(charCount / targetFrames);
    const min = Math.max(10, Math.round(avg * 0.5));
    const max = Math.max(40, Math.round(avg * 1.8));
    const avgMin = Math.max(15, Math.round(avg * 0.8));
    const avgMax = Math.max(25, Math.round(avg * 1.15));
    return { avg, min, max, avgMin, avgMax };
  }, [targetFrames, charCount]);

  const parseOpt = (s: string) => {
    const t = s.trim();
    if (!t) return null;
    const n = Math.round(Number(t));
    return Number.isFinite(n) && n > 0 ? n : null;
  };

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-white/10 bg-white/[0.02] p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground">Разбивка</h3>
      </div>

      {calc && targetFrames ? (
        <div className="flex flex-col gap-2 rounded-xl border border-cyan-500/30 bg-cyan-950/30 p-3 text-xs text-cyan-200 backdrop-blur-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-cyan-100 flex items-center gap-1.5">
              <Calculator className="h-3.5 w-3.5 text-cyan-400" />
              Расчёт под сценарий ролика
            </span>
            <Button
              type="button"
              size="sm"
              className="h-7 px-3 text-xs bg-cyan-600 hover:bg-cyan-500 text-white font-medium rounded-lg border border-cyan-400/40 shadow-sm shadow-cyan-500/20"
              onClick={() => {
                setMinDraft(calc.min.toString());
                setMaxDraft(calc.max.toString());
                setAvgMinDraft(calc.avgMin.toString());
                setAvgMaxDraft(calc.avgMax.toString());
                toast.success(
                  `Подставлены параметры под ${targetFrames} кадров: ${calc.min} / ${calc.max} / ${calc.avgMin}–${calc.avgMax}`,
                );
              }}
            >
              Применить расчёт ({targetFrames} кадр.)
            </Button>
          </div>
          <p className="text-cyan-200/80 leading-relaxed">
            В сценарии <strong>{targetFrames} кадров</strong>, закадровый текст <strong>{charCount} знаков</strong>.
            Средняя длина кадра: <strong>~{calc.avg} знаков (~{Math.round((calc.avg / 14) * 10) / 10} сек)</strong>.
          </p>
        </div>
      ) : null}

      <NumField
        label="Минимальное количество символов в кадре"
        description="Стандартное значение: 30 символов (~2 сек озвучки)"
        placeholder="30"
        value={minDraft}
        onChange={setMinDraft}
      />
      <NumField
        label="Максимальное количество символов в кадре"
        description="Стандартное значение: 120 символов (~8 сек озвучки)"
        placeholder="120"
        value={maxDraft}
        onChange={setMaxDraft}
      />
      <div className="grid gap-3 sm:grid-cols-2">
        <NumField
          label="Среднее значение — от"
          description="Стандарт: 50 знаков"
          placeholder="50"
          value={avgMinDraft}
          onChange={setAvgMinDraft}
        />
        <NumField
          label="Среднее значение — до"
          description="Стандарт: 80 знаков"
          placeholder="80"
          value={avgMaxDraft}
          onChange={setAvgMaxDraft}
        />
      </div>
      <Button
        size="sm"
        className="w-fit self-start h-8 px-5 text-xs font-semibold text-white bg-cyan-600 hover:bg-cyan-500 border border-cyan-400/50 shadow-md shadow-cyan-500/20 rounded-xl transition-all disabled:opacity-50"
        disabled={saving}
        onClick={() =>
          onSave({
            cell_min_chars: parseOpt(minDraft),
            cell_max_chars: parseOpt(maxDraft),
            cell_avg_min: parseOpt(avgMinDraft),
            cell_avg_max: parseOpt(avgMaxDraft),
          })
        }
      >
        {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
        Сохранить
      </Button>
    </section>
  );
}

function MediaStreamsFields({
  metaRecord,
  onSave,
  saving,
  nodeType,
}: {
  metaRecord: Record<string, unknown>;
  onSave: (metaPatch: Record<string, unknown>) => void;
  saving: boolean;
  nodeType: "images" | "videos";
}) {
  const currentStreams = Number(
    metaRecord.outsee_streams ?? metaRecord.img_streams ?? 2,
  );
  const [streams, setStreams] = useState<number>(currentStreams);

  useEffect(() => {
    setStreams(
      Number(metaRecord.outsee_streams ?? metaRecord.img_streams ?? 2),
    );
  }, [metaRecord.outsee_streams, metaRecord.img_streams]);

  const handleSelect = (val: number) => {
    setStreams(val);
    onSave({
      ...metaRecord,
      img_streams: val,
      outsee_streams: val,
    });
  };

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-white/10 bg-white/[0.02] p-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground">
          Параллельные потоки генерации
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Количество одновременных задач в Outsee API для генерации {nodeType === "images" ? "картинок" : "видео"}.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2 pt-1">
        {[1, 2, 3, 4].map((num) => (
          <Button
            key={num}
            type="button"
            size="sm"
            variant={streams === num ? "default" : "outline"}
            disabled={saving}
            className={cn(
              "h-8 px-3.5 text-xs",
              streams === num
                ? "bg-primary text-black font-semibold shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
            onClick={() => handleSelect(num)}
          >
            {num} {num === 1 ? "поток" : num < 5 ? "потока" : "потоков"}
            {num === 2 && <span className="ml-1 opacity-60 text-[10px]">(дефолт)</span>}
            {num === 4 && <span className="ml-1 opacity-60 text-[10px]">(макс)</span>}
          </Button>
        ))}
      </div>

      <p className="text-[11px] text-muted-foreground">
        💡 При выборе 3 или 4 потоков генерация кадров завершается быстрее, если ваш API-токен поддерживает параллельные запросы.
      </p>
    </section>
  );
}

export function NodeStepParamsPanel({
  projectId,
  nodeType,
}: {
  projectId: number;
  nodeType: string;
}) {
  const qc = useQueryClient();
  const step =
    nodeType === "plan" ||
    nodeType === "script" ||
    nodeType === "split" ||
    nodeType === "audio" ||
    nodeType === "assemble" ||
    nodeType === "images" ||
    nodeType === "videos"
      ? nodeType
      : null;

  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
    enabled: projectId > 0 && step != null,
  });

  const save = useMutation({
    mutationFn: async ({
      meta,
    }: {
      meta: Record<string, unknown>;
    }) => api.patchProject(projectId, { meta }),
    onSuccess: () => {
      toast.success("Параметры сохранены");
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["gpt-text", projectId] });
    },
    onError: (e) => toast.error(errorMessageFromUnknown(e)),
  });

  const metaRecord = (project.data?.meta || {}) as Record<string, unknown>;
  const params = useMemo(() => readNodeStepParams(metaRecord), [metaRecord]);

  if (!step || (project.isLoading && !project.data)) return null;

  const persist = (
    patch: PlanScriptStepParams | SplitStepParams | AudioStepParams | AssembleStepParams,
  ) => {
    if (step === "images" || step === "videos" || !step) return;
    save.mutate({ meta: withNodeStepParams(metaRecord, step, patch) });
  };

  const persistMeta = (metaPatch: Record<string, unknown>) => {
    save.mutate({ meta: metaPatch });
  };

  return (
    <div className="flex flex-col gap-4">
      {step !== "audio" && step !== "assemble" && step !== "images" && step !== "videos" ? (
        <p className="text-xs text-muted-foreground">
          Эти параметры автоматически добавляются в конец сопроводительного текста для генератора
          (вкладка «Промпты»).
        </p>
      ) : null}
      {step === "plan" ? (
        <PlanScriptFields
          header="Сценарий"
          step="plan"
          params={params}
          inheritedFromPlan={false}
          onSave={persist}
          saving={save.isPending}
        />
      ) : null}
      {step === "script" ? (
        <PlanScriptFields
          header="Закадровый текст"
          step="script"
          params={params}
          inheritedFromPlan={
            !(params.script?.duration_seconds != null && params.script.duration_seconds > 0) &&
            effectiveDurationSeconds(params, "script") != null
          }
          onSave={persist}
          saving={save.isPending}
        />
      ) : null}
      {step === "split" ? (
        <SplitFields params={params} project={project.data} onSave={persist} saving={save.isPending} />
      ) : null}
      {step === "audio" ? (
        <AudioFields params={params} onSave={persist} saving={save.isPending} />
      ) : null}
      {step === "assemble" ? (
        <AssembleFields
          params={params}
          metaRecord={metaRecord}
          onSave={persist}
          saving={save.isPending}
        />
      ) : null}
      {step === "images" || step === "videos" ? (
        <MediaStreamsFields
          metaRecord={metaRecord}
          onSave={persistMeta}
          saving={save.isPending}
          nodeType={step}
        />
      ) : null}
    </div>
  );
}
