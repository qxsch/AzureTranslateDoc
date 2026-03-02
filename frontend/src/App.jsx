import { useState } from "react";
import "./App.css";
import DocumentTranslate from "./DocumentTranslate";
import TextTranslate from "./TextTranslate";

/* ------------------------------------------------------------------ */
/*  Main App – shell with tab navigation                              */
/* ------------------------------------------------------------------ */
export default function App() {
  const [view, setView] = useState("document"); // "document" | "text"

  return (
    <div className="app">
      {/* header */}
      <header className="header">
        <h1 className="header__title">
          <span className="header__icon">🌍</span> TranslateDoc
        </h1>
        <p className="header__sub">
          Translate documents and text — fast, accurate, done!
        </p>
      </header>

      {/* navigation tabs */}
      <nav className="nav-tabs">
        <button
          className={`nav-tab ${view === "document" ? "nav-tab--active" : ""}`}
          onClick={() => setView("document")}
        >
          <span className="nav-tab__icon">📄</span>
          Document
        </button>
        <button
          className={`nav-tab ${view === "text" ? "nav-tab--active" : ""}`}
          onClick={() => setView("text")}
        >
          <span className="nav-tab__icon">✏️</span>
          Text
        </button>
      </nav>

      {/* view */}
      {view === "document" ? <DocumentTranslate /> : <TextTranslate />}

      <footer className="footer">
        Powered by Azure AI Translator &middot; Built with ❤️
      </footer>
    </div>
  );
}
