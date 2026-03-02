import { useState, useRef, useEffect, useCallback } from "react";

/**
 * CustomSelect – a styled dropdown that replaces native <select>.
 *
 * Props:
 *   value       – current selected value (string)
 *   onChange     – callback(newValue)
 *   options      – [{ code, name }]
 *   label        – optional top label text
 *   placeholder  – shown when nothing selected
 */
export default function CustomSelect({
  value,
  onChange,
  options = [],
  label,
  placeholder = "Select…",
}) {
  const [open, setOpen] = useState(false);
  const [focusIdx, setFocusIdx] = useState(-1);
  const containerRef = useRef(null);
  const listRef = useRef(null);

  const selected = options.find((o) => o.code === value);

  /* ---- close on outside click ---- */
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  /* ---- scroll focused item into view ---- */
  useEffect(() => {
    if (!open || focusIdx < 0 || !listRef.current) return;
    const item = listRef.current.children[focusIdx];
    if (item) item.scrollIntoView({ block: "nearest" });
  }, [focusIdx, open]);

  /* ---- keyboard navigation ---- */
  const onKeyDown = useCallback(
    (e) => {
      if (!open) {
        if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
          e.preventDefault();
          setOpen(true);
          setFocusIdx(options.findIndex((o) => o.code === value));
        }
        return;
      }

      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setFocusIdx((i) => Math.min(i + 1, options.length - 1));
          break;
        case "ArrowUp":
          e.preventDefault();
          setFocusIdx((i) => Math.max(i - 1, 0));
          break;
        case "Enter":
        case " ":
          e.preventDefault();
          if (focusIdx >= 0 && focusIdx < options.length) {
            onChange(options[focusIdx].code);
          }
          setOpen(false);
          break;
        case "Escape":
        case "Tab":
          setOpen(false);
          break;
        default:
          break;
      }
    },
    [open, focusIdx, options, value, onChange]
  );

  const toggle = () => {
    setOpen((prev) => {
      if (!prev) setFocusIdx(options.findIndex((o) => o.code === value));
      return !prev;
    });
  };

  const pick = (code) => {
    onChange(code);
    setOpen(false);
  };

  return (
    <div className="cselect" ref={containerRef}>
      {label && <span className="cselect__label">{label}</span>}

      <button
        type="button"
        className={`cselect__trigger${open ? " cselect__trigger--open" : ""}`}
        onClick={toggle}
        onKeyDown={onKeyDown}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="cselect__value">
          {selected ? selected.name : placeholder}
        </span>
        <svg
          className={`cselect__chevron${open ? " cselect__chevron--open" : ""}`}
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <ul
          className="cselect__menu"
          ref={listRef}
          role="listbox"
          aria-activedescendant={
            focusIdx >= 0 ? `cselect-opt-${options[focusIdx]?.code}` : undefined
          }
        >
          {options.map((opt, i) => {
            const isSelected = opt.code === value;
            const isFocused = i === focusIdx;
            return (
              <li
                key={opt.code}
                id={`cselect-opt-${opt.code}`}
                role="option"
                aria-selected={isSelected}
                className={
                  "cselect__option" +
                  (isSelected ? " cselect__option--selected" : "") +
                  (isFocused ? " cselect__option--focused" : "")
                }
                onClick={() => pick(opt.code)}
                onMouseEnter={() => setFocusIdx(i)}
              >
                <span className="cselect__option-text">{opt.name}</span>
                {isSelected && (
                  <svg
                    className="cselect__check"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="3"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
