import { useState, useRef, useCallback, useEffect } from "react";
import CustomSelect from "./CustomSelect";

const LANGUAGES = [
  { code: "auto", name: "Auto-detect" },
  { code: "en", name: "English" },
  { code: "de", name: "German" },
  { code: "fr", name: "French" },
  { code: "es", name: "Spanish" },
  { code: "it", name: "Italian" },
  { code: "zh-Hans", name: "Chinese" },
  { code: "ja", name: "Japanese" },
];

const TARGET_LANGUAGES = LANGUAGES.filter((l) => l.code !== "auto");

const MODES = [
  {
    value: "azure",
    label: "Azure Translate",
    desc: "Fast machine translation via Azure AI",
    icon: "⚡",
  },
  {
    value: "llm",
    label: "LLM Only",
    desc: "AI-powered translation using a large language model",
    icon: "🤖",
  },
  {
    value: "premium",
    label: "2-Pass Premium",
    desc: "Azure translation refined by LLM — highest quality",
    icon: "🔬",
    badge: "Premium",
  },
];

export default function TextTranslate() {
  const [sourceText, setSourceText] = useState("");
  const [translatedText, setTranslatedText] = useState("");
  const [sourceLang, setSourceLang] = useState("auto");
  const [targetLang, setTargetLang] = useState("en");
  const [mode, setMode] = useState("azure");

  // idle | translating | done | error
  const [phase, setPhase] = useState("idle");
  const [error, setError] = useState("");

  const textareaRef = useRef(null);

  /* auto-select target language */
  useEffect(() => {
    if (sourceLang === "en") setTargetLang("de");
    else if (sourceLang !== "auto" && targetLang === sourceLang)
      setTargetLang("en");
  }, [sourceLang]);

  /* ---- submit ---- */
  const handleTranslate = async () => {
    if (!sourceText.trim()) return;
    setPhase("translating");
    setError("");
    setTranslatedText("");

    const form = new FormData();
    form.append("text", sourceText);
    form.append("source_language", sourceLang);
    form.append("target_language", targetLang);
    form.append("mode", mode);

    try {
      const res = await fetch("/api/translate-text", {
        method: "POST",
        body: form,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Server error ${res.status}`);
      }
      const data = await res.json();
      setTranslatedText(data.translated_text);
      setPhase("done");
    } catch (err) {
      setError(err.message);
      setPhase("error");
    }
  };

  /* ---- copy result ---- */
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(translatedText);
    } catch {
      /* fallback */
      const ta = document.createElement("textarea");
      ta.value = translatedText;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
  };

  /* ---- reset ---- */
  const resetAll = () => {
    setSourceText("");
    setTranslatedText("");
    setPhase("idle");
    setError("");
  };

  const isTranslating = phase === "translating";
  const charCount = sourceText.length;
  const charLimit = 50000;

  return (
    <div className="card">
      {/* Source text area */}
      <div className="text-field">
        <label className="text-field__label">Source Text</label>
        <textarea
          ref={textareaRef}
          className="text-field__textarea"
          placeholder="Type or paste your text here…"
          value={sourceText}
          onChange={(e) => {
            setSourceText(e.target.value);
            if (phase === "done" || phase === "error") setPhase("idle");
          }}
          disabled={isTranslating}
          rows={6}
        />
        <span
          className={`text-field__count ${charCount > charLimit ? "text-field__count--over" : ""}`}
        >
          {charCount.toLocaleString()} / {charLimit.toLocaleString()}
        </span>
      </div>

      {/* Language selectors */}
      <div className="lang-row">
        <CustomSelect
          label="From"
          value={sourceLang}
          onChange={setSourceLang}
          options={LANGUAGES}
        />

        <span className="lang-arrow animate-pulse">➜</span>

        <CustomSelect
          label="To"
          value={targetLang}
          onChange={setTargetLang}
          options={TARGET_LANGUAGES}
        />
      </div>

      {/* Mode selector */}
      <div className="mode-selector">
        <span className="mode-selector__label">Translation Mode</span>
        <div className="mode-options">
          {MODES.map((m) => (
            <label
              key={m.value}
              className={`mode-option ${mode === m.value ? "mode-option--active" : ""}`}
            >
              <input
                type="radio"
                name="mode"
                value={m.value}
                checked={mode === m.value}
                onChange={() => setMode(m.value)}
                disabled={isTranslating}
              />
              <span className="mode-option__icon">{m.icon}</span>
              <span className="mode-option__body">
                <span className="mode-option__name">
                  {m.label}
                  {m.badge && (
                    <span className="enhance-toggle__badge">{m.badge}</span>
                  )}
                </span>
                <span className="mode-option__desc">{m.desc}</span>
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* Translate button */}
      <button
        className={`btn-translate ${isTranslating ? "btn-translate--loading" : ""}`}
        onClick={handleTranslate}
        disabled={!sourceText.trim() || charCount > charLimit || isTranslating}
      >
        {isTranslating ? (
          <>
            Translating
            <span className="dots">
              <span className="dot" />
              <span className="dot" />
              <span className="dot" />
            </span>
          </>
        ) : (
          <>🚀 Translate</>
        )}
      </button>

      {/* Progress bar */}
      {isTranslating && (
        <div className="progress-bar">
          <div className="progress-bar__shimmer" />
        </div>
      )}

      {/* Result */}
      {translatedText && (
        <div className="text-result animate-pop">
          <div className="text-result__header">
            <span className="text-result__title">✅ Translation</span>
            <button className="text-result__copy" onClick={handleCopy} title="Copy to clipboard">
              📋 Copy
            </button>
          </div>
          <div className="text-result__content">{translatedText}</div>
        </div>
      )}

      {/* Error */}
      {phase === "error" && (
        <div className="error animate-shake">⚠️ {error}</div>
      )}

      {/* New translation */}
      {phase === "done" && (
        <button className="btn-new" onClick={resetAll}>
          🔄 New Translation
        </button>
      )}
    </div>
  );
}
