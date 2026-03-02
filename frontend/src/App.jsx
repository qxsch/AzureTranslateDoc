import { useState, useRef, useCallback, useEffect } from "react";
import "./App.css";

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

/* Hardcoded fallback used until the API responds */
const FALLBACK_EXTENSIONS = new Set([".pdf", ".docx", ".txt", ".md"]);
const FALLBACK_ACCEPT = ".pdf,.docx,.txt,.md";

const POLL_INTERVAL = 2000; // ms

/* ------------------------------------------------------------------ */
/*  Main App                                                          */
/* ------------------------------------------------------------------ */
export default function App() {
  const [files, setFiles] = useState([]);
  const [sourceLang, setSourceLang] = useState("auto");
  const [targetLang, setTargetLang] = useState("en");
  const [enhanceAccuracy, setEnhanceAccuracy] = useState(false);

  // idle | uploading | processing | done | error
  const [phase, setPhase] = useState("idle");
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);

  const [dragActive, setDragActive] = useState(false);
  const inputRef = useRef(null);
  const pollRef = useRef(null);

  /* ---- supported formats (fetched from backend) ---- */
  const [supportedExts, setSupportedExts] = useState(FALLBACK_EXTENSIONS);
  const [acceptString, setAcceptString] = useState(FALLBACK_ACCEPT);
  const [formatLabels, setFormatLabels] = useState([]);
  const [fileWarning, setFileWarning] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/formats");
        if (!res.ok) return;
        const { formats } = await res.json();
        if (cancelled || !formats?.length) return;

        const exts = new Set();
        for (const fmt of formats) {
          for (const ext of fmt.fileExtensions ?? []) {
            exts.add(ext.startsWith(".") ? ext.toLowerCase() : `.${ext.toLowerCase()}`);
          }
        }
        setSupportedExts(exts);
        setAcceptString([...exts].sort().join(","));
        setFormatLabels([...exts].sort());
      } catch {
        /* keep fallback */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  /* auto-select target language */
  useEffect(() => {
    if (sourceLang === "en") setTargetLang("de");
    else if (sourceLang !== "auto" && targetLang === sourceLang) setTargetLang("en");
  }, [sourceLang]);

  /* Validate file extension */
  const isFileSupported = useCallback(
    (f) => {
      if (!f?.name) return false;
      const ext = f.name.includes(".")
        ? `.${f.name.split(".").pop().toLowerCase()}`
        : "";
      return supportedExts.has(ext);
    },
    [supportedExts],
  );

  /* ---- file selection ---- */
  const addFiles = useCallback(
    (newFiles) => {
      const arr = Array.from(newFiles);
      setFiles((prev) => [...prev, ...arr]);
      setPhase("idle");
      setError("");
      setJobId(null);
      setJobStatus(null);

      const unsupported = arr.filter((f) => !isFileSupported(f));
      if (unsupported.length > 0) {
        setFileWarning(
          `Unsupported: ${unsupported.map((f) => f.name).join(", ")}. ` +
          `Accepted: ${[...supportedExts].sort().join(", ")}`,
        );
      } else {
        setFileWarning("");
      }
    },
    [isFileSupported, supportedExts],
  );

  const removeFile = (index) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    setFileWarning("");
  };

  /* ---- drag & drop handlers ---- */
  const onDragOver = (e) => {
    e.preventDefault();
    setDragActive(true);
  };
  const onDragLeave = () => setDragActive(false);
  const onDrop = (e) => {
    e.preventDefault();
    setDragActive(false);
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
  };
  const onFileChange = (e) => {
    if (e.target.files?.length) addFiles(e.target.files);
    e.target.value = ""; // allow re-selecting same files
  };

  /* ---- polling ---- */
  useEffect(() => {
    if (!jobId || phase !== "processing") return;

    const poll = async () => {
      try {
        const res = await fetch(`/api/jobs/${jobId}`);
        if (!res.ok) throw new Error("Failed to fetch job status");
        const data = await res.json();
        setJobStatus(data);

        if (data.status === "completed" || data.status === "completed_with_errors") {
          setPhase("done");
          clearInterval(pollRef.current);
        }
      } catch (err) {
        setError(err.message);
        setPhase("error");
        clearInterval(pollRef.current);
      }
    };

    poll(); // immediate first poll
    pollRef.current = setInterval(poll, POLL_INTERVAL);

    return () => clearInterval(pollRef.current);
  }, [jobId, phase]);

  /* ---- submit ---- */
  const handleTranslate = async () => {
    if (!files.length) return;
    setPhase("uploading");
    setError("");
    setJobStatus(null);

    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    form.append("source_language", sourceLang);
    form.append("target_language", targetLang);
    if (enhanceAccuracy) form.append("enhance_accuracy", "true");

    try {
      const res = await fetch("/api/translate", { method: "POST", body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Server error ${res.status}`);
      }
      const { job_id } = await res.json();
      setJobId(job_id);
      setPhase("processing");
    } catch (err) {
      setError(err.message);
      setPhase("error");
    }
  };

  /* ---- download single file ---- */
  const handleDownload = async (fileIndex, outputName) => {
    try {
      const res = await fetch(`/api/jobs/${jobId}/files/${fileIndex}/download`);
      if (!res.ok) throw new Error("Download failed");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = outputName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    }
  };

  /* ---- download all completed files ---- */
  const handleDownloadAll = async () => {
    if (!jobStatus?.files) return;
    for (const f of jobStatus.files) {
      if (f.status === "completed") {
        await handleDownload(f.index, f.output_name);
      }
    }
  };

  /* ---- reset ---- */
  const resetAll = () => {
    setFiles([]);
    setPhase("idle");
    setJobId(null);
    setJobStatus(null);
    setError("");
    setFileWarning("");
  };

  /* ---- helpers ---- */
  const formatSize = (bytes) =>
    bytes < 1024
      ? `${bytes} B`
      : bytes < 1048576
        ? `${(bytes / 1024).toFixed(1)} KB`
        : `${(bytes / 1048576).toFixed(1)} MB`;

  const allFilesSupported = files.length > 0 && files.every(isFileSupported);
  const completedCount =
    jobStatus?.files?.filter((f) => f.status === "completed").length ?? 0;
  const isProcessing = phase === "uploading" || phase === "processing";

  const statusIcon = (s) =>
    s === "completed" ? "✅" :
    s === "error" ? "❌" :
    s === "translating" ? "⏳" :
    s === "enhancing" ? "🔬" : "⏸️";

  const statusLabel = (f) => {
    if (f.status === "completed") return "Done";
    if (f.status === "error") return f.error?.slice(0, 80) || "Failed";
    if (f.status === "translating") return "Translating…";
    if (f.status === "enhancing") {
      if (f.substatus === "pass1") return "Pass 1 — translating…";
      if (f.substatus === "extracting") return "Extracting text…";
      if (f.substatus === "glossary") return "Generating glossary…";
      if (f.substatus === "pass2") return "Pass 2 — re-translating…";
      return "Enhancing…";
    }
    return "Pending";
  };

  /* ---- Render ---- */
  return (
    <div className="app">
      {/* header */}
      <header className="header">
        <h1 className="header__title">
          <span className="header__icon">🌍</span> TranslateDoc
        </h1>
        <p className="header__sub">
          Drop documents, pick languages, hit translate — done!
        </p>
      </header>

      <main className="card">
        {/* Drop zone */}
        <div
          className={`dropzone ${dragActive ? "dropzone--active" : ""} ${files.length ? "dropzone--has-file" : ""}`}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          role="button"
          tabIndex={0}
        >
          <input
            ref={inputRef}
            type="file"
            accept={acceptString}
            onChange={onFileChange}
            multiple
            hidden
          />
          {files.length ? (
            <div className="file-list" onClick={(e) => e.stopPropagation()}>
              {files.map((f, i) => (
                <div key={`${f.name}-${i}`} className="file-chip animate-pop">
                  <span className="file-chip__icon">📄</span>
                  <span className="file-chip__name">{f.name}</span>
                  <span className="file-chip__size">{formatSize(f.size)}</span>
                  {!isProcessing && phase !== "done" && (
                    <button
                      className="file-chip__remove"
                      onClick={() => removeFile(i)}
                      title="Remove"
                    >
                      ×
                    </button>
                  )}
                </div>
              ))}
              {!isProcessing && phase !== "done" && (
                <div
                  className="file-list__add-hint"
                  onClick={() => inputRef.current?.click()}
                >
                  + Click or drop to add more
                </div>
              )}
            </div>
          ) : (
            <div className="dropzone__prompt">
              <span className="dropzone__big-icon animate-float">📁</span>
              <p className="dropzone__text">
                Drag &amp; drop files here, or <strong>click to browse</strong>
              </p>
              <p className="dropzone__hint">
                {formatLabels.length
                  ? `Supports ${formatLabels.join(", ")} · Multiple files OK`
                  : "Supports PDF, Word (.docx), TXT, Markdown · Multiple files OK"}
              </p>
            </div>
          )}
        </div>

        {/* Language selectors */}
        <div className="lang-row">
          <label className="lang-select">
            <span className="lang-select__label">From</span>
            <select
              value={sourceLang}
              onChange={(e) => setSourceLang(e.target.value)}
            >
              {LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.name}
                </option>
              ))}
            </select>
          </label>

          <span className="lang-arrow animate-pulse">➜</span>

          <label className="lang-select">
            <span className="lang-select__label">To</span>
            <select
              value={targetLang}
              onChange={(e) => setTargetLang(e.target.value)}
            >
              {TARGET_LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* File type warning */}
        {fileWarning && (
          <div className="warning animate-shake">⚠️ {fileWarning}</div>
        )}

        {/* Enhance accuracy toggle */}
        <label className="enhance-toggle">
          <input
            type="checkbox"
            checked={enhanceAccuracy}
            onChange={(e) => setEnhanceAccuracy(e.target.checked)}
            disabled={isProcessing || phase === "done"}
          />
          <span className="enhance-toggle__content">
            <span className="enhance-toggle__label">
              🔬 Enhance accuracy
              <span className="enhance-toggle__badge">Premium</span>
            </span>
            <span className="enhance-toggle__hint">
              Two-pass translation with AI glossary — slower but more accurate for
              domain-specific documents
            </span>
          </span>
        </label>

        {/* Translate button */}
        <button
          className={`btn-translate ${isProcessing ? "btn-translate--loading" : ""}`}
          onClick={handleTranslate}
          disabled={!allFilesSupported || isProcessing || phase === "done"}
        >
          {isProcessing ? (
            <>
              Translating
              <span className="dots">
                <span className="dot" />
                <span className="dot" />
                <span className="dot" />
              </span>
            </>
          ) : phase === "done" ? (
            <>✅ Complete</>
          ) : (
            <>🚀 {enhanceAccuracy ? "Enhanced " : ""}Translate{files.length > 1 ? ` ${files.length} files` : ""}</>
          )}
        </button>

        {/* Progress bar */}
        {isProcessing && (
          <div className="progress-bar">
            <div className="progress-bar__shimmer" />
          </div>
        )}

        {/* Per-file status during/after processing */}
        {jobStatus && (
          <div className="job-results animate-pop">
            {jobStatus.files.map((f) => (
              <div key={f.index} className={`result-row result-row--${f.status}`}>
                <span className="result-row__icon">{statusIcon(f.status)}</span>
                <span className="result-row__name">{f.output_name}</span>
                <span className="result-row__status">
                  {statusLabel(f)}
                </span>
                {f.status === "completed" && (
                  <button
                    className="btn-download-small"
                    onClick={() => handleDownload(f.index, f.output_name)}
                  >
                    ⬇️
                  </button>
                )}
              </div>
            ))}

            {completedCount > 0 && phase === "done" && (
              <button className="btn-download" onClick={handleDownloadAll}>
                ⬇️ Download{" "}
                {completedCount > 1
                  ? `all ${completedCount} files`
                  : jobStatus.files[0]?.output_name}
              </button>
            )}
          </div>
        )}

        {/* Error */}
        {phase === "error" && (
          <div className="error animate-shake">⚠️ {error}</div>
        )}

        {/* New translation button */}
        {phase === "done" && (
          <button className="btn-new" onClick={resetAll}>
            🔄 New Translation
          </button>
        )}
      </main>

      <footer className="footer">
        Powered by Azure AI Translator &middot; Built with ❤️
      </footer>
    </div>
  );
}
